#!/usr/bin/env python3
"""Minimal live probe: session token -> ticket -> WS connect -> play indexstream.
Usage: python3 probe_ws.py <recording_url_with_session>
"""
import sys, re, json, ssl, asyncio, base64, os, urllib.parse, urllib.request, collections

URL = sys.argv[1]
COOKIE_OVERRIDE = os.environ.get("ACD_COOKIE")  # full "name=val; name=val" header
host = urllib.parse.urlparse(URL).netloc
rec_id = urllib.parse.urlparse(URL).path.strip("/").split("/")[0]
session = urllib.parse.parse_qs(urllib.parse.urlparse(URL).query).get("session", [""])[0]

# 1) fetch page, extract ticket + account/sco
cookie_header = COOKIE_OVERRIDE if COOKIE_OVERRIDE else f"BREEZESESSION={session}"
req = urllib.request.Request(URL, headers={
    "User-Agent": "Mozilla/5.0",
    "Cookie": cookie_header,
})
_noverify = ssl.create_default_context(); _noverify.check_hostname=False; _noverify.verify_mode=ssl.CERT_NONE
html = urllib.request.urlopen(req, timeout=30, context=_noverify).read().decode("utf-8", "replace")
dec = urllib.parse.unquote(urllib.parse.unquote(html))   # double-decode the launch params
ticket = re.search(r"ticket=([A-Za-z0-9]+)", dec)
appinst = re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output", dec)
login = ("loginField" in html)
print(f"host={host} rec_id={rec_id} session={session[:10]}...")
print(f"page shows loginField: {login}")
print(f"ticket: {ticket.group(1) if ticket else None}")
print(f"appInstance: {appinst.groups() if appinst else None}")
if not ticket or not appinst:
    print("!! could not extract ticket/appInstance -> session likely expired or page changed")
    # dump a hint
    m = re.search(r"loginUsername|This recording|enter your", html)
    print("hint:", m.group(0) if m else "(none)")
    sys.exit(1)

ticket = ticket.group(1)
acct, sco = appinst.groups()
connect_url = f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"
print(f"connect_url: {connect_url}")

import websockets

async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    ws_url = f"wss://{host}:1443/"
    async with websockets.connect(
        ws_url, ssl=ssl_ctx, origin=f"https://{host}",
        additional_headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie_header},
        max_size=None, open_timeout=30,
    ) as ws:
        print("  WS OPENED. subprotocol=", ws.subprotocol, "resp_headers=", dict(ws.response.headers) if hasattr(ws,'response') else 'n/a')
        async def send(obj):
            await ws.send(json.dumps(obj)); print("  >>", json.dumps(obj)[:90])
        await send({"type":"WSFunc","method":"startHeartbeat","value":True})
        await send({"type":"WSFunc","method":"allowPacketDrop","value":False})
        await send({"type":"WSFunc","method":"fragmentVideoPacket","value":False})
        await send({"type":"NCFunc","method":"connect","url":connect_url,
                    "params":{"ticket":ticket,"reconnection":False,
                              "swfUrl":f"https://{host}/common/meetinghtml/index.html","Recording":True}})
        await send({"type":"NCFunc","method":"createNetStream","nsId":"nsID_0","mediaAvailable":False})
        await send({"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream",
                    "start":0,"length":-1,"reset":3,"mediaAvailable":False})

        streams=[]; bincount=collections.Counter(); connected=False; duration=None; rawcount=0
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=12)
                rawcount+=1
                if isinstance(msg, (bytes, bytearray)):
                    bincount["bin"]+=1
                    if bincount["bin"]<=2: print("  << [BINARY]", len(msg), msg[:24].hex())
                    continue
                if rawcount<=8: print("  <<", msg[:160])
                j=json.loads(msg)
                code=(j.get("status") or {}).get("code")
                if code:
                    print("  onStatus:", code, j.get("nsId",""))
                    if code=="NetConnection.Connect.Success": connected=True
                cmd=j.get("cmdString")
                if cmd=="onMetaData":
                    duration=j["params"]["arg_0"].get("duration"); print("  metadata duration =", duration)
                if cmd=="playEvent" and isinstance(j["params"].get("arg_2"),list):
                    for s in j["params"]["arg_2"]:
                        streams.append((s.get("streamName"), s.get("streamType"), s.get("startTime")))
                        print("  streamAdded:", s.get("streamName"), s.get("streamType"), "@", s.get("startTime"))
        except asyncio.TimeoutError:
            pass
        print(f"\nRESULT: connected={connected} duration={duration} streams_found={len(streams)} binary_frames={bincount['bin']}")

asyncio.run(main())
