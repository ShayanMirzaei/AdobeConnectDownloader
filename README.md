# AdobeConnectDownloader

Download recorded Adobe Connect lectures (screen-share, webcam, audio — and eventually
whiteboard) to a normal `.mp4` you can watch, archive, or play at higher speed.

Built originally for Shahid Beheshti University (SBU) recordings, whose web player only
allows 1× playback and whose "download" button is disabled for students.

> **Status: early development.** The protocol is fully reverse-engineered and proven (see
> [`docs/PROTOCOL.md`](docs/PROTOCOL.md)); the clean app (`acdl/`) has a working engine, a
> download-manager web UI, webcam picture-in-picture, and a **whiteboard renderer** (vector
> draw data → synced video, incl. Persian handwriting). The original R&D prototypes and
> capture files are kept out of the repo in a local, gitignored `_attic/` folder.

## How it will work (goal)

1. Paste the recording link (e.g. `https://vc10.sbu.ac.ir/<id>/?session=<token>`).
2. The app authenticates from the link itself — **no cookies to copy** in the common case.
3. It downloads all streams in parallel (~24× realtime), composes them into one MP4
   (screen-share with the webcam as a corner picture-in-picture), and saves it.
4. A small download-manager UI shows progress and can **resume** if interrupted.

Cross-platform (Windows / macOS / Linux), self-contained, open source (MIT).

## Quick start (dev, from source)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
acdl "https://vc10.sbu.ac.ir/<id>/?session=<token>" -o lecture.mp4   # CLI (headless)
acdl-ui                                                              # local web UI
```

`ffmpeg` is used for muxing; the app finds a system install or fetches the right build
for your OS on first run.

## Architecture

```
acdl/
  core/      protocol: auth, gateway connection, stream discovery, parallel download
  media/     FLV reconstruction, ffmpeg composition (PiP), whiteboard rendering
  jobs/      resumable job manifest + on-disk chunk store (the download manager)
  ui/        local web UI + thin server bridging UI ↔ jobs
  cli.py     headless entry point
```

See [`TASKS.md`](TASKS.md) for the roadmap and [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for
how the Adobe Connect recording protocol actually works.

## Legal / ethical

Intended for downloading recordings **you are authorized to access** (e.g. your own
university courses). You are responsible for complying with your institution's terms.
