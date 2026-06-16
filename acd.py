#!/usr/bin/env python3
"""
acd.py — Adobe Connect recording downloader.

Downloads a recorded Adobe Connect class (screen share + audio) over the
HTML5 player's WebSocket protocol and muxes it into a single MP4 you can
watch (and speed up) in any player.

Usage:
    python3 acd.py "https://HOST/XXXX/?session=YYYY" [-o out.mp4] [options]

Auth: needs your logged-in cookies. Provide via:
    --cookies cookies.txt   (file with lines "Name: value" or "Name=value")
  or --cookie  "BREEZESESSION=...; JSESSIONID=...; BreezeCCookie=..."
  (defaults to ./cookies.txt next to this script)

Requires: ffmpeg on PATH, Python 3.8+ (auto-installs the 'websockets' package).
"""
import sys, re, os, ssl, json, time, struct, asyncio, argparse, subprocess, collections, shutil

# ---------- deps ----------
try:
    import websockets
except ImportError:
    print("Installing 'websockets'…", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "websockets"], check=True)
    import websockets

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
def log(*a): print(*a, flush=True)

# ---------- FLV writing ----------
def flv_header(has_audio, has_video):
    return b"FLV" + bytes([1, (4 if has_audio else 0) | (1 if has_video else 0)]) + struct.pack(">I", 9) + struct.pack(">I", 0)
def flv_tag(tag_type, ts, payload):              # tag_type: 8 audio, 9 video
    sz = len(payload)
    h = bytes([tag_type]) + struct.pack(">I", sz)[1:] + struct.pack(">I", ts & 0xFFFFFF)[1:] + bytes([(ts >> 24) & 0xFF]) + b"\x00\x00\x00"
    return h + payload + struct.pack(">I", 11 + sz)

def parse_bin(raw):
    if not raw or raw[0] not in (3, 4): return None
    typ = raw[0]; ts = struct.unpack(">I", raw[1:5])[0]; nlen = struct.unpack(">I", raw[5:9])[0]
    return typ, ts, raw[9:9+nlen].decode("ascii", "replace"), raw[9+nlen:]

# ---------- cookies ----------
def load_cookies(args, script_dir):
    if args.cookie:
        return args.cookie.strip()
    path = args.cookies or os.path.join(script_dir, "cookies.txt")
    if not os.path.exists(path):
        sys.exit(f"No cookies. Pass --cookie '...' or create {path} (lines like 'BREEZESESSION: ...').")
    parts = []
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"): continue
        m = re.match(r"\s*([A-Za-z0-9_]+)\s*[:=]\s*(.+)$", line)
        if m: parts.append(f"{m.group(1)}={m.group(2).strip()}")
    if not parts: sys.exit(f"{path} has no cookies.")
    return "; ".join(parts)

# ---------- HTTP (ticket) ----------
def http_get(url, cookie, method="GET"):
    import urllib.request
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Cookie": cookie, "Origin": "https://" + url.split("/")[2]})
    return urllib.request.urlopen(req, timeout=30, context=ctx).read().decode("utf-8", "replace")

def get_session_info(url, cookie):
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc
    who = http_get(f"https://{host}/api/xml?action=common-info", cookie)
    m = re.search(r"<name>([^<]*)</name>", who)
    user = m.group(1) if m else None
    html = http_get(url, cookie)
    dec = urllib.parse.unquote(urllib.parse.unquote(html))
    mt = re.search(r"ticket=([A-Za-z0-9]+)", dec)
    ma = re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output", dec)
    if not (mt and ma):
        sys.exit("Couldn't get a ticket — cookies expired or not authorized for this recording.")
    title = None
    mtitle = re.search(r"<title>([^<]*)</title>", html)
    if mtitle: title = mtitle.group(1).strip()
    acct, sco = ma.groups()
    return {"host": host, "ticket": mt.group(1), "acct": acct, "sco": sco, "user": user, "title": title,
            "connect_url": f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"}

# ---------- WebSocket downloader ----------
class Downloader:
    """Downloads all media streams of a recording, surviving WebSocket drops.

    Captured frames are keyed by *stream name* (not the per-connection nsId) so
    that a reconnect resumes the same streams. The gateway is flaky — it may
    refuse `connect`, or accept it then go silent on createNetStream — so every
    (re)connection is retried, and a mid-download drop reconnects and resumes
    each unfinished stream from its last received timestamp.
    """
    def __init__(self, info_provider, max_seconds=None):
        self.info_provider = info_provider      # callable -> fresh info dict (re-mints ticket)
        self.max_seconds = max_seconds
        # persistent across reconnects:
        self.frames = collections.defaultdict(list)   # streamName -> [(type,ts,payload)]
        self.done = set()                              # streamName that reported Play.Stop/Complete
        self.media = None                              # [stream dict] (discovered once)
        self.duration = None
        self.last_frame_t = 0.0

    # ---- per-connection state ----
    def _reset_conn(self):
        self.connected = asyncio.Event(); self.created = {}
        self.streams = {}; self.meta = {}; self.nsid2name = {}; self.ws_closed = False

    async def _reader(self):
        try:
            while True:
                msg = await self.ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    r = parse_bin(msg)
                    if r:
                        typ, ts, nsid, pl = r
                        name = self.nsid2name.get(nsid)
                        if name:
                            self.frames[name].append((typ, ts, pl)); self.last_frame_t = time.monotonic()
                    continue
                j = json.loads(msg); st = j.get("status") or {}; code = st.get("code"); desc = st.get("description")
                if code == "NetConnection.Connect.Success": self.connected.set()
                if desc == "StreamCreated": self.created.setdefault(j.get("nsId"), asyncio.Event()).set()
                if code and ("Play.Stop" in code or "Play.Complete" in code or "Play.UnpublishNotify" in code):
                    name = self.nsid2name.get(j.get("nsId"))
                    if name: self.done.add(name)
                cmd = j.get("cmdString")
                if cmd == "onMetaData": self.meta["duration"] = j["params"]["arg_0"].get("duration")
                if cmd == "playEvent" and isinstance(j["params"].get("arg_2"), list):
                    for s in j["params"]["arg_2"]:
                        if isinstance(s, dict) and s.get("streamName"):
                            self.streams.setdefault(s["streamName"], s)
        except asyncio.CancelledError: raise
        except Exception: self.ws_closed = True

    async def _send(self, o): await self.ws.send(json.dumps(o))
    async def _create(self, nsid):
        # a working gateway answers StreamCreated in ~0.1s; a silent (half-open) one never
        # does — so fail fast (8s) and let the retry loop try a fresh connection.
        self.created.setdefault(nsid, asyncio.Event())
        await self._send({"type": "NCFunc", "method": "createNetStream", "nsId": nsid, "mediaAvailable": False})
        await asyncio.wait_for(self.created[nsid].wait(), 8)

    async def _cleanup(self):
        try: self._rt.cancel()
        except Exception: pass
        try: await self.ws.close()
        except Exception: pass

    async def _open_session(self, info):
        """Open + prime one WS connection (connect, nsID_0, index). On the first
        successful call, discovers `self.media`/`self.duration`. Raises on any failure."""
        self._reset_conn()
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        self.ws = await websockets.connect(f"wss://{info['host']}:1443/", ssl=ctx,
                origin=f"https://{info['host']}", additional_headers={"User-Agent": UA},
                compression=None, max_size=None, open_timeout=20)
        self._rt = asyncio.create_task(self._reader())
        await self._send({"type": "WSFunc", "method": "startHeartbeat", "value": True})
        await self._send({"type": "WSFunc", "method": "allowPacketDrop", "value": False})
        await self._send({"type": "WSFunc", "method": "fragmentVideoPacket", "value": False})
        await self._send({"type": "NCFunc", "method": "connect", "url": info["connect_url"],
                          "params": {"ticket": info["ticket"], "reconnection": False,
                                     "swfUrl": f"https://{info['host']}/common/meetinghtml/index.html", "Recording": True}})
        await asyncio.wait_for(self.connected.wait(), 6)              # raises TimeoutError -> retry
        # prime the index pod (also how streams are discovered the first time)
        await self._create("nsID_0")                                  # raises TimeoutError if gateway silent
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

    async def _establish(self, attempts=30):
        """Retry _open_session until a primed session is live. A *working* gateway answers in
        <1s, but those good windows are brief and intermittent — so we sample DENSELY (light
        0.5s spacing; the 6–8s timeouts already pace us) to catch one, bounded by `attempts`.
        Throttle-avoidance is the bounded cap, NOT wide backoff (wide gaps just miss the good
        windows). If it never answers within the cap, the caller advises waiting (likely
        overload/rate-limit). Re-mints the ticket periodically (per-page-load, can expire)."""
        info = None
        for attempt in range(attempts):
            if attempt % 6 == 0:
                try: info = await asyncio.to_thread(self.info_provider)
                except Exception: pass
            if info is None:
                await asyncio.sleep(2); continue
            try:
                await self._open_session(info); return True
            except Exception:
                await self._cleanup()
                if (attempt + 1) % 6 == 0:
                    log(f"  …still connecting (attempt {attempt+1}/{attempts}; gateway windows are brief)")
                await asyncio.sleep(0.5)
        return False

    def _reached(self):
        return max((self.frames[s["streamName"]][-1][1] for s in self.media if self.frames[s["streamName"]]), default=0) / 1000.0
    def _bytes(self):
        return sum(sum(len(p) for _, _, p in self.frames[s["streamName"]]) for s in self.media)

    async def _play_remaining(self):
        """Create+play every not-yet-finished stream from its resume point."""
        i = 10
        for s in self.media:
            name = s["streamName"]
            if name in self.done: continue
            nsid = f"nsID_{i}"; i += 1
            self.nsid2name[nsid] = name
            await self._create(nsid)
            if s["streamType"] == "cameraVoip":
                await self._send({"type": "NSFunc", "method": "receiveAudio", "nsId": nsid, "action": True})
            resume = (self.frames[name][-1][1] / 1000.0) if self.frames[name] else 0.0
            await self._send({"type": "NSFunc", "method": "play", "nsId": nsid, "streamName": name,
                              "start": resume, "length": -1, "reset": 1, "mediaAvailable": True})

    async def run(self):
        t0 = time.monotonic(); announced = False; stale_cycles = 0
        IDLE = 30
        while True:
            if not await self._establish():
                if not self.frames:
                    sys.exit("The :1443 media gateway never answered (accepts the socket, then silent).\n"
                             "Your login/cookies are fine — this is the gateway itself. Most likely it's\n"
                             "overloaded or temporarily rate-limiting this account after repeated attempts.\n"
                             "Fix: stop, wait ~30–60 min (or try off-peak), then run again. Avoid rapid re-runs.")
                log("Couldn't reconnect; muxing what we have so far."); break
            if not announced:
                log(f"Recording duration: {self.duration:.0f}s ({self.duration/60:.1f} min)")
                log("Media streams: " + ", ".join(f"{s['streamName']}({s['streamType']})" for s in self.media))
                announced = True
            else:
                log(f"  reconnected — resuming from {self._reached():.0f}s "
                    f"({len(self.done)}/{len(self.media)} streams already complete)")

            bytes_before = self._bytes(); finished = False
            # play + collect; ANY mid-session failure (silent createNetStream, dropped send)
            # must fall through to cleanup + reconnect, never crash the program.
            try:
                await self._play_remaining()
                self.last_frame_t = time.monotonic()
                while True:
                    await asyncio.sleep(2)
                    if all(s["streamName"] in self.done for s in self.media):
                        log("All streams finished."); finished = True; break
                    if self.max_seconds and time.monotonic() - t0 > self.max_seconds:
                        log("Reached --max-seconds cap; stopping."); finished = True; break
                    if self.ws_closed:
                        log("⚠️  WebSocket dropped — reconnecting to resume…"); break
                    if time.monotonic() - self.last_frame_t > IDLE:
                        log(f"Idle {IDLE}s with socket open — reconnecting to resume…"); break
                    pct = (self._reached() / self.duration * 100) if self.duration else 0
                    log(f"  …{self._reached():6.0f}s / {self.duration:.0f}s ({pct:4.1f}%)  {self._bytes()/1e6:6.1f} MB  done {len(self.done)}/{len(self.media)}")
            except Exception:
                log("  session error mid-download — reconnecting to resume…")
            await self._cleanup()
            if finished:
                return self.media
            # guard against an endless reconnect loop that makes no progress
            if self._bytes() <= bytes_before:
                stale_cycles += 1
                if stale_cycles >= 4:
                    log("No further data after several reconnects; muxing what we have."); break
            else:
                stale_cycles = 0
        return self.media

# ---------- mux ----------
def write_flvs(dl, workdir):
    out = {"video": [], "audio": []}
    for s in dl.media or []:
        name = s["streamName"]
        fr = dl.frames.get(name) or []
        if not fr: continue
        fr = sorted(fr, key=lambda x: x[1])      # a resume can append overlapping/out-of-order frames
        is_v = (s["streamType"] == "screenshare")
        fn = re.sub(r"[^A-Za-z0-9_]", "_", name.strip("/"))
        path = os.path.join(workdir, fn + ".flv")
        with open(path, "wb") as f:
            f.write(flv_header(not is_v, is_v))
            for typ, ts, pl in fr:
                f.write(flv_tag(9 if is_v else 8, ts, pl))
        rec = {"path": path, "start": s.get("startTime", 0) / 1000.0, "frames": len(fr)}
        out["video" if is_v else "audio"].append(rec)
    out["video"].sort(key=lambda r: r["start"]); out["audio"].sort(key=lambda r: r["start"])
    return out

def mux(flvs, output, duration):
    if not flvs["video"] and not flvs["audio"]:
        sys.exit("No media captured.")
    inputs, fc, idx = [], [], 0
    vlabels, alabels = [], []
    for r in flvs["video"]:
        inputs += ["-i", r["path"]]
        off = int(round(r["start"] * 1000))
        fc.append(f"[{idx}:v]setpts=PTS-STARTPTS+{off}/1000/TB[v{idx}]"); vlabels.append(f"[v{idx}]"); idx += 1
    for r in flvs["audio"]:
        inputs += ["-i", r["path"]]
        off = int(round(r["start"] * 1000))
        fc.append(f"[{idx}:a]adelay={off}|{off}[a{idx}]"); alabels.append(f"[a{idx}]"); idx += 1
    filt = []
    vmap = amap = None
    if vlabels:
        if len(vlabels) == 1: vsrc = vlabels[0]
        else:
            filt.append("".join(vlabels) + f"concat=n={len(vlabels)}:v=1:a=0[vcat]"); vsrc = "[vcat]"
        vmap = "[vout]"; filt.append(f"{vsrc}format=yuv420p[vout]")
    if alabels:
        if len(alabels) == 1: filt.append(f"{alabels[0]}anull[aout]")
        else: filt.append("".join(alabels) + f"amix=inputs={len(alabels)}:normalize=0:dropout_transition=0[aout]")
        amap = "[aout]"
    cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-stats", *inputs,
           "-filter_complex", ";".join(fc + filt)]
    if vmap: cmd += ["-map", vmap, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p"]
    if amap: cmd += ["-map", amap, "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", output]
    log("\nMuxing with ffmpeg…")
    subprocess.run(cmd, check=True)

# ---------- main ----------
async def amain(args):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cookie = load_cookies(args, script_dir)
    provider = lambda: get_session_info(args.url, cookie)   # re-mints a fresh ticket each call
    info0 = provider()                                       # validate creds + log up front
    log(f"Authorized as: {info0['user']}")
    if info0.get("title"): log(f"Recording: {info0['title']}")
    dl = Downloader(provider, max_seconds=args.max_seconds)
    await dl.run()
    workdir = args.workdir or (os.path.splitext(args.output)[0] + "_streams")
    os.makedirs(workdir, exist_ok=True)
    flvs = write_flvs(dl, workdir)
    log(f"Wrote {len(flvs['video'])} video + {len(flvs['audio'])} audio stream file(s).")
    mux(flvs, args.output, dl.duration)
    if not args.keep: shutil.rmtree(workdir, ignore_errors=True)
    log(f"\n✅ Done → {args.output}")

def main():
    ap = argparse.ArgumentParser(description="Download an Adobe Connect recording to MP4.")
    ap.add_argument("url", help="recording URL incl ?session=…")
    ap.add_argument("-o", "--output", default="recording.mp4")
    ap.add_argument("--cookie", help="cookie header string")
    ap.add_argument("--cookies", help="path to cookies file (default ./cookies.txt)")
    ap.add_argument("--max-seconds", type=float, help="stop downloading after N wall-clock seconds (for testing)")
    ap.add_argument("--workdir", help="where to put intermediate .flv files")
    ap.add_argument("--keep", action="store_true", help="keep intermediate .flv files")
    args = ap.parse_args()
    if not shutil.which("ffmpeg"): sys.exit("ffmpeg not found on PATH. Install it first.")
    asyncio.run(amain(args))

if __name__ == "__main__":
    main()
