#!/usr/bin/env python3
"""
acd_fast.py — PARALLEL Adobe Connect recording downloader (much faster than realtime).

Instead of playing each stream once at 1× (≈77 min for a 77-min class), this opens
MANY seeked chunks concurrently on the ONE allowed connection and stitches them, so a
77-min recording comes down in ~10-15 min.

How it works (validated in research/parallel_test.py):
  - The gateway serves multiple concurrent seeked `play`s on a single connection.
  - `play start=K length=L` seeks to K (within a stream's own timeline) and starts on a
    keyframe → each chunk is independently decodable.
  - Seeked frames carry RELATIVE timestamps (0..L), so each chunk is offset by its start
    when rebuilding the per-stream FLV.

Usage:
    python3 acd_fast.py "https://HOST/XXXX/?session=YYYY" [-o out.mp4]
            [--par 12] [--chunk 300] [--seconds N] [--keep] [--cookies cookies.txt]

IMPORTANT: close the recording in your browser first — Adobe Connect allows only ONE
connection per account, and the browser will otherwise hold the slot. Needs ffmpeg.
"""
import sys, os, ssl, json, time, math, asyncio, argparse, collections, shutil
import acd  # reuse FLV writing, parse_bin, cookies, get_session_info, mux
try:
    import websockets
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "websockets"], check=True)
    import websockets

log = acd.log
MARGIN = 6.0  # extra seconds requested per chunk so seams overlap (no gaps); dedup at write

class FastDownloader:
    def __init__(self, info_provider, par=12, chunk_sec=300, seconds=None):
        self.info_provider = info_provider
        self.par = par; self.chunk_sec = chunk_sec; self.seconds = seconds
        self.media = None; self.duration = None
        self.chunks = []                  # list of chunk dicts (persist across reconnects)
        self.last_frame_t = 0.0

    # ---- per-connection state ----
    def _reset_conn(self):
        self.connected = asyncio.Event(); self.logged_in = asyncio.Event(); self.created = {}
        self.streams = {}; self.meta = {}; self.nsid2chunk = {}; self.ws_closed = False
        self._hb = None

    async def _send(self, o): await self.ws.send(json.dumps(o))
    async def _heartbeat(self):
        # the browser sends @getStats every ~5s; without it the gateway times the
        # connection out after ~90-130s. This is the connection keepalive.
        rid = 1000
        try:
            while True:
                await asyncio.sleep(5)
                rid += 1
                await self._send({"type": "NCFunc", "method": "call", "method-name": "@getStats", "responderId": rid})
        except asyncio.CancelledError: raise
        except Exception: self.ws_closed = True
    async def _create(self, nsid):
        self.created.setdefault(nsid, asyncio.Event())
        await self._send({"type": "NCFunc", "method": "createNetStream", "nsId": nsid, "mediaAvailable": False})
        await asyncio.wait_for(self.created[nsid].wait(), 8)

    async def _reader(self):
        try:
            while True:
                msg = await self.ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    r = acd.parse_bin(msg)
                    if r:
                        typ, ts, nsid, pl = r
                        ci = self.nsid2chunk.get(nsid)
                        if ci is not None:
                            c = self.chunks[ci]      # re-base relative ts onto the chunk's own timeline
                            c["frames"].append((typ, c["ts_offset"] + ts, pl)); self.last_frame_t = time.monotonic()
                    continue
                j = json.loads(msg); st = j.get("status") or {}; code = st.get("code"); desc = st.get("description")
                if code == "NetConnection.Connect.Success": self.connected.set()
                if j.get("method") == "onCommand":
                    a0 = (j.get("params") or {}).get("arg_0")
                    if isinstance(a0, dict) and a0.get("command") == "accepted": self.logged_in.set()
                if desc == "StreamCreated": self.created.setdefault(j.get("nsId"), asyncio.Event()).set()
                if code and ("Play.Stop" in code or "Play.Complete" in code or "Play.UnpublishNotify" in code):
                    ci = self.nsid2chunk.get(j.get("nsId"))
                    if ci is not None: self.chunks[ci]["done"] = True
                cmd = j.get("cmdString")
                if cmd == "onMetaData": self.meta["duration"] = j["params"]["arg_0"].get("duration")
                if cmd == "playEvent" and isinstance(j["params"].get("arg_2"), list):
                    for s in j["params"]["arg_2"]:
                        if isinstance(s, dict) and s.get("streamName"):
                            self.streams.setdefault(s["streamName"], s)
        except asyncio.CancelledError: raise
        except Exception: self.ws_closed = True

    async def _cleanup(self):
        for t in (getattr(self, "_hb", None), getattr(self, "_rt", None)):
            try:
                if t: t.cancel()
            except Exception: pass
        try: await self.ws.close()
        except Exception: pass

    async def _open(self, info):
        """Open one primed connection; discover streams on first call."""
        self._reset_conn()
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        self.ws = await websockets.connect(f"wss://{info['host']}:1443/", ssl=ctx, origin=f"https://{info['host']}",
                additional_headers={"User-Agent": acd.UA}, compression=None, max_size=None, open_timeout=20)
        self._rt = asyncio.create_task(self._reader())
        await self._send({"type": "WSFunc", "method": "startHeartbeat", "value": True})
        await self._send({"type": "WSFunc", "method": "allowPacketDrop", "value": False})
        await self._send({"type": "WSFunc", "method": "fragmentVideoPacket", "value": False})
        await self._send({"type": "NCFunc", "method": "connect", "url": info["connect_url"],
                          "params": {"ticket": info["ticket"], "reconnection": False,
                                     "swfUrl": f"https://{info['host']}/common/meetinghtml/index.html", "Recording": True}})
        await asyncio.wait_for(self.connected.wait(), 5)
        # The gateway must register us into the meeting (loginHandler 'accepted') before it
        # honors ANY stream op. A REUSED ticket yields Connect.Success but NO 'accepted' — the
        # ticket is effectively single-use — so _establish re-mints a fresh ticket per attempt.
        await asyncio.wait_for(self.logged_in.wait(), 5)
        self._hb = asyncio.create_task(self._heartbeat())     # keepalive every 5s, or the gateway drops us
        await self._create("nsID_0")
        await self._send({"type": "NCFunc", "method": "call", "method-name": "startLoadEditInfo", "params": {}, "responderId": 1})
        await self._send({"type": "NCFunc", "method": "call", "method-name": "preloadStreams", "responderId": 2})
        await self._send({"type": "NSFunc", "method": "play", "nsId": "nsID_0", "streamName": "indexstream", "start": 0, "length": -1, "reset": 3, "mediaAvailable": False})
        await self._send({"type": "NSFunc", "method": "play", "nsId": "nsID_0", "streamName": "indexstream", "start": 0, "length": 5, "reset": 2, "mediaAvailable": False})
        if self.media is None:
            last = -1; stable = 0
            for _ in range(40):
                await asyncio.sleep(0.5)
                n = len(self.streams)
                if n == last: stable += 1
                else: stable = 0; last = n
                if self.meta.get("duration") and stable >= 6 and n > 0: break
            if not self.meta.get("duration"):
                raise RuntimeError("index returned no duration")
            self.duration = self.meta["duration"]
            self.media = sorted([s for s in self.streams.values() if s.get("streamType") in ("cameraVoip", "screenshare")],
                                key=lambda s: s.get("startTime", 0))

    async def _establish(self, attempts=60):
        """Connect like the browser: mint a FRESH ticket and roll the dice. The gateway
        only registers a connection (loginHandler 'accepted') for the FIRST use of a ticket
        — a reused ticket gets Connect.Success but never 'accepted' and is dead-on-arrival —
        and Connect.Success itself is independently flaky (~20-60% per try). So we re-mint a
        fresh ticket EVERY attempt and retry quickly until one fully registers, then hold it.
        Churn is NOT penalized (the browser opens fresh connections freely), so fast retry is
        correct; the old code's reused tickets were exactly why it 'went silent'."""
        for attempt in range(attempts):
            info = None
            try: info = await asyncio.to_thread(self.info_provider)   # FRESH ticket every attempt
            except Exception: pass
            if info is not None:
                try:
                    await self._open(info); return True             # connect + loginHandler 'accepted' + discover
                except Exception:
                    await self._cleanup()
            if (attempt + 1) % 5 == 0:
                log(f"  …registering (attempt {attempt+1}/{attempts}; fresh ticket each try — gateway connect is flaky)")
            await asyncio.sleep(1.0)
        return False

    def _build_chunks(self):
        """Split each stream's own timeline into chunks (persist; only built once)."""
        # per-stream own length (ms): gap to the next stream of the SAME type, else to the end
        by_type = collections.defaultdict(list)
        for s in self.media: by_type[s["streamType"]].append(s)
        dur_ms = self.duration * 1000.0
        limit_ms = (self.seconds * 1000.0) if self.seconds else None
        cid = 0
        for stype, lst in by_type.items():
            lst.sort(key=lambda s: s.get("startTime", 0))
            for i, s in enumerate(lst):
                start_ms = s.get("startTime", 0)
                end_ms = lst[i + 1].get("startTime", 0) if i + 1 < len(lst) else dur_ms
                own_len = max(0.0, (end_ms - start_ms) / 1000.0)            # seconds of this stream
                if limit_ms is not None:                                    # --seconds: cap to global window
                    if start_ms >= limit_ms: continue
                    own_len = min(own_len, (limit_ms - start_ms) / 1000.0)
                if own_len <= 0: continue
                nch = max(1, math.ceil(own_len / self.chunk_sec))
                piece = own_len / nch
                for j in range(nch):
                    cstart = j * piece
                    self.chunks.append({
                        "id": cid, "stream": s["streamName"], "type": stype,
                        "start": cstart, "len": piece, "global_off": start_ms / 1000.0,
                        "done": False, "frames": [], "nsid": None, "ts_offset": 0,
                    })
                    cid += 1

    async def _dispatch(self, ci, nsid):
        """Start one chunk playing. Returns True on success; False if createNetStream timed
        out — that's just per-stream jitter, so the caller retries the chunk WITHOUT tearing
        down the connection. A genuine send failure (dead socket) raises → caller reconnects."""
        c = self.chunks[ci]
        self.nsid2chunk[nsid] = ci
        try:
            await self._create(nsid)
        except asyncio.TimeoutError:
            self.nsid2chunk.pop(nsid, None)
            return False
        c["nsid"] = nsid
        if c["type"] == "cameraVoip":
            await self._send({"type": "NSFunc", "method": "receiveAudio", "nsId": nsid, "action": True})
        # resume within the chunk from the furthest ts we already captured. The replayed
        # frames arrive with relative ts starting at 0, so set ts_offset = that resume point
        # (in the chunk's own timeline) and the reader re-bases them to continue seamlessly.
        base_ms = c["frames"][-1][1] if c["frames"] else 0
        c["ts_offset"] = base_ms
        base_s = base_ms / 1000.0
        await self._send({"type": "NSFunc", "method": "play", "nsId": nsid, "streamName": c["stream"],
                          "start": c["start"] + base_s, "length": (c["len"] - base_s) + MARGIN,
                          "reset": 1, "mediaAvailable": True})
        return True

    def _progress(self):
        done = sum(1 for c in self.chunks if c["done"])
        mb = sum(sum(len(p) for _, _, p in c["frames"]) for c in self.chunks) / 1e6
        got = sum(((c["frames"][-1][1] / 1000.0) if c["frames"] else 0.0) for c in self.chunks)
        return done, mb, got

    async def run(self):
        if not await self._establish():
            sys.exit("Couldn't connect. Make sure the recording is CLOSED in your browser, then retry.")
        if not self.chunks:
            self._build_chunks()
        total = len(self.chunks)
        content = sum(c["len"] for c in self.chunks)
        log(f"Recording: {self.duration:.0f}s. {len(self.media)} streams → {total} chunks "
            f"({content:.0f} stream-seconds) at parallelism {self.par}.")
        t0 = time.monotonic(); nsid_n = 100; create_fails = 0
        self.last_frame_t = time.monotonic()
        while True:
            if all(c["done"] for c in self.chunks):
                log("All chunks complete."); break
            if self.ws_closed:                              # socket genuinely died → reconnect + resume
                log("  ⚠️ connection lost — reconnecting to resume remaining chunks…")
                await self._cleanup()
                for c in self.chunks:
                    if not c["done"]: c["nsid"] = None
                self.nsid2chunk = {}; create_fails = 0
                if not await self._establish():
                    log("Couldn't reconnect; muxing what we have."); break
                self.last_frame_t = time.monotonic()
                continue
            # fill free slots; a create-timeout just leaves the chunk for next tick (jitter),
            # it does NOT tear down the connection.
            inflight = sum(1 for c in self.chunks if c["nsid"] is not None and not c["done"])
            for c in self.chunks:
                if inflight >= self.par: break
                if c["done"] or c["nsid"] is not None: continue
                nsid_n += 1
                try:
                    ok = await self._dispatch(c["id"], f"nsID_{nsid_n}")
                except Exception:
                    self.ws_closed = True; break            # real send failure → dead socket
                if ok:
                    inflight += 1; create_fails = 0
                else:
                    create_fails += 1
                    if create_fails >= 15:                  # persistent → connection is probably half-dead
                        self.ws_closed = True; break
                await asyncio.sleep(0.3)                     # pace creates — a rapid burst makes the gateway drop us
            await asyncio.sleep(2)
            if self.ws_closed: continue
            if time.monotonic() - self.last_frame_t > 45:   # stalled though socket "open" → recycle it
                log("  stalled 45s — recycling connection…"); self.ws_closed = True; continue
            done, mb, got = self._progress()
            pct = got / max(1.0, content) * 100
            rate = got / max(0.001, time.monotonic() - t0)
            eta = (content - got) / rate if rate > 0 else 0
            log(f"  …{done:2d}/{total} chunks  {pct:4.1f}%  {mb:6.1f}MB  {rate:4.1f}× realtime  ~{eta/60:4.1f} min left")
        await self._cleanup()
        log(f"Download wall time: {(time.monotonic()-t0)/60:.1f} min")

    def write_flvs(self, workdir):
        """Rebuild one FLV per stream from its chunks (chunk frames offset by chunk start)."""
        out = {"video": [], "audio": []}
        by_stream = collections.defaultdict(list)
        for c in self.chunks: by_stream[c["stream"]].append(c)
        for s in self.media:
            name = s["streamName"]
            cs = sorted(by_stream.get(name) or [], key=lambda c: c["start"])
            # Walk chunks in start order; each chunk's frames are relative (0..len), so global
            # ts = chunk.start + ts. Keep only frames BEYOND what an earlier chunk already
            # covered (strictly increasing) — this drops the MARGIN overlap cleanly so the
            # seam region isn't written twice (no doubled audio, no dup video frames).
            tagged = []; last = -1
            for c in cs:
                off = int(round(c["start"] * 1000))
                for typ, ts, pl in sorted(c["frames"], key=lambda x: x[1]):
                    g = off + ts
                    if g <= last: continue
                    tagged.append((g, typ, pl)); last = g
            if not tagged: continue
            is_v = (s["streamType"] == "screenshare")
            fn = acd.re.sub(r"[^A-Za-z0-9_]", "_", name.strip("/"))
            path = os.path.join(workdir, fn + ".flv")
            with open(path, "wb") as f:
                f.write(acd.flv_header(not is_v, is_v))
                for ts, typ, pl in tagged:
                    f.write(acd.flv_tag(9 if is_v else 8, ts, pl))
            out["video" if is_v else "audio"].append({"path": path, "start": s.get("startTime", 0) / 1000.0})
        out["video"].sort(key=lambda r: r["start"]); out["audio"].sort(key=lambda r: r["start"])
        return out


async def amain(args):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cookie = acd.load_cookies(args, script_dir)
    provider = lambda: acd.get_session_info(args.url, cookie)
    info0 = provider()
    log(f"Authorized as: {info0['user']}")
    if info0.get("title"): log(f"Recording: {info0['title']}")
    dl = FastDownloader(provider, par=args.par, chunk_sec=args.chunk, seconds=args.seconds)
    await dl.run()
    workdir = args.workdir or (os.path.splitext(args.output)[0] + "_streams")
    os.makedirs(workdir, exist_ok=True)
    flvs = dl.write_flvs(workdir)
    log(f"Wrote {len(flvs['video'])} video + {len(flvs['audio'])} audio stream file(s).")
    acd.mux(flvs, args.output, dl.duration)
    if not args.keep: shutil.rmtree(workdir, ignore_errors=True)
    log(f"\n✅ Done → {args.output}")


def main():
    ap = argparse.ArgumentParser(description="Download an Adobe Connect recording FAST (parallel chunks).")
    ap.add_argument("url"); ap.add_argument("-o", "--output", default="recording.mp4")
    ap.add_argument("--cookie"); ap.add_argument("--cookies")
    ap.add_argument("--par", type=int, default=12, help="concurrent chunks (default 12)")
    ap.add_argument("--chunk", type=int, default=300, help="seconds of content per chunk (default 300)")
    ap.add_argument("--seconds", type=float, help="only download the first N seconds (for testing)")
    ap.add_argument("--workdir"); ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if not shutil.which("ffmpeg"): sys.exit("ffmpeg not found on PATH.")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
