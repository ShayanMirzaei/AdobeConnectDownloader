#!/usr/bin/env python3
"""Probe a recording: dump the FULL stream inventory (all streamTypes), then play each
stream briefly and report what frame types/codecs actually flow. Answers:
  - does cameraVoip carry webcam VIDEO (type 4) or only audio (type 3)?
  - is a 'whiteboard' a capturable video stream, or a non-media (vector) pod we'd miss?
Usage: python3 research/webcam_probe.py "<url>" [collect_seconds]
"""
import asyncio, json, ssl, sys, struct, collections, time, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import websockets, acd

URL = sys.argv[1]
COLLECT = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
cookie = acd.load_cookies(type("A", (), {"cookie": None, "cookies": None})(), ROOT)
def log(*a): print(*a, flush=True)

VCODEC = {0x12: "h263", 0x14: "vp6", 0x15: "vp6-alpha", 0x16: "screen-v2", 0x17: "h264-kf",
          0x24: "vp6-inter", 0x27: "h264-inter", 0x44: "h264?", 0x47: "h264?"}

async def run():
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(60):
        try:
            info = await asyncio.to_thread(acd.get_session_info, URL, cookie)
        except Exception as e:
            log(f"ticket mint FAILED ({e}). Cookies likely stale — refresh cookies.txt."); return
        connected = asyncio.Event(); logged_in = asyncio.Event()
        created = collections.defaultdict(asyncio.Event)
        streams = {}; meta = {}; frames = collections.defaultdict(list)
        try:
            ws = await websockets.connect(f"wss://{info['host']}:1443/", ssl=ctx,
                    origin=f"https://{info['host']}", additional_headers={"User-Agent": acd.UA},
                    compression=None, max_size=None, open_timeout=15)
        except Exception:
            await asyncio.sleep(1); continue
        async def reader():
            try:
                while True:
                    msg = await ws.recv()
                    if isinstance(msg, (bytes, bytearray)):
                        r = acd.parse_bin(msg)
                        if r: frames[r[2]].append((r[0], r[1], r[3]))
                        continue
                    j = json.loads(msg); st = j.get("status") or {}
                    if st.get("code") == "NetConnection.Connect.Success": connected.set()
                    if j.get("method") == "onCommand":
                        a0 = (j.get("params") or {}).get("arg_0")
                        if isinstance(a0, dict) and a0.get("command") == "accepted": logged_in.set()
                    if st.get("description") == "StreamCreated": created[j.get("nsId")].set()
                    if j.get("cmdString") == "onMetaData": meta["d"] = j["params"]["arg_0"].get("duration")
                    if j.get("cmdString") == "playEvent" and isinstance(j["params"].get("arg_2"), list):
                        for s in j["params"]["arg_2"]:
                            if isinstance(s, dict) and s.get("streamName"): streams.setdefault(s["streamName"], s)
            except Exception: pass
        rt = asyncio.create_task(reader())
        async def send(o): await ws.send(json.dumps(o))
        try:
            await send({"type": "WSFunc", "method": "startHeartbeat", "value": True})
            await send({"type": "WSFunc", "method": "allowPacketDrop", "value": False})
            await send({"type": "WSFunc", "method": "fragmentVideoPacket", "value": False})
            await send({"type": "NCFunc", "method": "connect", "url": info["connect_url"],
                        "params": {"ticket": info["ticket"], "reconnection": False,
                                   "swfUrl": f"https://{info['host']}/common/meetinghtml/index.html", "Recording": True}})
            await asyncio.wait_for(connected.wait(), 5)
            await asyncio.wait_for(logged_in.wait(), 5)
        except asyncio.TimeoutError:
            rt.cancel()
            try: await ws.close()
            except Exception: pass
            log(f"  establish attempt {attempt+1}: connect/accepted timeout — retrying (fresh ticket)…")
            await asyncio.sleep(1); continue
        log(f"ESTABLISHED on attempt {attempt+1}. user={info['user']}")
        # keepalive
        async def hb():
            rid = 1000
            try:
                while True:
                    await asyncio.sleep(5); rid += 1
                    await send({"type": "NCFunc", "method": "call", "method-name": "@getStats", "responderId": rid})
            except Exception: pass
        hbt = asyncio.create_task(hb())
        # discover
        await send({"type": "NCFunc", "method": "createNetStream", "nsId": "nsID_0", "mediaAvailable": False})
        await asyncio.wait_for(created["nsID_0"].wait(), 8)
        await send({"type": "NCFunc", "method": "call", "method-name": "startLoadEditInfo", "params": {}, "responderId": 1})
        await send({"type": "NCFunc", "method": "call", "method-name": "preloadStreams", "responderId": 2})
        await send({"type": "NSFunc", "method": "play", "nsId": "nsID_0", "streamName": "indexstream", "start": 0, "length": -1, "reset": 3, "mediaAvailable": False})
        await send({"type": "NSFunc", "method": "play", "nsId": "nsID_0", "streamName": "indexstream", "start": 0, "length": 5, "reset": 2, "mediaAvailable": False})
        last_n = -1; stable = 0
        for _ in range(40):
            await asyncio.sleep(0.5)
            n = len(streams)
            if n == last_n: stable += 1
            else: stable = 0; last_n = n
            if meta.get("d") and n > 0 and stable >= 6: break
        dur = meta.get("d") or 0
        log(f"\nduration={dur}s   {len(streams)} streams:")
        inv = sorted(streams.values(), key=lambda s: s.get("startTime", 0))
        for s in inv:
            log(f"  {s.get('streamName'):32} type={s.get('streamType'):14} start={s.get('startTime',0)/1000.0:8.1f}s")
        # sample each stream at several points ACROSS its lifetime (the webcam may turn on
        # only partway through, so sampling only the start would miss it).
        log(f"\nsampling each stream at 4 points (~{COLLECT:.0f}s each) across the recording:")
        nsn = 10
        for s in inv:
            nm = s["streamName"]; st0 = s.get("startTime", 0) / 1000.0
            span = max(1.0, dur - st0)
            offsets = [st0 + 5, st0 + 0.25 * span, st0 + 0.5 * span, st0 + 0.75 * span]
            per = []
            for off in offsets:
                nsn += 1; nsid = f"nsID_{nsn}"
                await send({"type": "NCFunc", "method": "createNetStream", "nsId": nsid, "mediaAvailable": False})
                try: await asyncio.wait_for(created[nsid].wait(), 8)
                except asyncio.TimeoutError:
                    per.append(f"@{off:.0f}s:create-timeout"); continue
                await send({"type": "NSFunc", "method": "receiveAudio", "nsId": nsid, "action": True})
                await send({"type": "NSFunc", "method": "receiveVideo", "nsId": nsid, "action": True})
                await send({"type": "NSFunc", "method": "play", "nsId": nsid, "streamName": nm,
                            "start": off, "length": COLLECT + 3, "reset": 1, "mediaAvailable": True})
                await asyncio.sleep(COLLECT)
                fr = frames.get(nsid, [])
                a = sum(1 for t, _, _ in fr if t == 3); v = sum(1 for t, _, _ in fr if t == 4)
                vc = collections.Counter()
                for t, _, pl in fr:
                    if t == 4 and pl: vc[VCODEC.get(pl[0] & 0xFF, hex(pl[0]))] += 1
                per.append(f"@{off:.0f}s: A={a} V={v} {dict(vc) if vc else ''}")
            log(f"  {nm} (type={s.get('streamType')}):")
            for p in per: log(f"       {p}")
        hbt.cancel(); rt.cancel()
        try: await ws.close()
        except Exception: pass
        return
    log("could not establish after 60 attempts")

asyncio.run(run())
