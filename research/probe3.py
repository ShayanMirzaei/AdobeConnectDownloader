#!/usr/bin/env python3
import sys, re, json, ssl, asyncio, os, urllib.parse, urllib.request
import websockets
URL=sys.argv[1]; COOKIE=os.environ["ACD_COOKIE"]
p=urllib.parse.urlparse(URL); host=p.netloc
def log(*a): print(*a, flush=True)
nov=ssl.create_default_context(); nov.check_hostname=False; nov.verify_mode=ssl.CERT_NONE
html=urllib.request.urlopen(urllib.request.Request(URL,headers={"User-Agent":"Mozilla/5.0","Cookie":COOKIE}),timeout=30,context=nov).read().decode("utf-8","replace")
dec=urllib.parse.unquote(urllib.parse.unquote(html))
ticket=re.search(r"ticket=([A-Za-z0-9]+)",dec).group(1)
acct,sco=re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output",dec).groups()
connect_url=f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"
log("ticket=",ticket)

async def main():
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    async with websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":"Mozilla/5.0","Cookie":COOKIE,
                                "Cache-Control":"no-cache","Pragma":"no-cache",
                                "Accept-Language":"en-US,en;q=0.9"},
            compression=None, max_size=None, open_timeout=30) as ws:
        log("negotiated extensions:", ws.protocol.extensions if hasattr(ws,'protocol') else 'n/a')
        log("server:", ws.response.headers.get("server"))
        async def send(o): await ws.send(json.dumps(o)); log("  >>", json.dumps(o)[:70])
        await send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await send({"type":"WSFunc","method":"fragmentVideoPacket","value":True})
        await send({"type":"NCFunc","method":"connect","url":connect_url,
                    "params":{"ticket":ticket,"reconnection":False,
                              "swfUrl":f"https://{host}/common/meetinghtml/index.html?timestamp=1781474187247&view=mobileHtml","Recording":True}})
        got=0
        try:
            while got<10:
                msg=await asyncio.wait_for(ws.recv(), timeout=8)
                got+=1
                if isinstance(msg,(bytes,bytearray)): log("  << [bin]", len(msg))
                else: log("  <<", msg[:160])
        except asyncio.TimeoutError:
            log("  (timeout, no message)")
        except websockets.ConnectionClosed as e:
            log("  CLOSED code=",e.code," reason=",repr(e.reason))
    log("done, messages received:", got)
asyncio.run(main())
