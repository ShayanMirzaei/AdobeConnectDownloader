"""Stream discovery: play indexstream → inventory of all streams.

The index yields onMetaData.duration (seconds, NOMINAL — may exceed real content) and
playEvent.arg_2[] stream-added events: {streamName, streamType, startTime(ms)}.
Types: 'screenshare' (VP6 video), 'cameraVoip' (VP6 webcam + Nellymoser audio),
content/aux streams incl. the whiteboard (ftcontent1, see media/whiteboard.py).

Reference: acd_fast.py FastDownloader._open discovery block.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Stream:
    name: str
    type: str            # 'screenshare' | 'cameraVoip' | 'ftcontent1' | ...
    start_ms: int


@dataclass
class Inventory:
    duration_s: float
    streams: list[Stream] = field(default_factory=list)

    @property
    def media(self) -> list[Stream]:
        return [s for s in self.streams if s.type in ("cameraVoip", "screenshare")]


async def discover(gateway) -> Inventory:  # noqa: ANN001 (Gateway)
    """Drive indexstream playback and collect the inventory once the list stabilizes."""
    raise NotImplementedError("M1: port from acd_fast.py")
