# AdobeConnectDownloader

Download recorded Adobe Connect lectures (screen-share, whiteboard, and audio) to a normal
`.mp4` you can watch, archive, or play at higher speed.

Built originally for Shahid Beheshti University (SBU) recordings, whose web player only
allows 1× playback and whose "download" button is disabled for students.

> **Status: early development.** The protocol is fully reverse-engineered and proven (see
> [`docs/PROTOCOL.md`](docs/PROTOCOL.md)). The app (`acdl/`) has a working parallel engine, a
> resumable download-manager web UI, and a **whiteboard renderer** (the lecturer's vector
> drawing → a synced video track, including Persian handwriting). The current output is
> **whiteboard + screen-share + audio**; webcam picture-in-picture is built but
> [parked](TASKS.md) (off by default) pending a compositing fix. R&D prototypes and capture
> files are kept out of the repo in a local, gitignored `_attic/` folder.

## How it works

1. Paste the recording link (e.g. `https://vc10.sbu.ac.ir/<id>/?session=<token>`).
2. The app authenticates from the link itself — **no cookies to copy** in the common case.
3. It downloads the streams in parallel (~24× realtime), captures the whiteboard's vector draw
   events, composes everything into one MP4 (the shared content as it changes over time, in
   sync with the audio), and saves it.
4. A small download-manager UI shows progress and can **resume** if interrupted.

Cross-platform (Windows / macOS / Linux), self-contained, open source (MIT).

## Download & run (no Python needed)

Grab the binary for your OS from the [**Releases**](../../releases) page and run it:

- **Windows** — `AdobeConnectDownloader-windows-x64.exe` (double-click)
- **macOS** — `AdobeConnectDownloader-macos-arm64`
- **Linux** — `AdobeConnectDownloader-linux-x64`

It opens a small web page in your browser; paste the recording link and download. The first
launch fetches `ffmpeg` automatically (one-time, ~30–80 MB) and may take a few seconds to
start up. No Python, no install.

## Run from source (developers)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
acdl "https://vc10.sbu.ac.ir/<id>/?session=<token>" -o lecture.mp4   # CLI (headless)
acdl-ui                                                              # local web UI
```

`ffmpeg` is used for muxing; the app uses a system install if present, otherwise downloads a
static build for your OS on first run (cached per-user, never committed).

## Build a binary

```bash
pip install . "pyinstaller>=6"
pyinstaller --clean packaging/AdobeConnectDownloader.spec   # -> dist/AdobeConnectDownloader[.exe]
```

PyInstaller can't cross-compile, so each OS's binary is built on that OS — the
[release workflow](.github/workflows/release.yml) does this on GitHub's Windows/macOS/Linux
runners and attaches the results to a Release when you push a `v*` tag.

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
