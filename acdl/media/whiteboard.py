"""Whiteboard rendering (M4).

The whiteboard is NOT a media stream — it is vector draw data delivered as SharedObject
updates on the content stream `ftcontent1` (captured by `core/content.py` into a
`whiteboard.json`). Each shared whiteboard ("board") owns pages; each page holds shapes
keyed by an ever-increasing id, ordered by `depth`:

    {"type":"pencil", "pts":[{x,y},...], "x","y","width","height",   # bbox in native coords
     "strokeCol": <0xRRGGBB int>, "strokeWeight": <native px>, "alpha", "htmlText"}

`pts` are normalised to [0,1] inside the shape's bounding box, so an absolute native point
is (x + px*width, y + py*height) on the board's nativeWidth×nativeHeight canvas (e.g. 800×600).
A change with newValue=dict adds/updates a shape; newValue=null deletes it (eraser / undo).

We rasterise each board to a 0-based H.264 video spanning its on-stage interval, which
`compose` then treats as a main-stage video source exactly like a screen-share segment
(webcam stays a PiP over it). Rendering is incremental: we only fully repaint when a delete
happens; pure stroke appends are drawn onto the running canvas. Snapshots are emitted on
change and encoded with ffmpeg's concat demuxer (per-frame durations) → constant-fps video.
"""
from __future__ import annotations
import html as _html
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - Pillow is a declared dependency
    Image = None

log = logging.getLogger("acdl.whiteboard")

MIN_FRAME_MS = 80          # merge changes closer than this (≈12 fps ceiling for handwriting)
DEFAULT_TARGET_W = 1280    # render width; height follows the board's native aspect


@dataclass
class WBSegment:
    path: str
    start_s: float
    end_s: float
    board: str


def load_capture(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _rgb(col) -> tuple[int, int, int]:
    try:
        c = int(col)
    except (TypeError, ValueError):
        return (0, 0, 0)
    return ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)


def _board_events(wb: dict) -> dict[str, list]:
    """Group whiteboard ('wb'/'setWBSo') events by board, each sorted by time."""
    by_board: dict[str, list] = {}
    for e in wb.get("events", []):
        if e.get("evt") not in ("wb", "setWBSo"):
            continue
        by_board.setdefault(e.get("board"), []).append(e)
    for evs in by_board.values():
        evs.sort(key=lambda e: (e.get("t") if e.get("t") is not None else 0))
    return by_board


def _ops(events: list) -> list[tuple]:
    """Flatten board events into chronological shape ops: (t_ms, 'add'|'del', id, shape|None)."""
    ops = []
    for e in events:
        t = e.get("t") or 0
        for ch in e.get("changes", []):
            if not isinstance(ch, dict):
                continue
            name, nv, ov = ch.get("name"), ch.get("newValue"), ch.get("oldValue")
            if not (isinstance(name, str) and name.isdigit()):
                continue                      # bookkeeping (tID, lastShapeOnPage, …) — ignore
            if isinstance(nv, dict) and "type" in nv:
                ops.append((t, "add", name, nv))
            elif nv is None and isinstance(ov, dict):
                ops.append((t, "del", name, None))
    ops.sort(key=lambda o: o[0])
    return ops


def board_intervals(wb: dict, other_starts: list[float], duration_s: float) -> list[tuple[str, float, float]]:
    """Each board's on-stage interval [start_s, end_s).

    A board is shown from its first event until the next main-stage source begins (another
    board or a screen-share, given via `other_starts`) — or the recording end.
    """
    by_board = _board_events(wb)
    starts = {}
    for board, evs in by_board.items():
        ts = [e["t"] for e in evs if e.get("t") is not None]
        if board and ts:
            starts[board] = min(ts) / 1000.0
    board_starts = sorted(starts.values())
    boundaries = sorted(set(board_starts) | set(other_starts) | {duration_s})
    out = []
    for board, s in sorted(starts.items(), key=lambda kv: kv[1]):
        s = 0.0 if s < 1.0 else s            # a board present from the first frame starts at 0
        nxt = next((b for b in boundaries if b > s + 0.5), duration_s)
        out.append((board, s, max(s, nxt)))
    return out


def _draw_shape(draw: "ImageDraw.ImageDraw", shape: dict, scale: float, font) -> None:
    nx, ny = shape.get("x", 0.0), shape.get("y", 0.0)
    nw, nh = shape.get("width", 0.0), shape.get("height", 0.0)
    pts = shape.get("pts") or []
    col = _rgb(shape.get("strokeCol", 0))
    w = max(1, round(shape.get("strokeWeight", 1) * scale))
    abs_pts = [((nx + p.get("x", 0.0) * nw) * scale, (ny + p.get("y", 0.0) * nh) * scale)
               for p in pts if isinstance(p, dict)]

    text = shape.get("htmlText")
    if text:
        txt = _html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if txt and font is not None:
            draw.text(((nx) * scale, (ny) * scale), txt, fill=col, font=font)
        return

    if not abs_pts:
        return
    if len(abs_pts) == 1:
        x, y = abs_pts[0]
        r = max(1, w / 2)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=col)
        return
    draw.line(abs_pts, fill=col, width=w, joint="curve")
    r = w / 2                                  # round caps/joins so strokes don't look chiselled
    if r >= 1:
        for x, y in abs_pts:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=col)


def _render_board(events: list, start_s: float, end_s: float, native_w: float, native_h: float,
                  out_path: str, ffmpeg: str, target_w: int, fps: int) -> bool:
    """Rasterise one board to a 0-based video covering [0, end_s-start_s]. Returns False if empty."""
    ops = _ops(events)
    if not ops:
        return False
    scale = target_w / max(1.0, native_w)
    W, H = target_w, max(1, round(native_h * scale))
    start_ms, dur_ms = start_s * 1000.0, max(1.0, (end_s - start_s) * 1000.0)

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", max(10, round(18 * scale)))
    except Exception:
        font = ImageFont.load_default()

    work = os.path.dirname(out_path)
    frames_dir = os.path.join(work, "_wb_" + re.sub(r"\W+", "", os.path.basename(out_path)))
    os.makedirs(frames_dir, exist_ok=True)

    shapes: dict[str, dict] = {}               # id -> shape (visible set)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    def repaint() -> None:
        draw.rectangle([0, 0, W, H], fill=(255, 255, 255))
        for _id, sh in sorted(shapes.items(), key=lambda kv: kv[1].get("depth", 0)):
            _draw_shape(draw, sh, scale, font)

    # group ops by (clamped) snapshot time
    frames: list[tuple[float, str]] = []       # (rel_ms, png_path)
    i, n, fidx = 0, len(ops), 0
    last_t = None
    pending_added: list[dict] = []
    pending_del = False
    while i <= n:
        t = ops[i][0] if i < n else None
        # flush a snapshot when the timestamp advances past the merge window (or at the end)
        if last_t is not None and (t is None or t - last_t >= MIN_FRAME_MS):
            if pending_del:
                repaint()
            else:
                for sh in pending_added:
                    _draw_shape(draw, sh, scale, font)
            rel = max(0.0, last_t - start_ms)
            png = os.path.join(frames_dir, f"f{fidx:05d}.png")
            canvas.save(png)
            frames.append((rel, png))
            fidx += 1
            pending_added, pending_del = [], False
        if i == n:
            break
        _t, kind, sid, sh = ops[i]
        if kind == "add":
            shapes[sid] = sh
            pending_added.append(sh)
        else:
            if shapes.pop(sid, None) is not None:
                pending_del = True
        last_t = _t
        i += 1

    if not frames:
        return False
    # prepend a blank/preloaded frame at t=0 if the first change starts later
    if frames[0][0] > 0:
        blank = Image.new("RGB", (W, H), (255, 255, 255))
        p0 = os.path.join(frames_dir, "f_init.png")
        blank.save(p0)
        frames.insert(0, (0.0, p0))

    # concat demuxer with per-frame durations -> constant-fps H.264
    list_path = os.path.join(frames_dir, "list.txt")
    with open(list_path, "w") as f:
        f.write("ffconcat version 1.0\n")
        for k, (rel, png) in enumerate(frames):
            nxt = frames[k + 1][0] if k + 1 < len(frames) else dur_ms
            d = max(0.04, (nxt - rel) / 1000.0)
            f.write(f"file '{os.path.basename(png)}'\nduration {d:.3f}\n")
        f.write(f"file '{os.path.basename(frames[-1][1])}'\n")   # repeat last (concat quirk)

    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
           "-vf", f"fps={fps},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "20", "-t", f"{dur_ms/1000.0:.3f}", os.path.abspath(out_path)]
    subprocess.run(cmd, check=True, cwd=frames_dir)
    return True


def whiteboard_video_tracks(wb_json_path: str, video_starts: list[float], duration_s: float,
                            out_dir: str, ffmpeg: str = "ffmpeg",
                            target_w: int = DEFAULT_TARGET_W, fps: int = 12) -> list:
    """Render captured whiteboards and return them as compose `Track`s (kind='video').

    `video_starts` = start_s of the screen-share segments, so each board's interval ends when
    the next main-stage source begins. Returns [] when there's no capture / no draw events.
    """
    from .flv import Track
    if not wb_json_path or not os.path.exists(wb_json_path):
        return []
    wb = load_capture(wb_json_path)
    if not wb.get("events"):
        return []
    segs = render_whiteboard(wb, video_starts, duration_s, out_dir, ffmpeg, target_w, fps)
    return [Track(path=s.path, kind="video", start_s=s.start_s) for s in segs]


def render_whiteboard(wb: dict, other_starts: list[float], duration_s: float, out_dir: str,
                      ffmpeg: str = "ffmpeg", target_w: int = DEFAULT_TARGET_W,
                      fps: int = 12) -> list[WBSegment]:
    """Render every shared whiteboard to a video segment for `compose`.

    `other_starts` = start_s of the other main-stage sources (screen-shares) so each board's
    on-stage interval ends when the next source begins.
    """
    if Image is None:
        raise RuntimeError("Pillow is required for whiteboard rendering (pip install pillow).")
    os.makedirs(out_dir, exist_ok=True)
    native_w = float(wb.get("nativeWidth") or 800)
    native_h = float(wb.get("nativeHeight") or 600)
    by_board = _board_events(wb)
    segments: list[WBSegment] = []
    for board, s, e in board_intervals(wb, other_starts, duration_s):
        out_path = os.path.join(out_dir, "wb_" + re.sub(r"\W+", "_", board) + ".mp4")
        log.info("  whiteboard %s → %.0f..%.0fs", board, s, e)
        if _render_board(by_board[board], s, e, native_w, native_h, out_path, ffmpeg, target_w, fps):
            segments.append(WBSegment(path=out_path, start_s=s, end_s=e, board=board))
    return segments
