#!/usr/bin/env python3
"""Bounded live test: auth -> connect -> index -> pull media bytes for a few seconds."""
import sys, re, json, ssl, asyncio, base64, os, struct, time, collections
import urllib.parse, urllib.request
import websockets

URL = sys.argv[1]
RUN_SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
COOKIE = os.environ["ACD_COOKIE"]
p = urllib.parse.urlparse(URL)
host = p.netloc
session = urllib.parse.parse_qs(p.query).get("session", [""])[0]

def log(*a): print(*a, flush=True)

# --- 1) page -> ticket + appInstance ---
nov = ssl.create_default_context(); nov.check_hostname=False; nov.verify_mode=ssl.CERT_NONE
req = urllib.request.Request(URL, headers={"User-Agent":"Mozilla/5.0","Cookie":COOKIE})
html = urllib.request.urlopen(req, timeout=30, context=nov).read().decode("utf-8","replace")
dec = urllib.parse.unquote(urllib.parse.unquote(html))
ticket = re.search(r"ticket=([A-Za-z0-9]+)", dec).group(1)
acct, sco = re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output", dec).groups()
connect_url = f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"
log(f"ticket={ticket}  appInstance={acct}/{sco}  connect_url={connect_url}")

def parse_bin(raw):
    if not raw or raw[0]!=0x03: return None
    ts=struct.unpack(">I",raw[1:5])[0]
    nlen=struct.unpack(">I",raw[5:9])[0]
    nsid=raw[9:9+nlen].decode("ascii","replace")
    return ts, nsid, raw[9+nlen:]

async def main():
    ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    async with websockets.connect(f"wss://{host}:1443/", ssl=ctx, origin=f"https://{host}",
            additional_headers={"User-Agent":"Mozilla/5.0","Cookie":COOKIE},
            max_size=None, open_timeout=30) as ws:
        log("WS opened:", ws.response.headers.get("server"))
        nid=[0]
        async def send(o): await ws.send(json.dumps(o))
        async def newstream():
            n=f"nsID_{nid[0]}"; nid[0]+=1
            await send({"type":"NCFunc","method":"createNetStream","nsId":n,"mediaAvailable":False}); return n
        async def play(n,name):
            await send({"type":"NSFunc","method":"play","nsId":n,"streamName":name,"start":0,"length":-1,"reset":3,"mediaAvailable":False})
        await send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
        await send({"type":"NCFunc","method":"connect","url":connect_url,
                    "params":{"ticket":ticket,"reconnection":False,"swfUrl":f"https://{host}/common/meetinghtml/index.html","Recording":True}})
        idx=await newstream(); await play(idx,"indexstream")

        nsmap={idx:"indexstream"}; streams=[]; bytes_by=collections.Counter(); frames_by=collections.Counter()
        firstbyte=collections.Counter(); connected=False; duration=None; played_media=False
        deadline=time.monotonic()+RUN_SECS
        while time.monotonic()<deadline:
            try: msg=await asyncio.wait_for(ws.recv(), timeout=deadline-time.monotonic())
            except asyncio.TimeoutError: break
            if isinstance(msg,(bytes,bytearray)):
                r=parse_bin(msg)
                if r: ts,nsid,pl=r; bytes_by[nsid]+=len(pl); frames_by[nsid]+=1; firstbyte[(nsid,pl[0] if pl else -1)]+=1
                else: frames_by["<nohdr>"]+=1
                continue
            j=json.loads(msg)
            code=(j.get("status") or {}).get("code")
            if code=="NetConnection.Connect.Success": connected=True; log("  connect OK")
            cmd=j.get("cmdString")
            if cmd=="onMetaData": duration=j["params"]["arg_0"].get("duration"); log("  duration =",duration)
            if cmd=="playEvent" and isinstance(j["params"].get("arg_2"),list):
                for s in j["params"]["arg_2"]:
                    streams.append(s); log("  +stream", s.get("streamName"), s.get("streamType"), "@", s.get("startTime"))
            # once we know media streams, start pulling the first cam + screen as a proof
            if not played_media and len([s for s in streams if s.get("streamType") in ("cameraVoip","screenshare")])>=2:
                played_media=True
                for s in streams:
                    if s.get("streamType") in ("cameraVoip","screenshare"):
                        n=await newstream(); nsmap[n]=s["streamName"]; await play(n,s["streamName"])
                        log("  -> pulling", s["streamName"], "as", n)
        log("\n==== RESULT ====")
        log("connected:",connected," duration:",duration," streams discovered:",len(streams))
        for nsid,b in bytes_by.items():
            log(f"  {nsid} ({nsmap.get(nsid,'?')}): {frames_by[nsid]} frames, {b} bytes")
        log("  frames without header:", frames_by.get("<nohdr>",0))
        log("  sample (nsid,firstbyte)->count:", dict(list(firstbyte.items())[:8]))

asyncio.run(main())
