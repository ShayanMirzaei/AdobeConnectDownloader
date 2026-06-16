> ⚠️ **SUPERSEDED — early R&D scratch notes.** Some conclusions here were later proven WRONG
> (e.g. the "geo-block" theory; the real blocker was sending a Cookie header on the WS upgrade,
> and the zip method being disabled). The accurate, complete reference is **`../HOW_IT_WORKS.md`**.
> Kept only for history.

# Adobe Connect Recording Download — R&D Findings

Target sample: `https://vc10.sbu.ac.ir/paktw0zusk0a/?session=breez5psrq64q2tvcb52y`
(SBU = Shahid Beheshti University. Adobe Connect **v12.10.0**, on-premise.)

## How Adobe Connect recordings work

A recording is NOT a single video file. It is a bundle of time-stamped streams the
player re-assembles live:

- `mainstream.xml` / `indexstream.xml` — the **timeline index** (when each stream
  starts/stops, in ms).
- `cameraVoip_*.flv` — **audio** (and webcam video when present).
- `screenshare_*.flv` — **screen-share video** (also encodes cursor data).
- optional: `layout.xml`, PDF/share-pod assets under `_a7/...` paths.

`sco-id=1225819`, `account_id=7`, internal content path `/content/7/1225819-1/output/`.
Two recording flavors exist:
1. **Classic FLV/RTMP** (this sample — `isWebRTCEnabledSco=false`, RTMP at `:1935`).
2. **WebRTC** → a single `output/webrtc/recording.mp4` (newer meetings only).

## The download method (what every existing tool uses)

1. Take recording URL `https://host/<ID>/?session=<S>`.
2. Set cookie `BREEZESESSION=<S>` (from the `?session=` param).
3. GET `https://host/<ID>/output/<anything>.zip?download=zip`
   - server zips the whole `output/` dir on the fly; filename is arbitrary.
   - success = response `Content-Type: application/zip` (starts with `PK`).
4. Unzip → reconstruct with **ffmpeg**:
   - transcode each `screenshare_*.flv` → mp4, time-pad to its start offset, concat.
   - delay each `cameraVoip_*.flv` audio by its start offset, amix.
   - mux video + audio → final single `.mp4` (then playable at any speed).

Reference implementations studied (in `research/`):
- `amirhossein/` — minimal Python (zip method + ffmpeg-python reconstruct). Cleanest.
- `hosseinshams/` — C# WinForms, most complete (PDF/layout, V2 XML parser, asset zip).
- `sina/` — Python, per-university modules (handles LMS login for KNTU/UT/IUT/IKIU).

## Live probe results against the sample (2026-06-15)

| endpoint | result |
|---|---|
| `/<ID>/?session=<S>` | 200, but body now shows `loginField` → **session expired** |
| `/<ID>/output/<ID>.zip?download=zip` | 200 `text/html` "Not Authorized" (no zip) |
| `output/webrtc/recording.mp4` | same 23512-byte "Not Authorized" page |
| `output/indexstream.xml`, `mainstream.xml`, any asset | same "Not Authorized" page |

**Conclusion:** every asset path returns the identical "Not Authorized" page →
this is the **expired-session** response, NOT proof that download is disabled.
With a FRESH session (link opened/copied seconds before), the zip method is
expected to work — that is exactly the mechanism these SBU-targeting tools rely on.

## Open item to confirm with a live session
Whether SBU returns `application/zip` for `output/x.zip?download=zip` with a valid
session. If yes → straightforward. If "Not Authorized" even when fresh → server has
disabled source download; fallback would be RTMP capture (rtmpdump) — much harder.
</content>
