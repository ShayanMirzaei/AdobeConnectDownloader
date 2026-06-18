"""Compose tracks into the final MP4 with ffmpeg.

Layout (docs/PROTOCOL.md §7):
  - Black base canvas for the full duration (covers gaps / pre-share intro / static screen).
  - The shared content (whiteboard / screen-share) is scaled-padded onto the canvas at its
    global start, enabled over its active span.
  - Audio segments adelay'd to their global starts and amix'd.
Transcode to H.264/AAC.

Webcam PiP is PARKED (INCLUDE_WEBCAM=False, see TASKS.md M2). When enabled, the stage splits
SIDE-BY-SIDE so the camera never hides the content: content fills a left region (~85% width)
and the webcam is a small box in the top-right corner (full-frame while nothing is shared). The
webcam is resampled to the base frame rate first (its native frames are sparse/bursty). That
code path is retained below but only runs when include_webcam=True.

Degenerate cases handled: no webcam → content + audio; no content → webcam full-frame
throughout (only when enabled); audio only → audio-only MP4.
"""
from __future__ import annotations
import subprocess

from .flv import Track

CANVAS_WITH_SCREEN = (1920, 1088)
CANVAS_WEBCAM_ONLY = (1280, 720)
FPS = 15                 # base canvas / output frame rate
PIP_FRAC = 0.12          # webcam width as a fraction of canvas width (small top-right corner)
PIP_MARGIN = 24          # webcam inset from the canvas edges
PIP_GAP = 24             # gap between the content region and the webcam column

# Webcam PiP is PARKED for now (see TASKS.md M2): capture is fixed, but compositing it into the
# final video has an unresolved overlay issue. Off by default — the layout/overlay code below is
# kept intact and re-enabled by flipping this (or passing include_webcam=True to compose()).
INCLUDE_WEBCAM = False


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


def compose(tracks: list[Track], output: str, duration_s: float, ffmpeg: str = "ffmpeg",
            include_webcam: bool = INCLUDE_WEBCAM) -> None:
    videos = sorted([t for t in tracks if t.kind == "video"], key=lambda t: t.start_s)
    webcams = (sorted([t for t in tracks if t.kind == "webcam"], key=lambda t: t.start_s)
               if include_webcam else [])      # webcam parked → content fills the canvas
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
        # Side-by-side stage when a webcam is present: content in a left region, camera in a
        # small top-right box that never overlaps it. No webcam → content fills the canvas.
        if cam_idx and videos:
            cam_w = (round(W * PIP_FRAC)) // 2 * 2
            cam_x = W - cam_w - PIP_MARGIN
            content_w = (cam_x - PIP_GAP - PIP_MARGIN) // 2 * 2
            content_x = PIP_MARGIN
        else:
            cam_w = (round(W * PIP_FRAC)) // 2 * 2
            content_w, content_x, cam_x = W, 0, W - cam_w - PIP_MARGIN

        def scalepad(w: int, h: int) -> str:
            return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")

        sp_content = scalepad(content_w, H)
        sp_full = scalepad(W, H)

        fc.append(f"color=c=black:s={W}x{H}:r={FPS}:d={duration_s:.3f}[base]")
        cur = "[base]"
        n = 0

        for k, (i, t) in enumerate(vid_idx):
            end = videos[k + 1].start_s if k + 1 < len(videos) else duration_s
            fc.append(f"[{i}:v]{sp_content},setpts=PTS-STARTPTS+{t.start_s:.3f}/TB[sv{k}]")
            out = f"[b{n+1}]"
            fc.append(f"{cur}[sv{k}]overlay={content_x}:0:enable='between(t,{t.start_s:.3f},{end:.3f})'{out}")
            cur, n = out, n + 1

        if cam_idx:
            shares, gaps = _share_intervals(videos, duration_s)
            share_en, gap_en = _enable(shares), _enable(gaps)
            for k, (i, t) in enumerate(cam_idx):
                # Resample to the base fps so the overlay advances frame-for-frame; the webcam's
                # own frames are sparse and bursty, which otherwise sticks on a stale frame.
                sh = f"setpts=PTS-STARTPTS+{t.start_s:.3f}/TB,fps={FPS}"
                # Build only the branches we actually overlay — a declared-but-unused split
                # output makes ffmpeg reject the whole graph (e.g. all-share → no full-frame).
                src_pip = src_full = f"[{i}:v]"
                if shares and gaps:
                    fc.append(f"[{i}:v]split=2[c{k}p][c{k}f]")
                    src_pip, src_full = f"[c{k}p]", f"[c{k}f]"
                if shares:                       # small top-right box, beside the content
                    fc.append(f"{src_pip}scale={cam_w}:-2,{sh}[pip{k}]")
                    out = f"[b{n+1}]"
                    fc.append(f"{cur}[pip{k}]overlay={cam_x}:{PIP_MARGIN}:enable='{share_en}'{out}")
                    cur, n = out, n + 1
                if gaps:                         # full-frame when nothing shared
                    fc.append(f"{src_full}{sp_full},{sh}[full{k}]")
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
