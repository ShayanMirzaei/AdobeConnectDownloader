#!/usr/bin/env python3
"""Find the per-connection concurrent-stream ceiling by ramping N.

For each N in the ramp, open a FRESH connection (with the @getStats keepalive + index
discovery, exactly like acd_fast), then create+play N seeked screenshare chunks ONE AT A
TIME — recording:
  - created_ok / N        (how many createNetStream got StreamCreated; first failure = cap)
  - delivering / N        (how many actually sent media frames in the collect window)
  - dropped               (did the connection die during collection?)
  - first_fail_index      (the chunk index where createNetStream first timed out)
Doesn't abort on a single create timeout — keeps going so we see the true boundary.
"""
import sys, os, ssl, json, asyncio, time, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import acd, websockets

URL = sys.argv[1]
RAMP = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [4, 6, 8, 10, 12, 14]
COLLECT = 12.0
class A: cookie=None; cookies=None
COOKIE = acd.load_cookies(A(), os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def log(*a): print(*a, flush=True)

async def connect_and_discover(info):
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    ws = await websockets.connect(f"wss://{info['host']}:1443/", ssl=ctx, origin=f"https://{info['host']}",
            additional_headers={"User-Agent": acd.UA}, compression=None, max_size=None, open_timeout=15)
    st = {"connected": asyncio.Event(), "created": {}, "streams": {}, "meta": {}, "frames": collections.defaultdict(list),
          "live": set(), "closed": False}
    async def send(o): await ws.send(json.dumps(o))
    async def reader():
        try:
            while True:
                m = await ws.recv()
                if isinstance(m, (bytes, bytearray)):
                    r = acd.parse_bin(m)
                    if r and r[2] in st["live"]: st["frames"][r[2]].append(1)
                    continue
                j = json.loads(m); s = j.get("status") or {}
                if s.get("code") == "NetConnection.Connect.Success": st["connected"].set()
                if s.get("description") == "StreamCreated": st["created"].setdefault(j.get("nsId"), asyncio.Event()).set()
                if j.get("cmdString") == "onMetaData": st["meta"]["d"] = j["params"]["arg_0"].get("duration")
                if j.get("cmdString") == "playEvent" and isinstance(j["params"].get("arg_2"), list):
                    for x in j["params"]["arg_2"]:
                        if isinstance(x, dict) and x.get("streamName"): st["streams"].setdefault(x["streamName"], x)
        except asyncio.CancelledError: raise
        except Exception: st["closed"] = True
    rt = asyncio.create_task(reader())
    async def hb():
        rid = 1000
        try:
            while True:
                await asyncio.sleep(5); rid += 1
                await send({"type":"NCFunc","method":"call","method-name":"@getStats","responderId":rid})
        except asyncio.CancelledError: raise
        except Exception: st["closed"] = True
    async def create(nsid, timeout=6):
        st["created"].setdefault(nsid, asyncio.Event())
        await send({"type":"NCFunc","method":"createNetStream","nsId":nsid,"mediaAvailable":False})
        await asyncio.wait_for(st["created"][nsid].wait(), timeout)
    await send({"type":"WSFunc","method":"startHeartbeat","value":True})
    await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
    await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
    await send({"type":"NCFunc","method":"connect","url":info["connect_url"],
                "params":{"ticket":info["ticket"],"reconnection":False,
                          "swfUrl":f"https://{info['host']}/common/meetinghtml/index.html","Recording":True}})
    await asyncio.wait_for(st["connected"].wait(), 6)
    hbt = asyncio.create_task(hb())
    await create("nsID_0")
    await send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
    await send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
    await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":-1,"reset":3,"mediaAvailable":False})
    await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":5,"reset":2,"mediaAvailable":False})
    for _ in range(40):
        await asyncio.sleep(0.5)
        if st["meta"].get("d") and st["streams"]: break
    return ws, st, send, create, rt, hbt

async def test_par(info, N):
    try:
        ws, st, send, create, rt, hbt = await connect_and_discover(info)
    except Exception as e:
        return f"connect/discover failed ({type(e).__name__})"
    dur = st["meta"].get("d"); ss = [s for s in st["streams"].values() if s.get("streamType") == "screenshare"]
    if not dur or not ss:
        rt.cancel(); hbt.cancel(); await ws.close(); return "no index"
    name = ss[0]["streamName"]
    offsets = [round(i * dur / (N + 1)) for i in range(N)]
    created_ok = 0; first_fail = None
    for i in range(N):
        nsid = f"nsID_{200+i}"
        try:
            await create(nsid, timeout=6); created_ok += 1
            st["live"].add(nsid)
            await send({"type":"NSFunc","method":"play","nsId":nsid,"streamName":name,
                        "start":offsets[i],"length":30,"reset":1,"mediaAvailable":True})
        except asyncio.TimeoutError:
            if first_fail is None: first_fail = i + 1
        except Exception:
            if first_fail is None: first_fail = i + 1
            st["closed"] = True; break
        await asyncio.sleep(0.3)
    await asyncio.sleep(COLLECT)
    delivering = sum(1 for nsid in st["live"] if st["frames"][nsid])
    dropped = st["closed"]
    rt.cancel(); hbt.cancel()
    try: await ws.close()
    except Exception: pass
    return {"created_ok": created_ok, "delivering": delivering, "dropped": dropped, "first_fail": first_fail}

async def main():
    results = []
    for N in RAMP:
        # fresh ticket + connect-retry for the flaky egress
        res = None
        for attempt in range(8):
            info = acd.get_session_info(URL, COOKIE)
            res = await test_par(info, N)
            if isinstance(res, dict): break
            await asyncio.sleep(1)
        if isinstance(res, dict):
            tag = "OK" if (res["created_ok"] == N and res["delivering"] == N and not res["dropped"]) else "⚠️ LIMIT"
            log(f"  N={N:2d}: created {res['created_ok']}/{N}, delivering {res['delivering']}/{N}, "
                f"dropped={res['dropped']}, first_fail_at={res['first_fail']}   {tag}")
            results.append((N, res))
        else:
            log(f"  N={N:2d}: {res} (couldn't establish — egress flakiness, inconclusive)")
        await asyncio.sleep(3)   # let the gateway release streams between tests
    log("\nCeiling = highest N that is fully OK (created==delivering==N, no drop).")

asyncio.run(main())
