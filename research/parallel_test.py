#!/usr/bin/env python3
"""Feasibility test for PARALLEL chunked download.

On ONE connection, open N netstreams of the screenshare seeked to different time
offsets and play a short slice of each concurrently. Answers:
  (1) does the gateway serve multiple concurrent seeked streams on one connection?
  (2) does start:N actually seek mid-stream?
  (3) absolute vs relative timestamps on seeked frames?
  (4) does each chunk start on a video keyframe (0x14)?
  (5) aggregate throughput vs ~1× single stream.
"""
import sys, os, ssl, json, asyncio, time, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import acd, websockets

URL = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 4
COLLECT = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
LEN = int(sys.argv[4]) if len(sys.argv) > 4 else 30  # seconds requested per chunk
class A: cookie=None; cookies=None
COOKIE = acd.load_cookies(A(), os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def log(*a): print(*a, flush=True)

async def main():
    info = acd.get_session_info(URL, COOKIE)
    log(f"user={info['user']} ticket={info['ticket'][:10]}…")
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    ws = await websockets.connect(f"wss://{info['host']}:1443/", ssl=ctx, origin=f"https://{info['host']}",
            additional_headers={"User-Agent": acd.UA}, compression=None, max_size=None, open_timeout=20)
    connected=asyncio.Event(); created={}; streams={}; meta={}
    frames=collections.defaultdict(list)   # nsid -> [(typ,ts,len)]
    nsids_live=set()
    async def send(o): await ws.send(json.dumps(o))
    async def reader():
        try:
            while True:
                m=await ws.recv()
                if isinstance(m,(bytes,bytearray)):
                    r=acd.parse_bin(m)
                    if r:
                        typ,ts,nsid,pl=r
                        if nsid in nsids_live: frames[nsid].append((typ,ts,len(pl),pl[0] if pl else None))
                    continue
                j=json.loads(m); st=j.get("status") or {}
                if st.get("code")=="NetConnection.Connect.Success": connected.set()
                if st.get("description")=="StreamCreated": created.setdefault(j.get("nsId"),asyncio.Event()).set()
                if st.get("code"):
                    # surface play status per nsid
                    if "Play" in (st.get("code") or ""): log(f"   onStatus {j.get('nsId')}: {st.get('code')}")
                if j.get("cmdString")=="onMetaData": meta["d"]=j["params"]["arg_0"].get("duration")
                if j.get("cmdString")=="playEvent" and isinstance(j["params"].get("arg_2"),list):
                    for s in j["params"]["arg_2"]:
                        if isinstance(s,dict) and s.get("streamName"): streams.setdefault(s["streamName"],s)
        except Exception: pass
    rt=asyncio.create_task(reader())
    async def create(nsid):
        created.setdefault(nsid,asyncio.Event())
        await send({"type":"NCFunc","method":"createNetStream","nsId":nsid,"mediaAvailable":False})
        await asyncio.wait_for(created[nsid].wait(),8)
    # connect + discover
    await send({"type":"WSFunc","method":"startHeartbeat","value":True})
    await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
    await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
    await send({"type":"NCFunc","method":"connect","url":info["connect_url"],
                "params":{"ticket":info["ticket"],"reconnection":False,
                          "swfUrl":f"https://{info['host']}/common/meetinghtml/index.html","Recording":True}})
    await asyncio.wait_for(connected.wait(),6)
    await create("nsID_0")
    await send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
    await send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
    await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":-1,"reset":3,"mediaAvailable":False})
    await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":5,"reset":2,"mediaAvailable":False})
    for _ in range(40):
        await asyncio.sleep(0.5)
        if meta.get("d"): break
    dur=meta.get("d")
    ss=[s for s in streams.values() if s.get("streamType")=="screenshare"]
    if not dur or not ss:
        log("discover failed (dur=%s, screenshare=%s)"%(dur,bool(ss))); rt.cancel(); await ws.close(); return
    name=ss[0]["streamName"]
    log(f"duration={dur:.0f}s  screenshare={name}")
    offsets=[round(i*dur/N) for i in range(N)]
    log(f"opening {N} chunks at offsets {offsets} (each length={LEN}s) on ONE connection…")
    t0=time.monotonic()
    for i,off in enumerate(offsets):
        nsid=f"nsID_{20+i}"; nsids_live.add(nsid)
        await create(nsid)
        await send({"type":"NSFunc","method":"play","nsId":nsid,"streamName":name,
                    "start":off,"length":LEN,"reset":1,"mediaAvailable":True})
    await asyncio.sleep(COLLECT)
    rt.cancel(); await ws.close()
    log(f"\n=== after {COLLECT:.0f}s wall ===")
    total=0; concurrent=0
    for i,off in enumerate(offsets):
        nsid=f"nsID_{20+i}"; fr=frames[nsid]
        if fr:
            concurrent+=1
            tss=[f[1] for f in fr]; byts=sum(f[2] for f in fr)
            kf=fr[0][3]
            total+=byts
            log(f"  chunk{i} start={off:5d}s: {len(fr):4d} frames, ts {min(tss)/1000:.1f}..{max(tss)/1000:.1f}s, "
                f"{byts/1e6:.2f}MB, firstByte={kf:#04x} ({'KEYFRAME' if kf==0x14 else 'not-kf'})")
        else:
            log(f"  chunk{i} start={off:5d}s: NO FRAMES")
    log(f"\n{concurrent}/{N} chunks delivered concurrently; aggregate {total/1e6:.2f}MB in {COLLECT:.0f}s "
        f"= {total/1e6/COLLECT:.2f}MB/s")
    log("ABSOLUTE timestamps if each chunk's ts≈its start offset; RELATIVE if all start near 0.")

asyncio.run(main())
