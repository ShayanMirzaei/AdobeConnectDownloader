#!/usr/bin/env python3
"""Discovery: connect (no cookie on WS), play indexstream, inventory all streams,
then briefly pull cam+screen to characterize the media frame format."""
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
    if not line or line.startswith("#"): continue
    m=re.match(r"\s*([A-Za-z0-9_]+)\s*[:=]\s*(.+)$",line)
    if m: parts.append(f"{m.group(1)}={m.group(2).strip()}")
COOKIE="; ".join(parts)
def http(u,method="GET"):
    r=urllib.request.Request(u,method=method,headers={"User-Agent":UA,"Cookie":COOKIE,"Origin":f"https://{host}"})
    return urllib.request.urlopen(r,timeout=30,context=nov).read().decode("utf-8","replace")
html=http(URL); dec=urllib.parse.unquote(urllib.parse.unquote(html))
ticket=re.search(r"ticket=([A-Za-z0-9]+)",dec).group(1)
acct,sco=re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output",dec).groups()
connect_url=f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"
log(f"ticket OK; content {acct}/{sco}/output/")

def parse_bin(raw):
    if not raw or raw[0]!=0x03: return None
    ts=struct.unpack(">I",raw[1:5])[0]; nlen=struct.unpack(">I",raw[5:9])[0]
    return ts, raw[9:9+nlen].decode("ascii","replace"), raw[9+nlen:]

async def main():
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    async with websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":UA}, compression=None, max_size=None, open_timeout=25) as ws:
        nid=[0]
        async def send(o): await ws.send(json.dumps(o))
        async def newstream():
            n=f"nsID_{nid[0]}"; nid[0]+=1
            await send({"type":"NCFunc","method":"createNetStream","nsId":n,"mediaAvailable":False}); return n
        async def play(n,name): await send({"type":"NSFunc","method":"play","nsId":n,"streamName":name,"start":0,"length":-1,"reset":3,"mediaAvailable":False})
        await send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
        await send({"type":"NCFunc","method":"connect","url":connect_url,
                    "params":{"ticket":ticket,"reconnection":False,"swfUrl":f"https://{host}/common/meetinghtml/index.html","Recording":True}})
        # replicate browser exactly: 3 netstreams, startLoadEditInfo, preloadStreams, then play index (twice)
        idx=await newstream()            # nsID_0
        await newstream(); await newstream()   # nsID_1, nsID_2
        await send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
        await send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
        await play(idx,"indexstream")
        await send({"type":"NSFunc","method":"play","nsId":idx,"streamName":"indexstream","start":0,"length":5,"reset":2,"mediaAvailable":False})
        log("connected; reading indexstream ...")
        t_start=time.monotonic(); streams={}; duration=None; last_event=time.monotonic()
        nsmap={idx:"indexstream"}; bytes_by=collections.Counter(); frames_by=collections.Counter()
        firstbytes=collections.defaultdict(collections.Counter); sizes=collections.defaultdict(collections.Counter)
        nohdr=0; pulled=False
        while time.monotonic()-t_start < 40:
            try: msg=await asyncio.wait_for(ws.recv(), timeout=6)
            except asyncio.TimeoutError:
                if pulled: break
                else: continue
            if isinstance(msg,(bytes,bytearray)):
                r=parse_bin(msg)
                if r:
                    ts,nsid,pl=r; bytes_by[nsid]+=len(pl); frames_by[nsid]+=1
                    if pl: firstbytes[nsid][pl[0]]+=1; sizes[nsid][len(pl)]+=1
                else: nohdr+=1
                continue
            j=json.loads(msg); cmd=j.get("cmdString")
            if cmd=="onMetaData": duration=j["params"]["arg_0"].get("duration"); log("  duration:",duration)
            if cmd=="playEvent" and isinstance(j["params"].get("arg_2"),list):
                for s in j["params"]["arg_2"]:
                    sn=s.get("streamName")
                    if sn and sn not in streams:
                        streams[sn]=s; last_event=time.monotonic()
                        log(f"  +{sn} ({s.get('streamType')}) @ {s.get('startTime')}s")
            # when we have media streams and indexevents settled, pull one cam + one screen
            media=[s for s in streams.values() if s.get("streamType") in ("cameraVoip","screenshare")]
            if not pulled and len(media)>=1 and (time.monotonic()-last_event)>3:
                pulled=True; log("  -- index settled; pulling sample media for ~10s --")
                picks={}
                for s in media:
                    picks.setdefault(s["streamType"], s)
                for st,s in picks.items():
                    n=await newstream(); nsmap[n]=s["streamName"]; await play(n,s["streamName"])
                pull_until=time.monotonic()+10
                while time.monotonic()<pull_until:
                    try: m2=await asyncio.wait_for(ws.recv(), timeout=pull_until-time.monotonic())
                    except asyncio.TimeoutError: break
                    if isinstance(m2,(bytes,bytearray)):
                        r=parse_bin(m2)
                        if r:
                            ts,nsid,pl=r; bytes_by[nsid]+=len(pl); frames_by[nsid]+=1
                            if pl: firstbytes[nsid][pl[0]]+=1; sizes[nsid][len(pl)]+=1
                        else: nohdr+=1
                break
        log("\n==== INVENTORY ====")
        log(f"duration={duration}  total streams discovered={len(streams)}")
        bytype=collections.Counter(s.get("streamType") for s in streams.values())
        log("by type:", dict(bytype))
        for sn,s in sorted(streams.items(), key=lambda kv: kv[1].get("startTime",0)):
            log(f"   {s.get('streamType'):12} {sn:24} start={s.get('startTime')}")
        log("\n==== MEDIA FRAME CHARACTERIZATION ====")
        for nsid in bytes_by:
            sn=nsmap.get(nsid,nsid)
            fb=dict(firstbytes[nsid].most_common(4)); sz=dict(sizes[nsid].most_common(4))
            log(f"   {sn}: {frames_by[nsid]} frames, {bytes_by[nsid]/1024:.0f}KB, payload[0](hex)={ {hex(k):v for k,v in fb.items()} }, sizes={sz}")
        log("   frames without 0x03 header:", nohdr)
asyncio.run(main())
