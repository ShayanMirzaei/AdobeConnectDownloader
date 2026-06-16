"""Stream discovery: play indexstream → inventory of all streams.

The index yields onMetaData.duration (seconds, NOMINAL — may exceed real content) and
playEvent.arg_2[] stream-added events: {streamName, streamType, startTime(ms)}.
Types: 'screenshare' (VP6 video), 'cameraVoip' (VP6 webcam + Nellymoser audio),
content/aux streams incl. the whiteboard (ftcontent1, see media/whiteboard.py).

Ported from acd_fast.py FastDownloader._open discovery block. Run once after establish;
streams persist on the Gateway across reconnects, so no need to re-discover.
"""
from __future__ import annotations
import asyncio
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


async def discover(gateway) -> Inventory:  # noqa: ANN001 (core.gateway.Gateway)
    """Drive indexstream playback and collect the inventory once the stream list stabilizes."""
    await gateway.create_netstream("nsID_0")
    await gateway.send({"type": "NCFunc", "method": "call", "method-name": "startLoadEditInfo",
                        "params": {}, "responderId": 1})
    await gateway.send({"type": "NCFunc", "method": "call", "method-name": "preloadStreams", "responderId": 2})
    await gateway.play("nsID_0", "indexstream", 0, -1, reset=3, media=False)
    await gateway.play("nsID_0", "indexstream", 0, 5, reset=2, media=False)

    last, stable = -1, 0
    for _ in range(40):
        await asyncio.sleep(0.5)
        n = len(gateway.streams)
        if n == last:
            stable += 1
        else:
            stable, last = 0, n
        if gateway.duration and stable >= 6 and n > 0:
            break
    if not gateway.duration:
        raise RuntimeError("index returned no duration (recording not ready / not authorized)")

    streams = [Stream(s["streamName"], s.get("streamType", ""), int(s.get("startTime", 0)))
               for s in gateway.streams.values()]
    streams.sort(key=lambda s: s.start_ms)
    return Inventory(duration_s=float(gateway.duration), streams=streams)
