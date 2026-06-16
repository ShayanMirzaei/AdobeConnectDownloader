"""Whiteboard rendering (M4) — the hard one.

The whiteboard is NOT a media stream. It is vector draw data delivered as SharedObject
updates on the content stream `ftcontent1`:
    __registerSo__ "setWBSo" / "set_WB_So_*"   with shareType:"wb"
    shapes: {pts:[{x,y},...], alpha, depth, currentPage, htmlText, height, ...}
To render it we must:
  1. Capture the content stream's SharedObject change events WITH timestamps.
  2. Build a model: pages -> ordered shapes (stroke/line/rect/ellipse/text/image), z by depth.
  3. Rasterize incrementally to a video track synced to playback time (incl. Persian text).
  4. Hand the result to compose() as a video source like screenshare.

Status: not started. See TASKS.md M4. This module currently only documents the shape format.
"""
from __future__ import annotations


def render_whiteboard(events, out_flv: str, duration_s: float, size=(1280, 720)) -> str:  # noqa: ANN001
    raise NotImplementedError("M4: parse WB SharedObjects and rasterize to video")
