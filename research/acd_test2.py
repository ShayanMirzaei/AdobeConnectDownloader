#!/usr/bin/env python3
"""
Adobe Connect gateway handshake tester — tries 4 upgrade variants to find which one the
media gateway accepts. RUN IN YOUR OWN TERMINAL.

    python3 acd_test2.py "https://vc10.sbu.ac.ir/XXXX/?session=YYYY"

Needs cookies.txt next to it (same as before).
"""
import sys, re, json, ssl, os, time, asyncio, subprocess, struct, collections
import urllib.parse, urllib.request
try:
    import websockets
except ImportError:
    subprocess.run([sys.executable,"-m","pip","install","--user","websockets"],check=True); import websockets

URL=sys.argv[1]; HERE=os.path.dirname(os.path.abspath(__file__))
host=urllib.parse.urlparse(URL).netloc
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
def log(*a): print(*a, flush=True)
nov=ssl.create_default_context(); nov.check_hostname=False; nov.verify_mode=ssl.CERT_NONE

# cookies — prefer the repo-root cookies.txt (the one acd.py uses) so there's ONE file to refresh
CKPATH=next((p for p in (os.path.join(HERE,"..","cookies.txt"), os.path.join(HERE,"cookies.txt")) if os.path.exists(p)),
            os.path.join(HERE,"cookies.txt"))
parts=[]
for line in open(CKPATH):
    line=line.strip()
    if not line or line.startswith("#"): continue
    m=re.match(r"\s*([A-Za-z0-9_]+)\s*[:=]\s*(.+)$",line)
    if m: parts.append(f"{m.group(1)}={m.group(2).strip()}")
COOKIE="; ".join(parts)

def http(url, method="GET"):
    r=urllib.request.Request(url,method=method,headers={"User-Agent":UA,"Cookie":COOKIE,"Origin":f"https://{host}"})
    return urllib.request.urlopen(r,timeout=30,context=nov).read().decode("utf-8","replace")

def get_ticket():
    html=http(URL); dec=urllib.parse.unquote(urllib.parse.unquote(html))
    mt=re.search(r"ticket=([A-Za-z0-9]+)",dec); ma=re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output",dec)
    if not (mt and ma):
        raise SystemExit("No ticket in the recording page — cookies/session expired. "
                         "Refresh cookies.txt from the browser and use a fresh ?session= URL.")
    a,s=ma.groups()
    return mt.group(1), f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{a}/{s}/output/"

async def try_variant(deflate, send_cookie):
    ticket, connect_url = get_ticket()
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    hdrs={"User-Agent":UA,"Cache-Control":"no-cache","Pragma":"no-cache","Accept-Language":"en-US,en;q=0.9"}
    if send_cookie: hdrs["Cookie"]=COOKIE
    kw=dict(ssl=ctx, origin=f"https://{host}", additional_headers=hdrs, max_size=None, open_timeout=20)
    kw["compression"] = "deflate" if deflate else None
    try:
        async with websockets.connect(f"wss://{host}:1443/", **kw) as ws:
            ext = ws.protocol.extensions if hasattr(ws,"protocol") else []
            async def send(o): await ws.send(json.dumps(o))
            await send({"type":"WSFunc","method":"startHeartbeat","value":True})
            await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
            await send({"type":"WSFunc","method":"fragmentVideoPacket","value":True})
            await send({"type":"NCFunc","method":"connect","url":connect_url,
                        "params":{"ticket":ticket,"reconnection":False,
                                  "swfUrl":f"https://{host}/common/meetinghtml/index.html?timestamp=1781474187247&view=mobileHtml","Recording":True}})
            try:
                while True:
                    msg=await asyncio.wait_for(ws.recv(), timeout=6)
                    if isinstance(msg,str):
                        j=json.loads(msg)
                        if (j.get("status") or {}).get("code")=="NetConnection.Connect.Success":
                            return True, f"ext={ext}"
            except asyncio.TimeoutError:
                return False, f"silent (ext={ext})"
    except Exception as e:
        return False, f"error {type(e).__name__}: {e}"

async def main():
    who = http(f"https://{host}/api/xml?action=common-info")
    m = re.search(r"<name>([^<]*)</name>", who)
    if m:
        log("auth:", m.group(1))
    else:
        log("auth: ⚠️  NO <name> in common-info — your session/cookies are NOT recognized "
            "(likely expired). Refresh cookies.txt. Server returned:")
        log("   " + " ".join(who.split())[:500])
        return
    variants=[("deflate=ON  cookie=OFF (exact browser)",True,False),
              ("deflate=ON  cookie=ON ",True,True),
              ("deflate=OFF cookie=OFF",False,False),
              ("deflate=OFF cookie=ON  (old test)",False,True)]
    winner=None
    for name,d,c in variants:
        ok,info=await try_variant(d,c)
        log(f"  [{'✅' if ok else '❌'}] {name}  -> {info}")
        if ok and not winner: winner=(name,d,c)
    log("\nWINNER:", winner[0] if winner else "none — still silent on all variants")
asyncio.run(main())
