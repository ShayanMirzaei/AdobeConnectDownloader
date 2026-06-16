#!/usr/bin/env python3
"""Download a short slice of screenshare(video)+cameraVoip(audio), write real FLV files,
so we can validate with ffprobe/ffmpeg."""
import sys, re, json, ssl, os, time, asyncio, struct, collections
import urllib.parse, urllib.request, websockets
URL=sys.argv[1]; SECS=float(sys.argv[2]) if len(sys.argv)>2 else 25
HERE=os.path.dirname(os.path.abspath(__file__)); host=urllib.parse.urlparse(URL).netloc
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
    return raw[0], struct.unpack(">I",raw[1:5])[0], struct.unpack(">I",raw[5:9])[0], raw

def flv_header(has_a,has_v):
    return b"FLV"+bytes([1,(4 if has_a else 0)|(1 if has_v else 0)])+struct.pack(">I",9)+struct.pack(">I",0)
def flv_tag(ttype,ts,payload):
    sz=len(payload)
    h=bytes([ttype])+struct.pack(">I",sz)[1:]+struct.pack(">I",ts&0xFFFFFF)[1:]+bytes([(ts>>24)&0xFF])+b"\x00\x00\x00"
    return h+payload+struct.pack(">I",11+sz)

async def attempt():
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    ws=await websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":UA}, compression=None, max_size=None, open_timeout=20)
    connected=asyncio.Event(); created={}; streams={}; meta={}
    frames=collections.defaultdict(list)   # nsid -> [(ttype,ts,payload)]
    nsinfo={}                               # nsid -> stream dict
    async def send(o): await ws.send(json.dumps(o))
    async def reader():
        try:
            while True:
                msg=await ws.recv()
                if isinstance(msg,(bytes,bytearray)):
                    r=parse_bin(msg)
                    if r:
                        ttype,ts,nlen,raw=r; nsid=raw[9:9+nlen].decode("ascii","replace"); pl=raw[9+nlen:]
                        if nsid in nsinfo: frames[nsid].append((ttype,ts,pl))
                    continue
                j=json.loads(msg); st=j.get("status") or {}
                if st.get("code")=="NetConnection.Connect.Success": connected.set()
                if st.get("description")=="StreamCreated": created.setdefault(j.get("nsId"),asyncio.Event()).set()
                cmd=j.get("cmdString")
                if cmd=="onMetaData": meta["duration"]=j["params"]["arg_0"].get("duration")
                if cmd=="playEvent" and isinstance(j["params"].get("arg_2"),list):
                    for s in j["params"]["arg_2"]:
                        if isinstance(s,dict) and s.get("streamName"): streams.setdefault(s["streamName"],s)
        except asyncio.CancelledError: raise
        except Exception as e: log("reader exit",type(e).__name__)
    rt=asyncio.create_task(reader())
    async def create(nsid):
        created.setdefault(nsid,asyncio.Event())
        await send({"type":"NCFunc","method":"createNetStream","nsId":nsid,"mediaAvailable":False})
        await asyncio.wait_for(created[nsid].wait(),10)
    await send({"type":"WSFunc","method":"startHeartbeat","value":True})
    await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
    await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
    await send({"type":"NCFunc","method":"connect","url":connect_url,
                "params":{"ticket":ticket,"reconnection":False,"swfUrl":f"https://{host}/common/meetinghtml/index.html","Recording":True}})
    try: await asyncio.wait_for(connected.wait(),6)
    except asyncio.TimeoutError: rt.cancel(); await ws.close(); return None
    await create("nsID_0")
    await send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
    await send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
    await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":-1,"reset":3,"mediaAvailable":False})
    await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":5,"reset":2,"mediaAvailable":False})
    await asyncio.sleep(8)
    if meta.get("duration") is None: rt.cancel(); await ws.close(); return None
    media=[s for s in streams.values() if s.get("streamType") in ("cameraVoip","screenshare")]
    log("duration",meta["duration"],"media streams:",[(s["streamName"],s["streamType"]) for s in media])
    # pick first screenshare + first cameraVoip
    pick=[]
    for want in ("screenshare","cameraVoip"):
        for s in media:
            if s["streamType"]==want: pick.append(s); break
    i=10
    for s in pick:
        nid=f"nsID_{i}"; i+=1; nsinfo[nid]=s; await create(nid)
        if s["streamType"]=="cameraVoip": await send({"type":"NSFunc","method":"receiveAudio","nsId":nid,"action":True})
        await send({"type":"NSFunc","method":"play","nsId":nid,"streamName":s["streamName"],"start":s.get("startTime",0)/1000.0,"length":-1,"reset":1,"mediaAvailable":True})
        log("playing",s["streamName"],"as",nid)
    await asyncio.sleep(SECS)
    rt.cancel(); await ws.close()
    # write FLVs
    out={}
    for nid,s in nsinfo.items():
        fr=frames[nid]
        if not fr: log("  no frames for",s["streamName"]); continue
        is_v=(s["streamType"]=="screenshare"); ttype=9 if is_v else 8
        path=os.path.join(HERE, ("screenshare.flv" if is_v else "camera0.flv"))
        with open(path,"wb") as f:
            f.write(flv_header(not is_v, is_v))
            for _,ts,pl in fr: f.write(flv_tag(ttype,ts,pl))
        out[path]=(len(fr), fr[-1][1])
        log(f"  wrote {path}: {len(fr)} tags, last ts={fr[-1][1]}ms, {os.path.getsize(path)/1024:.0f}KB")
    return out

async def main():
    for k in range(8):
        try: res=await attempt()
        except Exception as e: log("attempt err",type(e).__name__,e); res=None
        if res: break
        log(f"(retry {k+1})")
asyncio.run(main())
