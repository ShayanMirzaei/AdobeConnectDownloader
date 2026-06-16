# Tasks & Roadmap

Milestone-organized backlog. Check items off as they land. This is the source of truth for
"what's next"; once the repo is on GitHub these graduate to Issues/Projects.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[?]` needs decision/research

---

## M0 — Foundation  *(in progress)*
- [x] Reverse-engineer the protocol (auth, gateway, parallel download, FLV, mux) — see `docs/PROTOCOL.md`
- [x] Prove link-only auth (no manual cookies needed)
- [x] Repo scaffold: `.gitignore` (secrets protected), README, LICENSE (MIT), `pyproject.toml`
- [x] Clean package skeleton (`acdl/core|media|jobs|ui`)
- [ ] Move prototypes (`acd.py`, `acd_fast.py`) into `prototype/` once `acdl/` reaches parity
- [ ] CI: lint + a smoke test (GitHub Actions) — after M1

## M1 — Solid headless core  ✅ (code-complete; full-length LIVE run pending good network)
- [x] `core/protocol.py` — frame parse, FLV tag/header, constants
- [x] `core/auth.py` — link → ticket + connect_url (link-only; cookie fallback)
- [x] `core/gateway.py` — WS connect, establish = fresh ticket + wait-for `accepted` + heartbeat + should_stop
- [x] `core/discover.py` — indexstream → inventory
- [x] `core/downloader.py` — parallel chunks (par=24), reconnect+resume, chunks streamed to DISK, progress/pause
- [x] `jobs/manifest.py` + `jobs/store.py` — job JSON + on-disk partial data (resume)
- [x] `cli.py` — `acdl <url> -o out.mp4 [--par] [--chunk] [--seconds] [--keep]`
- [x] `media/flv.py` — rebuild FLV per stream, split cameraVoip audio vs webcam video by frame type
- [x] `media/compose.py` — screenshare + audio → MP4; `ffmpeg.py` system-PATH detect
- [x] Validated end-to-end (auth→establish→discover→download→tracks→compose→MP4) — produced valid MP4
- [ ] Full-length clean run + a real resume cycle on good network (sandbox egress too slow to finish here)
- [ ] Formal unit tests: protocol parse, manifest resume, FLV stitch dedup

## M2 — Webcam picture-in-picture  ✅ (compose validated offline; live capture pending network)
- [x] Capture cameraVoip **video** (vp6) — explicit receiveVideo + split by frame type in `flv.py`
- [x] `compose.py`: webcam top-right PiP while sharing, full-frame when not; black base for gaps
- [x] Share-active intervals derived from screenshare spans → PiP↔fullscreen switch
- [x] Validated with synthetic video (region-color check: PASS)
- [ ] Verify on a REAL webcam recording (needs a webcam-on slice; network)
- [ ] Webcam on/off mid-recording: avoid frozen last frame during camera-off gaps

## M3 — App: UI + packaging  *(first "actually useful for normal people" release)*
- [x] `ui/server.py` — local web server (stdlib, zero deps) exposing job control
- [x] `ui/` front-end — paste-link box, download-manager cards, progress bars, pause/resume/remove
- [x] `jobs/manager.py` — background-loop JobManager driving downloads, live progress
- [x] Auth UX: link-only (no cookies in the common case)
- [x] UI validated over localhost (page, list, submit, lifecycle, remove)
- [ ] `ffmpeg.py` — per-OS locate/**download** with cached binary (only detect done so far)
- [ ] Manual-cookie fallback screen in the UI
- [ ] Packaging: PyInstaller builds per OS for Releases
- [ ] README install steps + screenshots

## M4 — Whiteboard renderer  *(hard, high value — many lectures are whiteboard-only)*
- [?] Decision: confirmed it is **vector draw data** on the `ftcontent1` content stream
      (`setWBSo`/`set_WB_So_*` SharedObjects: shapes w/ points, pages, text) — NOT a video stream
- [ ] Capture the content/whiteboard stream's SharedObject events with timestamps
- [ ] Parse WB model: pages, shapes (stroke/line/rect/ellipse/text/image), z-order, colors
- [ ] Render incrementally to a video track synced to playback time (incl. Persian text)
- [ ] Integrate as a video source like screenshare (whiteboard segments + screenshare segments)

## M5 — Polish & open-source
- [ ] Error UX (expired link, no streams, gateway flaky → clear messages)
- [ ] Edge cases: multiple screenshare segments, multiple webcams, audio-only recordings
- [ ] Docs, contributing guide, GitHub release with prebuilt binaries

---

## Known facts that constrain design (see docs/PROTOCOL.md)
- Establish is flaky per-attempt (~20-60% connect); **must** re-mint a fresh ticket each attempt
  and wait for `loginHandler: accepted` (not just `Connect.Success`).
- Sweet-spot parallelism = **24** on one connection (0 drops); higher over-drives → drops.
- Reconnect must also re-mint a fresh ticket; resume relies on per-chunk disk state.
- Nominal duration can exceed real content (trailing dead air) — not an error.
