#!/usr/bin/env python3
"""Offline validation of the capture->FLV->mux path using REAL recorded traffic.

Reads a .har of an actual successful playback, reconstructs the nsId->streamName
mapping from the text frames, decodes the binary media frames, and feeds them
through acd.write_flvs() + acd.mux() exactly as the live downloader would — so we
can confirm the (rewritten) pipeline still yields a valid MP4 without the flaky
live gateway.
"""
import sys, os, json, base64, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import acd

HAR = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else "har_validate.mp4"
WORK = os.path.splitext(OUT)[0] + "_streams"

h = json.load(open(HAR))
ws = None
for e in h["log"]["entries"]:
    if e.get("_webSocketMessages") and ":1443" in e["request"]["url"]:
        ws = e["_webSocketMessages"]; break
if not ws:
    sys.exit("no :1443 websocket in HAR")

nsid2name = {}     # nsId -> streamName (from play commands the client sent)
streams = {}       # streamName -> {streamName, streamType, startTime}
duration = None
frames = collections.defaultdict(list)   # streamName -> [(type, ts, payload)]

for m in ws:
    op = m.get("opcode"); direction = m.get("type")
    data = m.get("data", "")
    if op == 1:  # text JSON
        try: j = json.loads(data)
        except Exception: continue
        if direction == "send" and j.get("method") == "play" and j.get("streamName") and j.get("nsId"):
            nsid2name[j["nsId"]] = j["streamName"]
        st = j.get("status") or {}
        cmd = j.get("cmdString")
        if cmd == "onMetaData":
            try: duration = j["params"]["arg_0"].get("duration")
            except Exception: pass
        if cmd == "playEvent" and isinstance((j.get("params") or {}).get("arg_2"), list):
            for s in j["params"]["arg_2"]:
                if isinstance(s, dict) and s.get("streamName"):
                    streams.setdefault(s["streamName"], s)
    elif op == 2:  # binary media frame
        raw = base64.b64decode(data)
        r = acd.parse_bin(raw)
        if not r: continue
        typ, ts, nsid, pl = r
        name = nsid2name.get(nsid)
        if name and name != "indexstream":
            frames[name].append((typ, ts, pl))

print(f"duration={duration}")
print(f"nsId map: {nsid2name}")
print(f"discovered streams: {[(s.get('streamName'), s.get('streamType'), s.get('startTime')) for s in streams.values()]}")
for name, fr in frames.items():
    if fr:
        print(f"  frames[{name}]: {len(fr)} tags, ts {fr[0][1]}..{fr[-1][1]} ms, {sum(len(p) for _,_,p in fr)/1e6:.2f} MB")

# Build the media list the way the downloader would, but only keep streams we have frames for.
media = []
for name in frames:
    s = streams.get(name) or {"streamName": name, "startTime": 0,
                              "streamType": "screenshare" if "screenshare" in name else "cameraVoip"}
    media.append(s)
media.sort(key=lambda s: s.get("startTime", 0))

class FakeDL:  # only what write_flvs/mux read
    pass
dl = FakeDL(); dl.media = media; dl.frames = frames; dl.duration = duration

os.makedirs(WORK, exist_ok=True)
flvs = acd.write_flvs(dl, WORK)
print(f"wrote {len(flvs['video'])} video + {len(flvs['audio'])} audio FLV(s)")
acd.mux(flvs, OUT, duration)
print(f"\nMUXED -> {OUT}")
