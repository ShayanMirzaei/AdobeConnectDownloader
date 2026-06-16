"""Locate ffmpeg: prefer a system install, else download the right build for this OS.

Never committed to the repo (Windows/macOS/Linux builds differ and are large). On first
need we look on PATH, then in a per-user cache dir, then fetch a static build matching
platform.system()/machine() into that cache.
"""
from __future__ import annotations
import shutil


def find_ffmpeg() -> str:
    """Return a path to a usable ffmpeg binary (system PATH for now; auto-download in M3)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError(
        "ffmpeg not found. Install it (https://ffmpeg.org/download.html) or wait for the "
        "app's auto-download (TASKS.md M3)."
    )
