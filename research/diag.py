#!/usr/bin/env python3
"""Diagnostic: where does connect/discover fail, and how often does it succeed from here?
Re-mints a fresh ticket, then runs N attempts logging the exact stage each one reaches:
  - upgrade ok?  (ws handshake)
  - Connect.Success within 6s?
  - StreamCreated (nsID_0) within 12s?
  - indexstream onMetaData (duration) within 10s?
"""
import sys, os, ssl, json, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import acd, websockets

URL = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 8

class A: cookie=None; cookies=None
cookie = acd.load_cookies(A(), os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def log(*a): print(*a, flush=True)

async def attempt(info, k):
    stage = "upgrade"
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    connected = asyncio.Event(); created = asyncio.Event(); logged_in = asyncio.Event(); meta = {}; t0 = time.monotonic()
    try:
        ws = await websockets.connect(f"wss://{info['host']}:1443/", ssl=ctx,
                origin=f"https://{info['host']}", additional_headers={"User-Agent": acd.UA},
                compression=None, max_size=None, open_timeout=15)
    except Exception as e:
        log(f"[{k}] upgrade FAILED: {type(e).__name__} {e}"); return "upgrade-fail"
    async def reader():
        try:
            while True:
                msg = await ws.recv()
                if isinstance(msg, (bytes, bytearray)): continue
                j = json.loads(msg); st = j.get("status") or {}
                if st.get("code") == "NetConnection.Connect.Success":
                    meta["nc"] = st.get("ncDetails"); connected.set()
                if st.get("description") == "StreamCreated": created.set()
                if j.get("cmdString") == "onMetaData": meta["d"] = j["params"]["arg_0"].get("duration")
                if j.get("method") == "onCommand":
                    a0 = (j.get("params") or {}).get("arg_0")
                    if isinstance(a0, dict) and a0.get("command") == "accepted":
                        meta["login_t"] = time.monotonic() - t0; logged_in.set()
        except Exception: pass
    rt = asyncio.create_task(reader())
    async def send(o): await ws.send(json.dumps(o))
    try:
        await send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
        await send({"type":"NCFunc","method":"connect","url":info["connect_url"],
                    "params":{"ticket":info["ticket"],"reconnection":False,
                              "swfUrl":f"https://{info['host']}/common/meetinghtml/index.html","Recording":True}})
        stage = "connect"
        try: await asyncio.wait_for(connected.wait(), 8)
        except asyncio.TimeoutError:
            log(f"[{k}] CONNECT timeout (no Connect.Success in 8s)  [{time.monotonic()-t0:.1f}s]")
            rt.cancel(); await ws.close(); return "connect-timeout"
        log(f"[{k}] connect OK [{time.monotonic()-t0:.1f}s] nc={meta.get('nc')} → await loginHandler 'accepted'…")
        try: await asyncio.wait_for(logged_in.wait(), 20)
        except asyncio.TimeoutError:
            log(f"[{k}] NO loginHandler 'accepted' in 20s (connected but never accepted)  [{time.monotonic()-t0:.1f}s]")
            rt.cancel(); await ws.close(); return "login-timeout"
        log(f"[{k}] logged in (accepted) [{meta.get('login_t'):.1f}s] → createNetStream…")
        stage = "createNetStream"
        await send({"type":"NCFunc","method":"createNetStream","nsId":"nsID_0","mediaAvailable":False})
        try: await asyncio.wait_for(created.wait(), 12)
        except asyncio.TimeoutError:
            log(f"[{k}] createNetStream timeout (connected but gateway went SILENT)  [{time.monotonic()-t0:.1f}s]")
            rt.cancel(); await ws.close(); return "createstream-timeout"
        log(f"[{k}] StreamCreated OK [{time.monotonic()-t0:.1f}s] → play indexstream…")
        stage = "index"
        await send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
        await send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
        await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":-1,"reset":3,"mediaAvailable":False})
        await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":5,"reset":2,"mediaAvailable":False})
        for _ in range(20):
            await asyncio.sleep(0.5)
            if meta.get("d"): break
        rt.cancel(); await ws.close()
        if meta.get("d"):
            log(f"[{k}] ✅ FULL discover OK: duration={meta['d']:.0f}s  [{time.monotonic()-t0:.1f}s]")
            return "ok"
        log(f"[{k}] index gave no duration  [{time.monotonic()-t0:.1f}s]")
        return "index-fail"
    except Exception as e:
        log(f"[{k}] exception at stage={stage}: {type(e).__name__} {e}")
        try: rt.cancel(); await ws.close()
        except Exception: pass
        return f"exc-{stage}"

async def main():
    log("re-minting a FRESH ticket for every attempt (testing single-use-ticket theory)")
    results = {}
    for k in range(1, N+1):
        info = await asyncio.to_thread(acd.get_session_info, URL, cookie)
        log(f"[{k}] fresh ticket={info['ticket'][:10]}…")
        r = await attempt(info, k)
        results[r] = results.get(r, 0) + 1
    log("\n=== SUMMARY ===")
    for r, c in sorted(results.items(), key=lambda x:-x[1]):
        log(f"  {c:2d} × {r}")

asyncio.run(main())
