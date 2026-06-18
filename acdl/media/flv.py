"""Rebuild per-track FLV files from downloaded chunk frames (read from the ChunkStore).

Per stream: walk its chunks in start order; global ts = chunk_start_ms + relative_ts.

Split by FRAME TYPE, not stream type (a cameraVoip stream carries BOTH):
    screenshare        -> 'video'  track (vp6)
    cameraVoip audio   -> 'audio'  track (nellymoser)
    cameraVoip video   -> 'webcam' track (vp6)   [used for PiP in M2]

Seam handling differs by codec:
  * Audio (Nellymoser) frames are independent — keep strictly-increasing ts, which drops the
    ~MARGIN-second chunk overlap so seams aren't doubled.
  * Video (VP6) frames are NOT independent. A seeked play() opens with a *priming burst*: a
    keyframe plus several interframes ALL stamped ts=0, which the decoder needs to reconstruct
    the image at the seek point. Dropping same-ts frames (as a naive ts-dedup does) strips that
    burst and the stream decodes to garbage. So we keep EVERY video frame in arrival order and
    instead trim the overlap by cutting each chunk at the next chunk's start — every chunk then
    re-primes with its own keyframe, giving a clean seam. ts is made monotonic only for muxing.
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
        for idx, c in enumerate(cs):
            off = int(round(c.start_s * 1000))
            cutoff = int(round(cs[idx + 1].start_s * 1000)) if idx + 1 < len(cs) else None
            for ftyp, ts, payload in store.read(c.id):     # arrival = decode order
                g = off + ts
                if stype == "screenshare":
                    kind = "video"
                else:
                    kind = "audio" if ftyp == AUDIO else "webcam"
                # Video: keep all frames in [chunk_start, next_chunk_start) — the next chunk
                # re-primes with its own keyframe, so we just drop the tail overlap here.
                if kind != "audio" and cutoff is not None and g >= cutoff:
                    continue
                buckets[kind].append((g, payload))

        start_s = s["start_ms"] / 1000.0
        for kind, frames in buckets.items():
            if not frames:
                continue
            is_video = kind in ("video", "webcam")
            path = os.path.join(workdir, f"{_sanitize(name)}_{kind}.flv")
            with open(path, "wb") as f:
                f.write(flv_header(has_audio=not is_video, has_video=is_video))
                tag = FLV_TAG_VIDEO if is_video else FLV_TAG_AUDIO
                if is_video:
                    last_g = -1
                    for g, payload in frames:               # keep order; force monotonic ts
                        gg = g if g > last_g else last_g + 1
                        f.write(flv_tag(tag, gg, payload))
                        last_g = gg
                else:
                    last_g = -1
                    for g, payload in sorted(frames, key=lambda r: r[0]):
                        if g <= last_g:                     # independent frames: drop overlap
                            continue
                        f.write(flv_tag(tag, g, payload))
                        last_g = g
            tracks.append(Track(path=path, kind=kind, start_s=start_s))
    return tracks
