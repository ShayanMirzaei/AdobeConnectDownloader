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

## M1 — Solid headless core  *(port the proven prototype, cleanly)*
- [ ] `core/protocol.py` — frame parse, FLV tag/header, constants  *(port from acd.py)*
- [ ] `core/auth.py` — link → ticket + connect_url (link-only; cookie fallback)  *(port; mostly done in R&D)*
- [ ] `core/gateway.py` — WS connect, establish = **fresh ticket + wait-for `accepted`** + heartbeat
- [ ] `core/discover.py` — indexstream → full inventory (media + content/whiteboard streams)
- [ ] `core/downloader.py` — parallel chunks (par=24), reconnect+resume, **chunks streamed to DISK**
- [ ] `jobs/manifest.py` + `jobs/store.py` — job state JSON + on-disk partial data (enables resume)
- [ ] `cli.py` — `acdl <url> -o out.mp4 [--par] [--chunk]`
- [ ] `media/flv.py` — rebuild FLV per stream, **split cameraVoip audio vs webcam video by frame type**
- [ ] `media/compose.py` — screenshare → MP4 (no PiP yet); detect-or-download ffmpeg (`ffmpeg.py`)
- [ ] Tests: protocol parse, manifest resume logic, FLV stitch dedup

## M2 — Webcam picture-in-picture
- [ ] Capture cameraVoip **video** (vp6) alongside its audio
- [ ] `compose.py`: webcam as small **top-right PiP while a screen-share is active**,
      **full-frame when nothing is shared** (per design); base canvas handles gaps/static frames
- [ ] Determine share-active intervals from stream metadata to switch PiP↔fullscreen
- [ ] Handle webcam on/off mid-recording (don't freeze last frame on screen)

## M3 — App: UI + packaging  *(first "actually useful for normal people" release)*
- [ ] `ui/server.py` — local web server exposing job control (start/pause/resume/list)
- [ ] `ui/` front-end — paste-link box, download-manager list, progress, output location
- [ ] Auth UX: link-only, with a clear walkthrough + manual-cookie fallback screen
- [ ] `ffmpeg.py` — robust per-OS locate/download with cached binary
- [ ] Packaging: runnable from source on Win/macOS/Linux; PyInstaller builds per OS for Releases
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
