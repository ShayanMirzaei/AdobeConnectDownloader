"""Rebuild per-track FLV files from downloaded chunk frames.

Per stream: walk chunks in start order; global ts = chunk_start_ms + relative_ts; keep only
strictly-increasing ts (drops the ~6s chunk overlap so seams aren't doubled).

IMPORTANT (M2): a cameraVoip stream carries BOTH audio (type 0x03) and webcam video
(type 0x04). Split by FRAME TYPE, not by stream type:
    screenshare         -> video track (vp6)
    cameraVoip audio    -> audio track (nellymoser)
    cameraVoip video    -> webcam video track (vp6)   [used for PiP]

Reference: acd_fast.py write_flvs (which currently lumps cameraVoip frames as audio —
the bug to fix here).
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Track:
    path: str
    kind: str        # 'video' | 'audio' | 'webcam'
    start_s: float   # global start time on the meeting timeline


def build_tracks(streams, store, workdir: str) -> list[Track]:  # noqa: ANN001
    """Reconstruct FLV files for all tracks (splitting cameraVoip by frame type)."""
    raise NotImplementedError("M1/M2: port + extend acd_fast.write_flvs")
