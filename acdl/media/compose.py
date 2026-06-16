"""Compose tracks into the final MP4 with ffmpeg.

Layout (docs/PROTOCOL.md §7):
  - A black base canvas of the full duration (handles gaps / pre-share intro / static screen).
  - Screenshare video placed at its global start (setpts), overlaid on the base.
  - Audio segments adelay'd to their global starts, amix'd.
  - Webcam (M2): small top-right PiP WHILE a screen-share is active; FULL-FRAME when nothing
    is shared. Share-active intervals come from screenshare stream spans.
Transcode -c:v libx264 -c:a aac -> MP4.

Reference: acd.py mux() (single-video + adelay/amix; needs the PiP/fullscreen overlay logic).
"""
from __future__ import annotations


def compose(tracks, output: str, duration_s: float, ffmpeg: str) -> None:  # noqa: ANN001
    """Build and run the ffmpeg filter graph producing `output`."""
    raise NotImplementedError("M1: screenshare+audio; M2: add webcam PiP/fullscreen")
