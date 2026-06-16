"""Compose tracks into the final MP4 with ffmpeg.

Layout (docs/PROTOCOL.md §7):
  - Black base canvas for the full duration (covers gaps / pre-share intro / static screen).
  - Each screenshare segment scaled-padded onto the canvas at its global start, enabled over
    its active span.
  - Webcam: while a screen-share is active → small top-right picture-in-picture; while nothing
    is shared → full-frame (per the design "no PiP if there's no sharing").
  - Audio segments adelay'd to their global starts and amix'd.
Transcode to H.264/AAC.

Degenerate cases handled: no webcam → screenshare + audio; no screenshare → webcam full-frame
throughout; audio only → audio-only MP4.
"""
from __future__ import annotations
import subprocess

from .flv import Track

CANVAS_WITH_SCREEN = (1920, 1088)
CANVAS_WEBCAM_ONLY = (1280, 720)


def _share_intervals(videos: list[Track], duration: float):
    """Spans where a screen-share is active, and the complementary (no-share) spans."""
    shares = []
    for i, t in enumerate(videos):
        end = videos[i + 1].start_s if i + 1 < len(videos) else duration
        if end > t.start_s:
            shares.append((t.start_s, end))
    gaps, cur = [], 0.0
    for a, b in shares:
        if a > cur:
            gaps.append((cur, a))
        cur = max(cur, b)
    if cur < duration:
        gaps.append((cur, duration))
    return shares, gaps


def _enable(intervals) -> str:
    if not intervals:
        return "0"
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in intervals)


def compose(tracks: list[Track], output: str, duration_s: float, ffmpeg: str = "ffmpeg") -> None:
    videos = sorted([t for t in tracks if t.kind == "video"], key=lambda t: t.start_s)
    webcams = sorted([t for t in tracks if t.kind == "webcam"], key=lambda t: t.start_s)
    audios = sorted([t for t in tracks if t.kind == "audio"], key=lambda t: t.start_s)
    if not (videos or webcams or audios):
        raise RuntimeError("No media captured to compose.")

    W, H = CANVAS_WITH_SCREEN if videos else CANVAS_WEBCAM_ONLY
    duration_s = max(duration_s, 0.1)

    inputs: list[str] = []

    def add_in(path: str) -> int:
        i = len(inputs) // 2
        inputs.extend(["-i", path])
        return i

    vid_idx = [(add_in(t.path), t) for t in videos]
    cam_idx = [(add_in(t.path), t) for t in webcams]
    aud_idx = [(add_in(t.path), t) for t in audios]

    fc: list[str] = []
    vmap = amap = None

    if videos or webcams:
        fc.append(f"color=c=black:s={W}x{H}:r=15:d={duration_s:.3f}[base]")
        cur = "[base]"
        n = 0
        scalepad = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2")

        for k, (i, t) in enumerate(vid_idx):
            end = videos[k + 1].start_s if k + 1 < len(videos) else duration_s
            fc.append(f"[{i}:v]{scalepad},setpts=PTS-STARTPTS+{t.start_s:.3f}/TB[sv{k}]")
            out = f"[b{n+1}]"
            fc.append(f"{cur}[sv{k}]overlay=0:0:enable='between(t,{t.start_s:.3f},{end:.3f})'{out}")
            cur, n = out, n + 1

        if cam_idx:
            shares, gaps = _share_intervals(videos, duration_s)
            share_en, gap_en = _enable(shares), _enable(gaps)
            pip_w = W // 4
            for k, (i, t) in enumerate(cam_idx):
                sh = f"setpts=PTS-STARTPTS+{t.start_s:.3f}/TB"
                fc.append(f"[{i}:v]split=2[c{k}p][c{k}f]")
                fc.append(f"[c{k}p]scale={pip_w}:-2,{sh}[pip{k}]")
                fc.append(f"[c{k}f]{scalepad},{sh}[full{k}]")
                if shares:                       # PiP top-right while sharing
                    out = f"[b{n+1}]"
                    fc.append(f"{cur}[pip{k}]overlay=W-w-24:24:enable='{share_en}'{out}")
                    cur, n = out, n + 1
                if gaps:                         # full-frame when nothing shared
                    out = f"[b{n+1}]"
                    fc.append(f"{cur}[full{k}]overlay=(W-w)/2:(H-h)/2:enable='{gap_en}'{out}")
                    cur, n = out, n + 1

        fc.append(f"{cur}format=yuv420p[vout]")
        vmap = "[vout]"

    if aud_idx:
        for i, t in aud_idx:
            off = int(round(t.start_s * 1000))
            fc.append(f"[{i}:a]adelay={off}|{off}[a{i}]")
        if len(aud_idx) == 1:
            fc.append(f"[a{aud_idx[0][0]}]anull[aout]")
        else:
            fc.append("".join(f"[a{i}]" for i, _ in aud_idx)
                      + f"amix=inputs={len(aud_idx)}:normalize=0:dropout_transition=0[aout]")
        amap = "[aout]"

    cmd = [ffmpeg, "-y", "-loglevel", "warning", "-stats", *inputs, "-filter_complex", ";".join(fc)]
    if vmap:
        cmd += ["-map", vmap, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p"]
    if amap:
        cmd += ["-map", amap, "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", "-t", f"{duration_s:.3f}", output]
    subprocess.run(cmd, check=True)
