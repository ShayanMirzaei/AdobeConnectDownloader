"""Headless CLI: `acdl <url> -o out.mp4`.

Pipeline: link-only auth → establish gateway → discover streams → parallel download
(disk-backed, resumable) → rebuild FLV tracks → ffmpeg compose → MP4.

Resumable: state lives in a job dir (default downloads/<sco>/). Re-running the same link
picks up where it left off. Pass --keep to retain the job dir (chunks/tracks) after success.
"""
from __future__ import annotations
import argparse
import asyncio
import os
import shutil

from . import applog
from .core.auth import AuthError, get_session_info
from .core.downloader import Downloader
from .ffmpeg import find_ffmpeg
from .jobs.manifest import Manifest
from .jobs.store import ChunkStore
from .media.compose import compose
from .media.flv import build_tracks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acdl", description="Download an Adobe Connect recording to MP4.")
    ap.add_argument("url", help="recording link, e.g. https://host/<id>/?session=<token>")
    ap.add_argument("-o", "--output", default="recording.mp4")
    ap.add_argument("--cookie", help="manual cookie header (fallback; usually unnecessary)")
    ap.add_argument("--par", type=int, default=24, help="concurrent chunks (default 24)")
    ap.add_argument("--chunk", type=int, default=300, help="seconds of content per chunk")
    ap.add_argument("--seconds", type=float, help="only download the first N seconds (testing)")
    ap.add_argument("--job-dir", help="where to keep resumable state (default downloads/<sco>)")
    ap.add_argument("--keep", action="store_true", help="keep the job dir after composing")
    args = ap.parse_args(argv)
    applog.setup()
    log = applog.get("acdl.cli")

    try:
        info = get_session_info(args.url, args.cookie)
    except AuthError as e:
        log.error("✗ %s", e)
        return 2

    log.info("✓ Authorized%s", f" as {info.user}" if info.user else "")
    if info.title:
        log.info("  Recording: %s", info.title)

    job_dir = args.job_dir or os.path.join("downloads", info.sco or "job")
    manifest_path = os.path.join(job_dir, "manifest.json")
    if os.path.exists(manifest_path):
        manifest = Manifest.load(manifest_path)
        log.info("  Resuming job in %s", job_dir)
    else:
        manifest = Manifest(url=args.url, host=info.host, sco=info.sco, title=info.title,
                            par=args.par, chunk_sec=args.chunk, path=manifest_path)
    store = ChunkStore(os.path.join(job_dir, "chunks"))

    mint = lambda: get_session_info(args.url, args.cookie)  # noqa: E731  (fresh ticket each call)
    dl = Downloader(mint, store, manifest, par=args.par, chunk_sec=args.chunk, seconds=args.seconds)
    try:
        asyncio.run(dl.run())
    except RuntimeError as e:
        log.error("✗ %s", e)
        return 1

    ffmpeg = find_ffmpeg()
    tracks = build_tracks(manifest.streams, manifest.chunks, store, os.path.join(job_dir, "tracks"))
    n_v = sum(1 for t in tracks if t.kind == "video")
    n_a = sum(1 for t in tracks if t.kind == "audio")
    n_w = sum(1 for t in tracks if t.kind == "webcam")
    log.info("Composing %d video + %d audio%s track(s)…", n_v, n_a,
             f" + {n_w} webcam (PiP)" if n_w else "")
    compose(tracks, args.output, manifest.duration_s, ffmpeg)

    manifest.status = "done"
    manifest.save()
    if not args.keep:
        shutil.rmtree(job_dir, ignore_errors=True)
    log.info("✅ Done → %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
