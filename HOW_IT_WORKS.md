# How the Adobe Connect recording downloader works

Canonical technical reference for this project. Written after reverse-engineering the
SBU Adobe Connect HTML5 player end-to-end and producing a working MP4.

**Target sample:** `https://vc10.sbu.ac.ir/paktw0zusk0a/?session=<token>`
(SBU = Shahid Beheshti University. Adobe Connect **12.10.0**, on-premise, FMS/AMS 5.)

The working implementation is **`acd.py`** at the repo root. The `research/` folder holds
the throwaway probes used to discover all of this (`acd_test2.py`, `seqtest2.py`,
`dump_flv.py`, plus the `.har` captures).

---

## 1. TL;DR of the pipeline

```
recording URL + your login cookies
        │
        ├─(HTTPS GET page)→ extract ticket + account/sco from page HTML
        │
        ▼
  wss://HOST:1443/   (WebSocket, JSON-RPC wrapping RTMP; NO cookie on the upgrade!)
        │  connect(ticket) → play "indexstream"  → learn duration + list of media streams
        │  play each media stream (mediaAvailable:true)
        ▼
  binary frames  [type][ts][nsId][FLV-tag-body]   →  one .flv per stream
        │   screenshare → VP6 video ;  cameraVoip → Nellymoser audio
        ▼
  ffmpeg: align each stream by its global startTime, transcode → single MP4 (H.264/AAC)
```

Result plays in any player; user can watch at 2×.

---

## 2. What an Adobe Connect recording is

NOT a single video file. It is a set of time-stamped **streams** that the player
re-assembles live. Relevant ones:

- `indexstream` — master timeline / metadata. Emits `onMetaData` (duration) and
  `playEvent → streamAdded` events that name every media stream + its `startTime` (ms).
- `screenshare_<pubId>_<n>` — the shared screen. **VP6** video (`vp6f`), e.g. 1920×1088.
- `cameraVoip_<pubId>_<n>` — mic audio (+ optional webcam). **Nellymoser** 22050 Hz mono.
  Audio is split into multiple files over a long meeting (mic reconnects / file rotation).
- `ftstage*`, `ftchat*`, `ftcontent*`, `transcriptstream` — feature tracks (layout, chat,
  captions). Not needed for a watchable video; we ignore them.

Two recording flavors exist in Connect; **handle both detections**:
- **Classic FLV/RTMP** (our sample): VP6 + Nellymoser over RTMP. This is what `acd.py` does.
- **WebRTC**: a single `output/webrtc/recording.mp4`. `sco-info` reports
  `isWebRTCRecording=true` for these — if so, just download that mp4 directly (not implemented).

---

## 3. What does NOT work (and why) — dead ends

- **`/<id>/output/<anything>.zip?download=zip`** (the method every existing GitHub tool uses):
  returns an HTML **"Not Authorized"** page on this server. `sco-info` shows
  `download-recording-access=false` — the admin disabled source download for students.
  Confirmed it fails even in the user's own browser with a full session.
- **URL `?session=` token alone is NOT enough auth.** It grants *viewing* but the page then
  shows a login form and the minted ticket is not fully privileged. You need the **full login
  cookies** (`BREEZESESSION` + `JSESSIONID` + `BreezeCCookie`) for the HTTP calls.
- **Native RTMP (rtmpdump) on :1935**: port is open, but the URL is FCS-tunneled
  (`rtmp://host:1935/?rtmp://localhost:8506/...`) with a ticket — fiddly. The WebSocket path
  is what the HTML5 client actually uses and is fully captured, so we use that instead.

---

## 4. Auth: cookies + the ticket

1. Copy three cookies from the browser (DevTools → Application → Cookies → the vc host):
   `BREEZESESSION`, `JSESSIONID`, `BreezeCCookie`. (`acd.py` reads them from `cookies.txt`,
   lines `Name: value`.) These are the **full logged-in session**.
2. Sanity check / who-am-I:
   `GET https://HOST/api/xml?action=common-info` (with cookies) → `<user …><name>…</name>`.
   If this shows a real user, cookies are valid. (Also: `?action=sco-info&sco-id=<id>` and
   `?action=acts-location` → `acts-proto-string=wss:1443`.)
3. `GET https://HOST/<id>/?session=<token>` (with cookies) → recording page HTML. The launch
   params are double-URL-encoded; decode twice, then regex:
   - `ticket=([A-Za-z0-9]+)`  — the FMS connection ticket (== `aicc_sid` == `sid`).
     **Freshly minted on every page load** (single-use-ish); fetch it right before connecting.
   - `appInstance=([0-9]+)/([0-9]+-1)/output` — the `account` and `sco-1` ids.
4. Build the RTMP connect URL:
   `rtmp://HOST:1935/?rtmp://localhost:8506/flvplayeras3app/<acct>/<sco-1>/output/`

---

## 5. The WebSocket protocol (exact)

Endpoint: `wss://HOST:1443/`  (a `Boost.Beast` gateway that bridges JSON ⇄ RTMP to FMS).
Messages are **JSON text frames** mirroring ActionScript NetConnection/NetStream:
`WSFunc` = socket control, `NCFunc` = NetConnection, `NSFunc` = NetStream.

### 5.1 Connect handshake (order matters)
```json
{"type":"WSFunc","method":"startHeartbeat","value":true}
{"type":"WSFunc","method":"allowPacketDrop","value":false}        // false = don't drop, we want all data
{"type":"WSFunc","method":"fragmentVideoPacket","value":false}    // false = clean unfragmented VP6 tags
{"type":"NCFunc","method":"connect","url":"<connect_url>",
   "params":{"ticket":"<ticket>","reconnection":false,
             "swfUrl":"https://HOST/common/meetinghtml/index.html","Recording":true}}
```
→ wait for `{"method":"onStatus","status":{"code":"NetConnection.Connect.Success"}}`
   (then a `loginHandler … "accepted"` onCommand).

### 5.2 Discover streams (the index)
```json
{"type":"NCFunc","method":"createNetStream","nsId":"nsID_0","mediaAvailable":false}
// wait for onStatus desc "StreamCreated" for nsID_0
{"type":"NCFunc","method":"call","method-name":"startLoadEditInfo","params":{},"responderId":1}
{"type":"NCFunc","method":"call","method-name":"preloadStreams","responderId":2}
{"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":-1,"reset":3,"mediaAvailable":false}
{"type":"NSFunc","method":"play","nsId":"nsID_0","streamName":"indexstream","start":0,"length":5,"reset":2,"mediaAvailable":false}
```
Responses on nsID_0:
- `onData cmdString=onMetaData` → `params.arg_0.duration` (seconds).
- `onData cmdString=playEvent` with `params.arg_2` = list of `streamAdded` objects:
  `{streamName, streamType, startTime, streamPublisherID}`. **`arg_2` items can be plain
  strings, not dicts — guard with `isinstance(s, dict)` or you crash the reader.**

### 5.3 Play each media stream
For every `screenshare`/`cameraVoip` stream:
```json
{"type":"NCFunc","method":"createNetStream","nsId":"nsID_N","mediaAvailable":false}   // wait StreamCreated
// audio only:
{"type":"NSFunc","method":"receiveAudio","nsId":"nsID_N","action":true}               // note "action", not "value"
{"type":"NSFunc","method":"play","nsId":"nsID_N","streamName":"<name>","start":0,"length":-1,"reset":1,"mediaAvailable":true}
```
Completion: `onStatus` with code containing `Play.Stop` / `Play.Complete` /
`Play.UnpublishNotify` for that nsId. (Fallback: global idle — no binary frame for ~25 s.)

### 5.4 Binary media frame layout
Big-endian, header is 9 bytes + the nsId string:
```
byte 0      : type  (0x03 = audio, 0x04 = video)
bytes 1..4  : timestamp in ms (uint32 BE)
bytes 5..8  : nsId length (uint32 BE)
bytes 9..   : nsId ascii (e.g. "nsID_10")
remainder   : payload == an FLV TAG BODY (no FLV tag header)
```
- Audio payload[0] = `0x6a` → FLV audio config byte = Nellymoser, 22050 Hz, 16-bit, mono.
- Video payload[0] = `0x14` (VP6 keyframe) / `0x24` (VP6 interframe) / `0x54` (info) → codecId 4.
Map nsId → stream via the play you issued; append `(type, ts, payload)`.

### 5.5 Build FLV per stream
Write a normal FLV: header (audio/video flag) + for each frame a tag
`[tagType 8|9][3B dataSize][3B ts][1B tsExt][3B streamID=0][payload][4B prevTagSize=11+len]`.
screenshare → video FLV (tagType 9), cameraVoip → audio FLV (tagType 8). ffmpeg reads these
directly (`vp6f`, `nellymoser`).

### 5.6 Mux (ffmpeg)
Align each stream on the global timeline by its `startTime`:
- video: `setpts=PTS-STARTPTS+<startMs>/1000/TB` (single screenshare → offset ≈ 0). If multiple
  screenshare segments, concat in startTime order.
- audio: `[k:a]adelay=<startMs>|<startMs>[ak]` per segment, then
  `amix=inputs=N:normalize=0:dropout_transition=0`.
- encode `-c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart`.

### 5.7 Surviving drops: reconnect + resume (why frames are keyed by stream NAME)
A full pull is ~77 min over a single WebSocket — and the gateway is flaky (gotcha #9), so the
socket can drop or stall mid-download. `acd.py` is built to survive that **without truncating
or restarting from zero**:
- Captured frames are accumulated in `frames[streamName]` (NOT keyed by the per-connection
  `nsId`, which is meaningless across reconnects). `done` is a set of finished stream names.
  `media`/`duration` are discovered once and persist.
- The reader sets a `ws_closed` flag if `recv()` raises. The collect loop breaks on: all
  streams `done` (real completion), `ws_closed` (drop), or ~30 s with no frames while the
  socket is still open (stall).
- On drop/stall it **re-establishes a fresh primed session** (re-minting the ticket) and
  **replays only the not-yet-`done` streams from their resume point**:
  `start = (last ts received for that stream)/1000`. Because each stream file's timeline is
  self-contained, seeking to a point *inside* it is valid (unlike seeking to the global
  `startTime`, gotcha #4). Frames from before and after the seam land in the same
  `frames[name]` list; `write_flvs` sorts by ts before writing, so an overlap at the seam is
  harmless.
- A no-progress guard (4 consecutive reconnect cycles that add no bytes) prevents an endless
  loop; it then muxes whatever was captured rather than spinning forever.
This is validated for the audio path against real recorded traffic (`research/har_replay.py`
replays the `.har`'s binary frames through `write_flvs`+`mux` → a valid Nellymoser→AAC MP4).
Video can't be validated from the HAR because the browser fragmented it (gotcha #6); the live
tool's `fragmentVideoPacket:false` path produced a valid `vp6f 1920×1088` FLV directly.

---

## 6. GOTCHAS (the hard-won ones — read before touching the code)

1. **NO `Cookie` header on the WebSocket upgrade.** This is THE killer bug. If you send cookies
   on the `wss://…:1443` upgrade, the gateway accepts the upgrade then goes **completely silent**
   (no `Connect.Success`, no error, nothing). The browser sends no cookie there — auth is 100%
   the ticket inside the connect message. Omit the cookie → it connects. (Cookies ARE needed on
   the HTTP page/API calls to get the ticket.)
2. **`mediaAvailable:true` on media `play` is required.** With `false` (the index value),
   audio doesn't flow at all and video timestamps all come back as 0. With `true`, audio flows
   and video gets real timestamps. (The very first video frames are still ts=0 — the initial
   keyframe burst — then they increment; that's fine.)
3. **Audio needs `receiveAudio` with `"action":true`** (NOT `"value":true`) sent before play,
   or the cameraVoip stream delivers zero frames.
4. **Play media with `start:0`, not `start:startTime/1000`.** Each stream file's internal
   timeline starts at ~0. The index `startTime` is the stream's *global* position (e.g. a 2nd
   audio segment at 1810506 ms = 30 min). If you pass that as `start`, you seek past the end of
   the file and get an immediate `Play.Stop`. Use `start:0` to pull the whole file; apply the
   global offset only at **mux** time.
5. **Sequence the control messages; don't burst them.** Wait for `NetConnection.Connect.Success`
   before `createNetStream`, and wait for each stream's `StreamCreated` before `play`. Bursting
   gives `onError: "Net Stream not found"` and nothing plays.
6. **`fragmentVideoPacket:false`** gives clean FLV VP6 tags (`0x14/0x24`). The browser uses
   `true` (low latency) which fragments video across frames with a `0x00/0x01` sub-header —
   harder to reassemble. Use `false`.
7. **`startLoadEditInfo` + `preloadStreams` calls** before playing the index — without them the
   index may not deliver `streamAdded` events. Replicate the browser's exact early sequence
   (3× createNetStream, the two calls, then the index play ×2).
8. **`playEvent.arg_2` may contain strings**, not just dicts — guard before `.get`.
9. **★ ONE connection per account — CLOSE THE BROWSER. ★** This is the real cause of all the
   "accepts the socket then goes silent" failures. Adobe Connect allows only **one concurrent
   media-gateway connection per account**. If the recording is open in the user's **browser**, the
   browser holds that single slot, and our tool's connection is accepted then gets no
   `Connect.Success` (or connects then goes silent on `createNetStream` / the index returns no
   duration). **Close the recording tab → the slot frees → `connect`+`createNetStream` succeed
   instantly (~0.2 s) and the download runs.** And don't reopen it during the ~77-min download —
   reopening steals the slot back and drops us (resume will try, but just leave it closed).
   - **Diagnose:** `lsof -nP -iTCP | grep 1443 | grep ESTABLISHED` — if Google Chrome (or anything)
     holds `…:1443`, that's the slot; `(none)` means it's free and the tool should connect.
   - **This answers "why does the browser always get it?"** — the browser got there first and owns
     the slot; we lose the race.
   - **Disproven theories (do not revisit):** *(a) "flaky from France / reliable from Iran"* — the
     user reproduced identical failures on their own machine. *(b) "gateway overloaded / rate-
     limiting the account"* — impossible, since the browser connects fine the whole time (a working
     browser is a live counter-example). *(c) "ticket is single-use"* — controlled experiment
     (`research/ticket_test.py`): reuse-ticket **0/5** *and* fresh-ticket **0/5** (slot was busy in
     both), so ticket freshness is not the discriminator. The "each batch did worse / recovered
     after waiting" pattern was just whether the browser held the slot at that moment.
   - `acd.py` retries `_establish` with dense sampling (~30 light-spaced attempts) to ride out a
     momentary slot grab, but with the browser closed it connects on roughly the first try.
10. **TLS:** the server's CA isn't in Python's default store here → use an unverified SSL context
    (`check_hostname=False; verify_mode=CERT_NONE`) for both the HTTPS fetch and the WSS connect.
11. **The ticket is per-page-load and short-lived.** Fetch the page → ticket immediately before
    connecting. Don't reuse an old one.

---

## 7. Sample recording facts (for reference / regression)

- `sco-id=1225819`, `account_id=7`, content path `/content/7/1225819-1/output/`.
- duration `4636.897 s` (~77.3 min). Title: "کلاس آنلاین ژئومورفولوژی ساحلی (4042210230601)".
- Streams: **1** screenshare (`/screenshare_1_2`, VP6 1920×1088, startTime 38 ms, continuous)
  + **3** cameraVoip audio segments (`_0_4` @ 48 ms, `_0_5` @ 1 810 506 ms, `_0_6` @ 1 835 216 ms).
- Logged-in test user: name "کيانا جعفري", login 402221022, user-id 272226.

---

## 8. Environment / tooling

- `ffmpeg` 7.1.1 (decodes vp6f + nellymoser, encodes h264/aac). Required on PATH.
- Python 3.12, `websockets` 15.x (acd.py auto-`pip install`s it if missing).
- Adobe Connect HTML5 client also ships `vp6.wasm` + `AudioCodecs.wasm` (in-browser decoders) —
  we don't need them; ffmpeg decodes both codecs.

---

## 9. Known limitations / open questions (TODO)

- **Download speed ≈ realtime.** The server paces VOD playback, so a 77-min recording takes
  ~77 min to pull. Not yet investigated: larger buffer / seek-chunked parallel download / a
  faster play mode. Acceptable for v1 (run in background) but the main UX wart.
- **Full-recording A/V sync across audio-segment boundaries** is implemented (adelay+amix by
  global startTime) but only validated on a short slice + the first segment. A full-length run
  to confirm sync **must happen on the user's (Iran) machine** — the France egress can't even
  complete discovery right now (gotcha #9), so end-to-end full-run verification is still pending.
- **Webcam-in-cameraVoip**: this recording's cameraVoip is audio-only (constant 257-byte frames).
  If a cameraVoip also carries webcam video, frames would mix audio (0x03) and video (0x04) on
  the same nsId — split by `type` when writing FLVs. (acd.py currently assumes cameraVoip=audio.)
- **Multiple screenshare segments** (screen sharing stopped/restarted) — concat path exists but
  untested; gaps would need black-padding for perfect timeline accuracy.
- **WebRTC recordings** (`isWebRTCRecording=true`) not handled — should just fetch
  `output/webrtc/recording.mp4`.
- **Packaging / output format**: deferred with the user (CLI for now; binary/GUI later;
  screen+audio vs +webcam later).
- **Auth UX**: currently manual cookie paste. Could auto-read browser cookies (browser_cookie3)
  or do username/password login (SSO-dependent) later.
```

---

## 10. Parallel/fast download (`acd_fast.py`) — established FACTS

`acd_fast.py` downloads many time-offset CHUNKS of each stream concurrently on the single
allowed connection, then stitches them — much faster than the 1× `acd.py`. What we have
actually OBSERVED (facts, 2026-06-16), separated from open questions:

**Proven facts:**
- The gateway **serves multiple concurrent seeked `play`s on ONE connection** (you cannot open
  a 2nd connection — one per account — but you can run many NetStreams inside the one).
- `play start=K length=L` **seeks to K within a stream's OWN timeline** and the first delivered
  video frame is a **keyframe** (`0x14`) → each chunk decodes independently.
- Seeked frames carry **RELATIVE timestamps** (each chunk starts at ts≈0), so each chunk must be
  offset by its own start when rebuilding the per-stream FLV. Stitch rule that works: walk a
  stream's chunks in start order, `global_ts = chunk.start*1000 + frame_ts`, keep only frames
  with `g > last_written` (strictly increasing). This trims the deliberate `MARGIN` overlap so
  seams are **not** doubled. (Bug we hit & fixed: `ts < last` kept exact-dup ts → doubled audio
  at every seam; the user heard it at the 40s mark.)
- **Keepalive is mandatory:** the browser sends `{"type":"NCFunc","method":"call",
  "method-name":"@getStats","responderId":N}` every ~5 s (HAR: 32 sends, steady 5.0 s gaps;
  server also pushes `heartbeat`/`pacingTick`). Without it the gateway **times the connection out
  at ~90-130 s**. Symptom when missing: chunks never finish, `%` resets each reconnect, MB
  balloons re-downloading. Fixed with a 5 s `@getStats` task.
- A single `createNetStream` timeout is **per-stream jitter, not a dead socket** → retry that one
  chunk; do NOT tear down the connection (the old code did → churn loop).
- **Creates must be paced** (~0.3 s apart). A rapid burst of many `createNetStream`s makes the
  gateway drop the socket. (Ramp test paced creates held; an unpaced burst dropped.)
- **`par=5` is proven STABLE sustained:** a full 600 s slice at par 5 ran ~4 min with **0 drops**
  and produced a clean, in-sync 605.96 s MP4 (video FLV 0 backsteps, audio FLV 0 dup/back).
- **`par=12` drops the connection within ~30-60 s, repeatedly** (after the keepalive + paced-create
  + resilient-dispatch fixes — so it is NOT any of those bugs).
- A short (12 s) concurrency ramp created+delivered up to **16/16** streams with no drop, but this
  was **too short to reveal the sustained limit** — see open question.

**Open question (best current hypothesis, NOT yet proven):**
- The gateway likely tolerates only a **handful of *sustained* concurrent media streams per
  connection (~5-8)**; ~12 exceeds it and the connection is closed after a short grace period.
  The 12 s ramp over-stated the safe number because the drop takes longer than 12 s to trigger.
  To pin the exact sustained ceiling, ramp N while **holding each level ~90 s** (not 12 s).
- Practical consequence: **use `par` 5-6 for a reliable download** (~22 min for the 77-min class,
  still ~3.5× faster than 1×). Higher `par` is faster only if it doesn't drop — unverified above ~6.

**Hard-won operational note:** repeatedly restarting full runs / bursting connects **degrades the
gateway** for the account (connect starts needing many tries + immediate drops — same state as a
throttle). After heavy experimentation, STOP and let it cool ~20-30 min, then do ONE uninterrupted
run. Fresh cookies right before the run (BREEZESESSION expires ~hourly).
