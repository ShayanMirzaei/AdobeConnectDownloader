"""Wire-level protocol primitives: binary media-frame parsing and FLV tag/header building.

Frame on the wire (binary WS message):
    [1B type][4B BE timestamp ms][4B BE nsIdLen][nsId ascii][FLV tag body]
    type 0x03 = audio (Nellymoser 0x6a, 22050 mono)
    type 0x04 = video (VP6 vp6f; body[0] 0x14 = keyframe, 0x24 = interframe)
"""
from __future__ import annotations
import struct
from dataclasses import dataclass

# Browser-identical UA — the gateway does not gate on it, but we match the browser anyway.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

AUDIO = 0x03
VIDEO = 0x04
FLV_TAG_AUDIO = 8
FLV_TAG_VIDEO = 9


@dataclass(frozen=True)
class MediaFrame:
    typ: int          # AUDIO or VIDEO
    ts: int           # timestamp in ms (relative to the play() start for seeked chunks)
    nsid: str         # NetStream id the frame arrived on
    payload: bytes    # FLV tag body


def parse_bin(raw: bytes) -> MediaFrame | None:
    """Parse one binary WS media frame. Returns None if it isn't an audio/video frame."""
    if not raw or raw[0] not in (AUDIO, VIDEO):
        return None
    typ = raw[0]
    ts = struct.unpack(">I", raw[1:5])[0]
    nlen = struct.unpack(">I", raw[5:9])[0]
    nsid = raw[9:9 + nlen].decode("ascii", "replace")
    return MediaFrame(typ, ts, nsid, raw[9 + nlen:])


def flv_header(has_audio: bool, has_video: bool) -> bytes:
    flags = (4 if has_audio else 0) | (1 if has_video else 0)
    return b"FLV" + bytes([1, flags]) + struct.pack(">I", 9) + struct.pack(">I", 0)


def flv_tag(tag_type: int, ts: int, payload: bytes) -> bytes:
    """Build one FLV tag. tag_type: 8 audio, 9 video. ts in ms."""
    size = len(payload)
    ts = int(ts)
    return (bytes([tag_type])
            + struct.pack(">I", size)[1:]                 # 3-byte data size
            + struct.pack(">I", ts & 0xFFFFFF)[1:]         # 3-byte timestamp (low)
            + bytes([(ts >> 24) & 0xFF])                   # 1-byte timestamp (high)
            + b"\x00\x00\x00"                              # 3-byte stream id (0)
            + payload
            + struct.pack(">I", size + 11))                # prev tag size
