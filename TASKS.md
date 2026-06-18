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
- [x] Move prototypes + R&D (`acd.py`, `acd_fast.py`, `research/`, HARs) into gitignored `_attic/`
- [x] File logging → `logs/acdl.log` (rotating) + console (`acdl/applog.py`)
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

## M2 — Webcam picture-in-picture  ⏸ PARKED (off by default; capture solved, compositing not)
Toggle: `compose.INCLUDE_WEBCAM` + `downloader.CAPTURE_WEBCAM` (both False). Code kept intact.
- [x] Capture cameraVoip **video** (vp6) — split by frame type in `flv.py`
- [x] **Fixed VP6 capture corruption**: a seeked play() opens with a *priming burst* (keyframe +
      interframes ALL at ts=0) the decoder needs; the old strictly-increasing-ts dedup dropped it
      → garbage. `flv.py` now keeps every video frame in arrival order and trims the seam by
      cutting each chunk at the next chunk's start. Verified clean decode across seams on a real
      webcam-on recording (sharp frames of the presenter).
- [x] `compose.py` side-by-side layout (content left ~85%, small camera top-right) so the camera
      never hides the writing — per user; validated the *content shrink* works.
- [ ] **BLOCKER (why it's parked):** the webcam overlay comes out blank in the FULL compose graph
      even though (a) the webcam track decodes to a clean person and (b) an isolated overlay onto a
      gray base shows it. The fault is specific to the webcam overlay *after the chained content
      overlays*; a hand-built equivalent chain (T2) showed the webcam, so the trigger is still
      unidentified. `fps`-resampling the webcam did not fix it.
- [ ] Camera on/off mid-recording: blank/held frame handling during camera-off gaps
- [ ] Re-verify side-by-side end-to-end once the overlay blocker is fixed

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

## M4 — Whiteboard renderer  ✅ (live-captured + rendered; validated against real recording)
- [x] Decision: confirmed it is **vector draw data** on the `ftcontent1` content stream
      (`setWBSo`/`set_WB_So_*` SharedObjects: shapes w/ points, pages, text) — NOT a video stream
- [x] `core/content.py` — play `ftcontent1` (fast burst, not realtime-paced) → capture
      `setWBSo`/`set_WB_So_*`/`setContentSo`/`__registerSo__` with `arg_0.time`; bind each event to
      its board via the active `__registerSo__`. Wired into `downloader` (saved as `whiteboard.json`).
- [x] Parse WB model in `media/whiteboard.py`: per-board/page shapes keyed by id, depth z-order,
      adds vs deletes (eraser/undo), `strokeCol`/`strokeWeight`, `pts` normalised in bbox.
- [x] Rasterise with Pillow (incremental draw; full repaint only on delete) → per-board 0-based
      H.264 via ffmpeg concat demuxer; smooth Persian/Latin handwriting confirmed.
- [x] Integrate as main-stage video sources over their on-stage intervals (alongside screenshare),
      webcam stays PiP (`whiteboard_video_tracks` → `compose`). Fixed a compose filtergraph bug
      (unused webcam split output when share intervals cover the whole timeline).
- [ ] htmlText (typed text boxes) is best-effort only — no complex-text shaping yet (no `raqm`);
      this recording had zero text shapes so it's untested. Bundle a Persian font + reshaper later.
- [ ] Multi-page boards (`set_WB_So_1`, `_2`, …): parsing handles page index but only page 0 seen
      in captures so far — verify on a multi-page lecture.

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
