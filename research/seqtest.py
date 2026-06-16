#!/usr/bin/env python3
"""Properly-sequenced test: connect -> wait Success -> create -> wait StreamCreated ->
preload -> play index -> collect streams -> play media -> collect bytes."""
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
log("ticket OK")

def parse_bin(raw):
    if not raw or raw[0]!=0x03: return None
    ts=struct.unpack(">I",raw[1:5])[0]; nlen=struct.unpack(">I",raw[5:9])[0]
    return ts, raw[9:9+nlen].decode("ascii","replace"), raw[9+nlen:]

class Client:
    def __init__(self, ws): self.ws=ws; self.connected=asyncio.Event(); self.created={}; self.streams={}
    self_duration=None
    async def send(self,o): await self.ws.send(json.dumps(o))
    async def reader(self, sink):
        try:
            while True:
                msg=await self.ws.recv()
                if isinstance(msg,(bytes,bytearray)):
                    r=parse_bin(msg)
                    if r: sink(r)
                    continue
                j=json.loads(msg); code=(j.get("status") or {}).get("code"); desc=(j.get("status") or {}).get("description")
                if code=="NetConnection.Connect.Success": self.connected.set()
                if desc=="StreamCreated":
                    self.created.setdefault(j.get("nsId"),asyncio.Event()).set()
                cmd=j.get("cmdString")
                if cmd=="onMetaData": self.duration=j["params"]["arg_0"].get("duration")
                if cmd=="playEvent" and isinstance(j["params"].get("arg_2"),list):
                    for s in j["params"]["arg_2"]:
                        if not isinstance(s,dict): continue
                        sn=s.get("streamName")
                        if sn and sn not in self.streams: self.streams[sn]=s
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("  !! reader crashed:", type(e).__name__, e)
    async def create(self, nsid):
        self.created.setdefault(nsid, asyncio.Event())
        await self.send({"type":"NCFunc","method":"createNetStream","nsId":nsid,"mediaAvailable":False})
        await asyncio.wait_for(self.created[nsid].wait(), 10)
    async def play(self, nsid, name, length=-1, reset=3):
        await self.send({"type":"NSFunc","method":"play","nsId":nsid,"streamName":name,"start":0,"length":length,"reset":reset,"mediaAvailable":False})

async def main():
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    bytes_by=collections.Counter(); frames_by=collections.Counter(); firstb=collections.defaultdict(collections.Counter); szs=collections.defaultdict(collections.Counter)
    def sink(r): ts,nsid,pl=r; bytes_by[nsid]+=len(pl); frames_by[nsid]+=1; (pl and (firstb[nsid].update([pl[0]]), szs[nsid].update([len(pl)])))
    async with websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":UA}, compression=None, max_size=None, open_timeout=25) as ws:
        c=Client(ws); c.duration=None
        rt=asyncio.create_task(c.reader(sink))
        await c.send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await c.send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await c.send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
        await c.send({"type":"NCFunc","method":"connect","url":connect_url,
                      "params":{"ticket":ticket,"reconnection":False,"swfUrl":f"https://{host}/common/meetinghtml/index.html","Recording":True}})
        await asyncio.wait_for(c.connected.wait(), 15); log("connect OK")
        await c.create("nsID_0")
        await c.send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
        await c.send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
        await c.play("nsID_0","indexstream")
        log("playing index; collecting stream list for 12s ...")
        await asyncio.sleep(12)
        media=[s for s in c.streams.values() if s.get("streamType") in ("cameraVoip","screenshare")]
        log(f"duration={c.duration}  total streams={len(c.streams)}  media={len(media)}")
        for sn,s in sorted(c.streams.items(), key=lambda kv:kv[1].get('startTime',0)):
            log(f"   {s.get('streamType'):12} {sn:26} @ {s.get('startTime')}")
        # pull one of each media type
        picks={}
        for s in media: picks.setdefault(s["streamType"], s)
        i=10
        for st,s in picks.items():
            nid=f"nsID_{i}"; i+=1; await c.create(nid); await c.play(nid, s["streamName"])
            log(f"   pulling {s['streamName']} as {nid}")
        await asyncio.sleep(10)
        rt.cancel()
        log("\n==== MEDIA FRAMES ====")
        for nsid in bytes_by:
            fb={hex(k):v for k,v in firstb[nsid].most_common(3)}; sz=dict(szs[nsid].most_common(3))
            log(f"   {nsid}: {frames_by[nsid]} frames {bytes_by[nsid]/1024:.0f}KB  payload[0]={fb} sizes={sz}")
asyncio.run(main())
