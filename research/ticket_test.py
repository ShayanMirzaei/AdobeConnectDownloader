#!/usr/bin/env python3
"""Controlled experiment: is the FMS connection ticket single-use?

Both phases open 5 connections (same connection count), so the ONLY difference is
ticket freshness:
  Phase A — mint ONE ticket, reuse it for all 5 attempts.
  Phase B — mint a FRESH ticket for each of 5 attempts (what the browser effectively does).
Each attempt opens a clean WS, connects, createNetStream, plays the index, and counts
success = got the index duration. Connections are hard-closed between attempts.

If B succeeds much more than A -> the ticket is single-use, and the fix is "fresh ticket
per attempt." If A == B (both low) -> it's NOT the ticket (look at connection count etc).
"""
import sys, os, ssl, json, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import acd, websockets

URL = sys.argv[1]
class A: cookie=None; cookies=None
COOKIE = acd.load_cookies(A(), os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def log(*a): print(*a, flush=True)

async def one_attempt(info, label):
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    try:
        ws = await websockets.connect(f"wss://{info['host']}:1443/", ssl=ctx,
                origin=f"https://{info['host']}", additional_headers={"User-Agent": acd.UA},
                compression=None, max_size=None, open_timeout=15, close_timeout=2)
    except Exception as e:
        log(f"  {label}: upgrade fail {type(e).__name__}"); return False
    connected = asyncio.Event(); created = asyncio.Event(); meta = {}
    async def reader():
        try:
            while True:
                m = await ws.recv()
                if isinstance(m,(bytes,bytearray)): continue
                j = json.loads(m); st = j.get("status") or {}
                if st.get("code")=="NetConnection.Connect.Success": connected.set()
                if st.get("description")=="StreamCreated": created.set()
                if j.get("cmdString")=="onMetaData": meta["d"]=j["params"]["arg_0"].get("duration")
        except Exception: pass
    rt = asyncio.create_task(reader())
    async def send(o): await ws.send(json.dumps(o))
    result = False; stage = "connect"
    try:
        await send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
        await send({"type":"NCFunc","method":"connect","url":info["connect_url"],
                    "params":{"ticket":info["ticket"],"reconnection":False,
                              "swfUrl":f"https://{info['host']}/common/meetinghtml/index.html","Recording":True}})
        await asyncio.wait_for(connected.wait(), 6); stage = "createNetStream"
        await send({"type":"NCFunc","method":"createNetStream","nsId":"nsID_0","mediaAvailable":False})
        await asyncio.wait_for(created.wait(), 8); stage = "index"
        await send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
        await send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
        await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":-1,"reset":3,"mediaAvailable":False})
        await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":5,"reset":2,"mediaAvailable":False})
        for _ in range(20):
            await asyncio.sleep(0.5)
            if meta.get("d"): break
        result = bool(meta.get("d"))
        log(f"  {label}: {'OK (dur=%.0f)'%meta['d'] if result else 'fail at '+('index-no-duration' if stage=='index' else stage)}")
    except asyncio.TimeoutError:
        log(f"  {label}: SILENT at {stage}")
    except Exception as e:
        log(f"  {label}: err {type(e).__name__} at {stage}")
    finally:
        rt.cancel()
        try: await asyncio.wait_for(ws.close(), 3)
        except Exception: pass
    return result

async def main():
    log("Phase A — ONE ticket reused 5×:")
    infoA = acd.get_session_info(URL, COOKIE)
    log(f"  (ticket {infoA['ticket'][:10]}… user {infoA['user']})")
    a = 0
    for i in range(5):
        a += await one_attempt(infoA, f"A{i+1} reuse")
        await asyncio.sleep(1)
    log("\nPhase B — FRESH ticket each of 5×:")
    b = 0
    for i in range(5):
        infoB = acd.get_session_info(URL, COOKIE)
        b += await one_attempt(infoB, f"B{i+1} fresh(t={infoB['ticket'][:8]}…)")
        await asyncio.sleep(1)
    log(f"\n==== RESULT: reuse-ticket {a}/5   fresh-ticket {b}/5 ====")
    log("If fresh >> reuse: ticket is single-use → fix = fresh ticket per connection (like the browser).")

asyncio.run(main())
