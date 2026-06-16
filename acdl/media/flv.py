"""Rebuild per-track FLV files from downloaded chunk frames (read from the ChunkStore).

Per stream: walk its chunks in start order; global ts = chunk_start_ms + relative_ts; keep
only strictly-increasing ts (drops the ~6s chunk overlap so seams aren't doubled).

Split by FRAME TYPE, not stream type (a cameraVoip stream carries BOTH):
    screenshare        -> 'video'  track (vp6)
    cameraVoip audio   -> 'audio'  track (nellymoser)
    cameraVoip video   -> 'webcam' track (vp6)   [used for PiP in M2]
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass

from ..core.protocol import AUDIO, FLV_TAG_AUDIO, FLV_TAG_VIDEO, flv_header, flv_tag


@dataclass
class Track:
    path: str
    kind: str        # 'video' | 'audio' | 'webcam'
    start_s: float   # global start time on the meeting timeline


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name.strip("/"))


def build_tracks(streams: list[dict], chunks, store, workdir: str) -> list[Track]:
    """streams: [{name,type,start_ms}]; chunks: list[Manifest.Chunk]; store: ChunkStore."""
    os.makedirs(workdir, exist_ok=True)
    by_stream: dict[str, list] = {}
    for c in chunks:
        by_stream.setdefault(c.stream, []).append(c)

    tracks: list[Track] = []
    for s in streams:
        name, stype = s["name"], s["type"]
        if stype not in ("screenshare", "cameraVoip"):
            continue
        cs = sorted(by_stream.get(name, []), key=lambda c: c.start_s)

        buckets: dict[str, list] = {"video": [], "audio": [], "webcam": []}
        last: dict[str, int] = {"video": -1, "audio": -1, "webcam": -1}
        for c in cs:
            off = int(round(c.start_s * 1000))
            for ftyp, ts, payload in sorted(store.read(c.id), key=lambda r: r[1]):
                g = off + ts
                if stype == "screenshare":
                    kind = "video"
                else:
                    kind = "audio" if ftyp == AUDIO else "webcam"
                if g <= last[kind]:
                    continue
                buckets[kind].append((g, payload))
                last[kind] = g

        start_s = s["start_ms"] / 1000.0
        for kind, frames in buckets.items():
            if not frames:
                continue
            is_video = kind in ("video", "webcam")
            path = os.path.join(workdir, f"{_sanitize(name)}_{kind}.flv")
            with open(path, "wb") as f:
                f.write(flv_header(has_audio=not is_video, has_video=is_video))
                tag = FLV_TAG_VIDEO if is_video else FLV_TAG_AUDIO
                for g, payload in frames:
                    f.write(flv_tag(tag, g, payload))
            tracks.append(Track(path=path, kind=kind, start_s=start_s))
    return tracks
