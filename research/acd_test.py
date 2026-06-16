#!/usr/bin/env python3
"""
Adobe Connect media-stream connectivity test.
RUN THIS IN YOUR OWN TERMINAL (not through Claude / not with the `!` prefix),
so it uses the same network as your browser.

Usage:
    python3 acd_test.py "https://vc10.sbu.ac.ir/XXXX/?session=YYYY"

Cookies: put them in a file named  cookies.txt  next to this script, e.g.
    BreezeCCookie: conn-....
    BREEZESESSION: breez....
    JSESSIONID: ....
(copy them from DevTools -> Application/Storage -> Cookies)
"""
import sys, re, json, ssl, os, struct, time, asyncio, subprocess, collections
import urllib.parse, urllib.request

# --- ensure 'websockets' is installed ---
try:
    import websockets
except ImportError:
    print("Installing 'websockets' ...", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "websockets"], check=True)
    import websockets

if len(sys.argv) < 2:
    print(__doc__); sys.exit(1)
URL = sys.argv[1]
HERE = os.path.dirname(os.path.abspath(__file__))

# --- load cookies ---
ck_path = os.path.join(HERE, "cookies.txt")
cookie_header = ""
if os.path.exists(ck_path):
    parts = []
    for line in open(ck_path):
        line = line.strip()
        if not line or line.startswith("#"): continue
        m = re.match(r"\s*([A-Za-z0-9_]+)\s*[:=]\s*(.+)$", line)
        if m: parts.append(f"{m.group(1)}={m.group(2).strip()}")
    cookie_header = "; ".join(parts)
if not cookie_header:
    print("!! No cookies found. Create cookies.txt next to this script (see header)."); sys.exit(1)

p = urllib.parse.urlparse(URL)
host = p.netloc
UA = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
def log(*a): print(*a, flush=True)

nov = ssl.create_default_context(); nov.check_hostname=False; nov.verify_mode=ssl.CERT_NONE
def http(url, method="GET"):
    r = urllib.request.Request(url, method=method, headers={"User-Agent":UA,"Cookie":cookie_header,"Origin":f"https://{host}"})
    return urllib.request.urlopen(r, timeout=30, context=nov).read().decode("utf-8","replace")

log("1) checking auth ...")
who = http(f"https://{host}/api/xml?action=common-info")
m = re.search(r'<user user-id="(\d+)"[^>]*>\s*<name>([^<]*)</name>\s*<login>([^<]*)</login>', who)
if m: log(f"   logged in as: {m.group(2)} (login {m.group(3)}, id {m.group(1)})")
else: log("   !! not logged in - cookies expired? refresh cookies.txt"); sys.exit(1)

log("2) fetching recording page for ticket ...")
html = http(URL)
dec = urllib.parse.unquote(urllib.parse.unquote(html))
mt = re.search(r"ticket=([A-Za-z0-9]+)", dec)
ma = re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output", dec)
if not (mt and ma): log("   !! could not extract ticket/appInstance (not authorized for this recording?)"); sys.exit(1)
ticket = mt.group(1); acct, sco = ma.groups()
connect_url = f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"
log(f"   ticket OK, content path {acct}/{sco}/output/")

def parse_bin(raw):
    if not raw or raw[0]!=0x03: return None
    nlen = struct.unpack(">I", raw[5:9])[0]
    return raw[9:9+nlen].decode("ascii","replace"), raw[9+nlen:]

async def main():
    log("3) connecting to media gateway wss://%s:1443 ..." % host)
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    async with websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":UA,"Cookie":cookie_header},
            compression=None, max_size=None, open_timeout=30) as ws:
        nid=[0]
        async def send(o): await ws.send(json.dumps(o))
        async def newstream():
            n=f"nsID_{nid[0]}"; nid[0]+=1
            await send({"type":"NCFunc","method":"createNetStream","nsId":n,"mediaAvailable":False}); return n
        await send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
        await send({"type":"NCFunc","method":"connect","url":connect_url,
                    "params":{"ticket":ticket,"reconnection":False,"swfUrl":f"https://{host}/common/meetinghtml/index.html","Recording":True}})
        idx=await newstream()
        await send({"type":"NSFunc","method":"play","nsId":idx,"streamName":"indexstream","start":0,"length":-1,"reset":3,"mediaAvailable":False})

        nsmap={idx:"indexstream"}; streams=[]; bytes_by=collections.Counter(); frames_by=collections.Counter()
        connected=False; duration=None; pulled=False
        deadline=time.monotonic()+18
        while time.monotonic()<deadline:
            try: msg=await asyncio.wait_for(ws.recv(), timeout=deadline-time.monotonic())
            except asyncio.TimeoutError: break
            if isinstance(msg,(bytes,bytearray)):
                r=parse_bin(msg)
                if r: bytes_by[r[0]]+=len(r[1]); frames_by[r[0]]+=1
                continue
            j=json.loads(msg)
            code=(j.get("status") or {}).get("code")
            if code=="NetConnection.Connect.Success": connected=True; log("   gateway connected ✓")
            cmd=j.get("cmdString")
            if cmd=="onMetaData": duration=j["params"]["arg_0"].get("duration"); log(f"   recording duration: {duration:.0f}s ({duration/60:.1f} min)")
            if cmd=="playEvent" and isinstance(j["params"].get("arg_2"),list):
                for s in j["params"]["arg_2"]:
                    streams.append(s); log(f"   + media stream: {s.get('streamName')} ({s.get('streamType')}) @ {s.get('startTime')}s")
            if not pulled and len([s for s in streams if s.get("streamType") in ("cameraVoip","screenshare")])>=1:
                pulled=True
                for s in streams:
                    if s.get("streamType") in ("cameraVoip","screenshare"):
                        n=await newstream(); nsmap[n]=s["streamName"]
                        await send({"type":"NSFunc","method":"play","nsId":n,"streamName":s["streamName"],"start":0,"length":-1,"reset":3,"mediaAvailable":False})
                log("   pulling media for ~12s ...")
        log("\n================ RESULT ================")
        total=sum(bytes_by.values())
        log(f"gateway connected: {connected}")
        log(f"duration: {duration}")
        log(f"media streams discovered: {len(streams)}")
        for nsid,b in bytes_by.items():
            log(f"  {nsmap.get(nsid,nsid)}: {frames_by[nsid]} frames, {b/1024:.0f} KB")
        if total>0 and connected:
            log(f"\n✅ SUCCESS — pulled {total/1024:.0f} KB of media. The downloader will work from this machine.")
        elif connected:
            log("\n⚠️ Connected but no media bytes — tell Claude.")
        else:
            log("\n❌ Gateway silent (no connect). If you're on a VPN, try toggling it to match your browser.")
asyncio.run(main())
