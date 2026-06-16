#!/usr/bin/env python3
import sys, re, json, ssl, os, time, asyncio, struct, collections
import urllib.parse, urllib.request, websockets
URL=sys.argv[1]; HERE=os.path.dirname(os.path.abspath(__file__))
host=urllib.parse.urlparse(URL).netloc
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
def log(*a): print(*a, flush=True)
nov=ssl.create_default_context(); nov.check_hostname=False; nov.verify_mode=ssl.CERT_NONE
parts=[]
for line in open(os.path.join(HERE,"cookies.txt")):
    line=line.strip()
    if line and not line.startswith("#"):
        m=re.match(r"\s*([A-Za-z0-9_]+)\s*[:=]\s*(.+)$",line)
        if m: parts.append(f"{m.group(1)}={m.group(2).strip()}")
COOKIE="; ".join(parts)
def http(u):
    return urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":UA,"Cookie":COOKIE,"Origin":f"https://{host}"}),timeout=30,context=nov).read().decode("utf-8","replace")
dec=urllib.parse.unquote(urllib.parse.unquote(http(URL)))
ticket=re.search(r"ticket=([A-Za-z0-9]+)",dec).group(1)
acct,sco=re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output",dec).groups()
connect_url=f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"

def parse_bin(raw):
    if not raw or raw[0] not in (3,4): return None
    typ=raw[0]; ts=struct.unpack(">I",raw[1:5])[0]; nlen=struct.unpack(">I",raw[5:9])[0]
    return typ, ts, raw[9:9+nlen].decode("ascii","replace"), raw[9+nlen:]

async def attempt(sink):
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    ws=await websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":UA}, compression=None, max_size=None, open_timeout=20)
    connected=asyncio.Event(); created={}; streams={}; meta={}
    statuses=[]
    async def send(o): await ws.send(json.dumps(o))
    async def reader():
        try:
            while True:
                msg=await ws.recv()
                if isinstance(msg,(bytes,bytearray)):
                    sink_raw(msg)
                    r=parse_bin(msg);  sink(r) if r else None; continue
                j=json.loads(msg); st=j.get("status") or {}
                code=st.get("code"); desc=st.get("description")
                if code=="NetConnection.Connect.Success": connected.set()
                if desc=="StreamCreated": created.setdefault(j.get("nsId"),asyncio.Event()).set()
                if code and code!="NetConnection.Connect.Success": statuses.append((j.get("nsId"),code))
                cmd=j.get("cmdString")
                if cmd=="onMetaData": meta["duration"]=j["params"]["arg_0"].get("duration")
                if cmd=="playEvent" and isinstance(j["params"].get("arg_2"),list):
                    for s in j["params"]["arg_2"]:
                        if isinstance(s,dict) and s.get("streamName") and s["streamName"] not in streams:
                            streams[s["streamName"]]=s
        except asyncio.CancelledError: raise
        except Exception as e: log("  reader exit:", type(e).__name__)
    nonlocal_raw={"n":0,"bytes":0,"heads":[]}
    def sink_raw(msg):
        nonlocal_raw["n"]+=1; nonlocal_raw["bytes"]+=len(msg)
        if len(nonlocal_raw["heads"])<6: nonlocal_raw["heads"].append((len(msg), bytes(msg[:16]).hex()))
    rt=asyncio.create_task(reader())
    async def create(nsid):
        created.setdefault(nsid,asyncio.Event())
        await send({"type":"NCFunc","method":"createNetStream","nsId":nsid,"mediaAvailable":False})
        await asyncio.wait_for(created[nsid].wait(),10)
    async def play(nsid,name,length=-1,reset=3):
        await send({"type":"NSFunc","method":"play","nsId":nsid,"streamName":name,"start":0,"length":length,"reset":reset,"mediaAvailable":False})
    await send({"type":"WSFunc","method":"startHeartbeat","value":True})
    await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
    await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
    await send({"type":"NCFunc","method":"connect","url":connect_url,
                "params":{"ticket":ticket,"reconnection":False,"swfUrl":f"https://{host}/common/meetinghtml/index.html","Recording":True}})
    try: await asyncio.wait_for(connected.wait(),6)
    except asyncio.TimeoutError:
        rt.cancel(); await ws.close(); return None
    log("connect OK")
    await create("nsID_0")
    await send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
    await send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
    await play("nsID_0","indexstream"); await play("nsID_0","indexstream",length=5,reset=2)
    await asyncio.sleep(10)
    if meta.get("duration") is None:
        log("   index did not deliver; retrying whole attempt"); rt.cancel(); await ws.close(); return None
    media=[s for s in streams.values() if s.get("streamType") in ("cameraVoip","screenshare")]
    log(f"duration={meta.get('duration')} streams={len(streams)} media={len(media)}")
    for sn,s in sorted(streams.items(),key=lambda kv:kv[1].get('startTime',0)):
        log(f"   {s.get('streamType'):12} {sn:26} @ {s.get('startTime')}")
    flt=os.environ.get("STREAM","all")
    i=10
    for s in media:
        st=s.get("streamType")
        if flt=="screen" and st!="screenshare": continue
        if flt=="audio" and st!="cameraVoip": continue
        nid=f"nsID_{i}"; i+=1
        await create(nid)
        if st=="cameraVoip":
            await send({"type":"NSFunc","method":"receiveAudio","nsId":nid,"action":True})
        start=s.get("startTime",0)/1000.0
        await send({"type":"NSFunc","method":"play","nsId":nid,"streamName":s["streamName"],"start":start,"length":-1,"reset":1,"mediaAvailable":True})
        log(f"   playing {s['streamName']} as {nid} (start={start})")
    await asyncio.sleep(15)
    log("   play statuses:", statuses[-8:])
    log(f"   RAW binary frames: {nonlocal_raw['n']}  ({nonlocal_raw['bytes']/1024:.0f}KB)")
    log("   raw heads:")
    for ln,hx in nonlocal_raw["heads"]: log(f"     len={ln} {hx}")
    rt.cancel(); await ws.close(); return True

async def main():
    bytes_by=collections.Counter(); frames_by=collections.Counter(); firstb=collections.defaultdict(collections.Counter); szs=collections.defaultdict(collections.Counter)
    payload_samples=collections.defaultdict(list); type_by=collections.defaultdict(collections.Counter)
    ts_min={}; ts_max={}; ts_list=collections.defaultdict(list)
    def sink(r):
        typ,ts,nsid,pl=r; bytes_by[nsid]+=len(pl); frames_by[nsid]+=1; type_by[nsid][typ]+=1
        ts_min[nsid]=min(ts_min.get(nsid,ts),ts); ts_max[nsid]=max(ts_max.get(nsid,ts),ts)
        if len(ts_list[nsid])<12: ts_list[nsid].append(ts)
        if pl:
            firstb[nsid][pl[0]]+=1; szs[nsid][len(pl)]+=1
            if len(payload_samples[(nsid,typ)])<3: payload_samples[(nsid,typ)].append((ts,len(pl),pl[:10].hex()))
    for k in range(6):
        log(f"-- connect attempt {k+1} --")
        try: res=await attempt(sink)
        except Exception as e: log("  attempt error:", type(e).__name__, e); res=None
        if res: break
    log("\n==== MEDIA FRAMES ====")
    for nsid in bytes_by:
        fb={hex(x):v for x,v in firstb[nsid].most_common(4)}
        log(f"   {nsid}: {frames_by[nsid]} frames {bytes_by[nsid]/1024:.0f}KB types={dict(type_by[nsid])} ts[min={ts_min.get(nsid)},max={ts_max.get(nsid)}] firstTs={ts_list[nsid]} payload[0]={fb}")
    log("   payload samples (nsid,type)->[(ts,len,payload[:10])]:")
    for k,v in payload_samples.items(): log(f"     {k}: {v}")
    if not bytes_by: log("   (none)")
asyncio.run(main())
