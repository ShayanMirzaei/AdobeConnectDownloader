#!/usr/bin/env python3
import sys, re, json, ssl, os, time, asyncio, struct
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

async def main():
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    async with websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":UA}, compression=None, max_size=None, open_timeout=25) as ws:
        async def send(o): await ws.send(json.dumps(o))
        await send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
        await send({"type":"NCFunc","method":"connect","url":connect_url,
                    "params":{"ticket":ticket,"reconnection":False,"swfUrl":f"https://{host}/common/meetinghtml/index.html","Recording":True}})
        for i in range(3): await send({"type":"NCFunc","method":"createNetStream","nsId":f"nsID_{i}","mediaAvailable":False})
        await send({"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1})
        await send({"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2})
        await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":-1,"reset":3,"mediaAvailable":False})
        await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":5,"reset":2,"mediaAvailable":False})
        t0=time.monotonic(); nbin=0
        while time.monotonic()-t0<15:
            try: msg=await asyncio.wait_for(ws.recv(),timeout=15-(time.monotonic()-t0))
            except asyncio.TimeoutError: break
            if isinstance(msg,(bytes,bytearray)):
                nbin+=1
                if nbin<=3: log(f"  [bin {len(msg)}] {msg[:20].hex()}")
                continue
            j=json.loads(msg)
            tag=j.get("cmdString") or (j.get("status") or {}).get("code") or j.get("method") or j.get("command")
            log(f"  t+{time.monotonic()-t0:5.2f} {tag}  ::{json.dumps(j)[:120]}")
        log("binary frames:", nbin)
asyncio.run(main())
