# How Adobe Connect recording playback works (reverse-engineered)

Target: Adobe Connect **12.10.0**, classic FLV recordings (not WebRTC). This is the
authoritative reference for the protocol the app implements. All of it is verified against
real sessions (see `research/` probes and the prototypes `acd.py` / `acd_fast.py`).

## 1. Transport
The HTML5 player streams media as **RTMP tunneled in JSON over WebSocket**:
`wss://<host>:1443/` (a Boost.Beast gateway bridging WS ↔ a local FMS/AMS on `localhost:8506`).

Message envelopes (text frames, JSON):
- `WSFunc` — socket setup (`startHeartbeat`, `allowPacketDrop`, `fragmentVideoPacket`)
- `NCFunc` — NetConnection: `connect`, `createNetStream`, `call` (e.g. `@getStats`)
- `NSFunc` — NetStream: `play`, `receiveAudio`, `receiveVideo`
Responses arrive as `onStatus` / `onCommand` / `onData` (with `cmdString` like `playEvent`,
`onMetaData`). Media arrives as **binary** frames (see §5).

## 2. Authentication — the link is the credential
A recording URL looks like `https://<host>/<id>/?session=<token>`. The `?session=` **token
is itself a `BREEZESESSION` value** — the link is self-authenticating:
- Fetch the recording page; scrape `ticket=([A-Za-z0-9]+)` and
  `appInstance=(\d+)/(\d+-1)/output`.
- This works **with no manually-supplied cookies** — the `?session=` token in the URL is
  enough to obtain the ticket. (Optionally set `BREEZESESSION=<token>` to also read the user
  name via `/api/xml?action=common-info`, purely cosmetic.)
- The ticket is re-minted on every page fetch.
Fallback: if a link ever fails, accept manually-pasted cookies (`BREEZESESSION`,
`JSESSIONID`, `BreezeCCookie`). Tokens/cookies expire ~hourly.

`connect_url = rtmp://<host>:1935/?rtmp://localhost:8506/flvplayeras3app/<acct>/<sco>-1/output/`

## 3. The establish handshake — THE critical part
After the WS upgrade (send no Cookie header; auth is the ticket):
1. `WSFunc startHeartbeat=true`, `allowPacketDrop=false`, `fragmentVideoPacket=false`
   (false = clean, unfragmented VP6; the browser uses true and gets `payload[0]=0x01` fragments).
2. `NCFunc connect {url: connect_url, params:{ticket, reconnection:false, swfUrl, Recording:true}}`
3. Wait for `onStatus NetConnection.Connect.Success`.
4. **Wait for `onCommand {arg_0:{command:"accepted", registerMeetingValues:{…}}}`** — this is
   the gateway registering you into the meeting. **Until `accepted` arrives, every stream op
   is silently ignored.** This was the single hardest bug to find.

Two independent failure modes when establishing:
- **Reused ticket → Connect.Success but NEVER `accepted`** (dead connection). The ticket is
  effectively **single-use for registration**. ⇒ **Re-mint a fresh ticket for every attempt.**
- **`Connect.Success` itself is flaky (~20-60%/attempt)**, varies with gateway load. ⇒ retry.

⇒ Establish loop = *fresh ticket each try + wait for `accepted` + fast retry* (lands in
~10-60s). Multiple browser tabs work precisely because each tab mints its own fresh ticket.

Keepalive: send `NCFunc call @getStats` every ~5s or the gateway drops the connection at
~90-130s.

## 4. Stream discovery
`createNetStream nsID_0`, then `call startLoadEditInfo` + `preloadStreams`, then
`play indexstream`. The index returns:
- `onMetaData.duration` (seconds) — **nominal**; may exceed real content (trailing dead air).
- `playEvent.arg_2[]` = stream-added events: `{streamName, streamType, startTime(ms)}`.

Stream types seen:
- `screenshare` — shared screen (VP6 video). Static screen ⇒ no new frames (last frame holds).
- `cameraVoip` — webcam **video (VP6) + mic audio (Nellymoser)** in one stream. Camera may be
  off (audio only) for stretches; turns on/off mid-recording.
- content/aux streams (`ftcontent1`, `ftstage2`, `ftchat0`, `transcriptstream`) — pod data,
  including the **whiteboard** (§8).

## 5. Media frames (binary)
Layout: `[1B type][4B BE timestamp ms][4B BE nsIdLen][nsId ascii][FLV tag body]`
- type `0x03` = audio (Nellymoser `0x6a`, 22050 Hz mono)
- type `0x04` = video (VP6 `vp6f`; first body byte `0x14`=keyframe, `0x24`=interframe)
- A `cameraVoip` stream yields **both** types — split by frame type (audio→audio track,
  video→webcam track), do NOT assume per-stream type.

## 6. Parallel download
The gateway serves **many concurrent seeked `play`s on one connection**. `play start=K
length=L` seeks within a stream's own timeline, **lands on a video keyframe**, and seeked
frames carry **relative** timestamps (0..L). So: split each stream's timeline into chunks
(~300s), run up to **par=24** concurrently, pace `createNetStream` ~0.3s apart.
- **Parallelism ceiling: 24** = max stable throughput (~24× realtime, 0 drops). Higher
  over-drives the single connection → repeated drops (recoverable via resume, but net slower).
- **Reconnect+resume:** on drop, re-establish (fresh ticket!) and resume unfinished chunks
  from where they stopped. Per-chunk `ts_offset` re-bases the relative ts of replayed frames.

## 7. Reconstruction & muxing
Per stream, walk chunks in start order; global ts = `chunk_start*1000 + relative_ts`; keep
only strictly-increasing ts (drops the ~6s chunk overlap so seams aren't doubled). Write an
FLV per track. Then ffmpeg:
- video placed at its global `startTime` via `setpts=PTS-STARTPTS+start/TB`
- audio placed via `adelay=start|start`, multiple segments `amix`'d
- **webcam PiP:** scale small, overlay top-right while a screen-share is active; full-frame
  when nothing is shared.
Transcode `-c:v libx264 -c:a aac` → MP4.

## 8. Whiteboard (NOT a video stream)
A whiteboard is **vector draw data**, delivered as SharedObject updates on the content stream
(`ftcontent1`): `__registerSo__ setWBSo` / `set_WB_So_*`, with `shareType:"wb"` and shape
objects `{pts:[{x,y}…], alpha, depth, currentPage, htmlText, height}`. There is **no pixel
stream** to capture. Rendering it requires interpreting these primitives (shapes, pages,
text) and rasterizing to a synced video track — see M4 in `TASKS.md`.

## 9. Disproven theories (don't revisit)
- "One connection per account / close the browser" — false; multiple tabs connect fine.
- "Flaky France egress / Iran-only" — false; reproduced everywhere.
- "Churn/rate penalty, cool down" — false; the real cause was reused tickets.
- "Must omit Cookie on WS" — true but irrelevant (browser omits it too; auth is the ticket).
- "Ticket is NOT single-use" — reversed: it **is** single-use *for registration*.
