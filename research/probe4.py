#!/usr/bin/env python3
import sys, re, json, ssl, asyncio, os, urllib.parse, urllib.request
import websockets
URL=sys.argv[1]; COOKIE=os.environ["ACD_COOKIE"]
UA="Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
p=urllib.parse.urlparse(URL); host=p.netloc
def log(*a): print(*a, flush=True)
nov=ssl.create_default_context(); nov.check_hostname=False; nov.verify_mode=ssl.CERT_NONE
def http(url, method="GET"):
    r=urllib.request.Request(url, method=method, headers={"User-Agent":UA,"Cookie":COOKIE,"Origin":f"https://{host}"})
    return urllib.request.urlopen(r,timeout=30,context=nov).read().decode("utf-8","replace")
# replicate pre-sequence
http(f"https://{host}/api/xml?action=sco-info&sco-id=1225819", "POST")
html=http(URL)
http(f"https://{host}/api/xml?action=acts-location","POST")
dec=urllib.parse.unquote(urllib.parse.unquote(html))
ticket=re.search(r"ticket=([A-Za-z0-9]+)",dec).group(1)
acct,sco=re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output",dec).groups()
connect_url=f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"
log("ticket=",ticket)

async def main():
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    async with websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":UA,"Cookie":COOKIE,"Cache-Control":"no-cache","Pragma":"no-cache","Accept-Language":"en-US,en;q=0.9"},
            compression=None, max_size=None, open_timeout=30) as ws:
        log("connected ws; server=", ws.response.headers.get("server"))
        # burst all setup+connect with no awaits between
        msgs=[{"type":"WSFunc","method":"startHeartbeat","value":True},
              {"type":"WSFunc","method":"allowPacketDrop","value":False},
              {"type":"WSFunc","method":"fragmentVideoPacket","value":True},
              {"type":"NCFunc","method":"connect","url":connect_url,
               "params":{"ticket":ticket,"reconnection":False,"swfUrl":f"https://{host}/common/meetinghtml/index.html?timestamp=1781474187247&view=mobileHtml","Recording":True}}]
        await asyncio.gather(*[ws.send(json.dumps(m)) for m in msgs])
        log("sent connect burst")
        async def heart():
            while True:
                await asyncio.sleep(3)
                try: await ws.send(json.dumps({"type":"WSFunc","method":"heartbeat"}))
                except: return
        ht=asyncio.create_task(heart())
        got=0
        try:
            while got<6:
                msg=await asyncio.wait_for(ws.recv(), timeout=20)
                got+=1
                log("  <<", (msg[:160] if isinstance(msg,str) else f"[bin {len(msg)}]"))
        except asyncio.TimeoutError: log("  TIMEOUT no message in 20s")
        except websockets.ConnectionClosed as e: log("  CLOSED", e.code, repr(e.reason))
        ht.cancel()
    log("messages:", got)
asyncio.run(main())
