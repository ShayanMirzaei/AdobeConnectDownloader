"""Capture the whiteboard / content SharedObject events from the `ftcontent1` stream.

Unlike media, `play(ftcontent1, start=0, length=-1)` dumps the ENTIRE SharedObject history
as a fast burst (the recording-time of each event is in arg_0.time, not paced to wall-clock),
so this completes in seconds regardless of lecture length.

We bind each whiteboard event to its board: a `__registerSo__{soEventName:"setWBSo"}` names
the active board (e.g. "public/all/15_WB8"); the `setWBSo` / `set_WB_So_<page>` events that
follow belong to it until the next board registers. `setContentSo` gives the wb/screen share
intervals on the main stage. Output matches what media/whiteboard.py renders.
"""
from __future__ import annotations
import asyncio
import time
from typing import Optional


async def capture_content(gateway, nsid: str = "nsID_1", stream: str = "ftcontent1",
                          quiet_s: float = 4.0, max_s: float = 120.0,
                          on_log: Optional[callable] = None) -> dict:
    """Play the content stream and collect whiteboard SharedObject events until the burst ends."""
    events: list[dict] = []
    content: list[dict] = []
    native = {"w": 800, "h": 600}
    active_board: list[Optional[str]] = [None]
    last = [0.0]

    def sink(j: dict) -> None:
        p = j.get("params") or {}
        a1 = p.get("arg_1")
        a2 = p.get("arg_2")
        a0 = p.get("arg_0")
        t = a0.get("time") if isinstance(a0, dict) else None
        last[0] = time.monotonic()
        if a1 == "__registerSo__" and isinstance(a2, list):
            for x in a2:
                if isinstance(x, dict) and x.get("soEventName") == "setWBSo":
                    active_board[0] = x.get("soName")
        elif a1 == "setContentSo" and isinstance(a2, list):
            for x in a2:
                if not isinstance(x, dict):
                    continue
                nv = x.get("newValue")
                if isinstance(nv, dict) and nv.get("shareType") in ("wb", "screen"):
                    content.append({"t": t, "name": str(x.get("name")),
                                    "shareType": nv.get("shareType"), "ctID": nv.get("ctID")})
        elif a1 == "setWBSo" and isinstance(a2, list):
            for x in a2:
                if isinstance(x, dict) and x.get("name") == "nativeWidth":
                    native["w"] = x.get("newValue") or native["w"]
                if isinstance(x, dict) and x.get("name") == "nativeHeight":
                    native["h"] = x.get("newValue") or native["h"]
            events.append({"t": t, "evt": "setWBSo", "board": active_board[0], "changes": a2})
        elif isinstance(a1, str) and a1.startswith("set_WB_So_") and isinstance(a2, list):
            try:
                page = int(a1.rsplit("_", 1)[-1])
            except ValueError:
                page = 0
            events.append({"t": t, "evt": "wb", "board": active_board[0],
                           "page": page, "changes": a2})

    gateway.so_sink = sink
    last[0] = time.monotonic()
    try:
        await gateway.create_netstream(nsid)
        await gateway.play(nsid, stream, 0, -1, reset=3, media=False)
        start = time.monotonic()
        while time.monotonic() - start < max_s:
            await asyncio.sleep(0.5)
            if events and time.monotonic() - last[0] > quiet_s:
                break
    finally:
        gateway.so_sink = None

    if on_log:
        nb = sum(1 for b in {e["board"] for e in events} if b)
        on_log(f"  whiteboard: {len(events)} SO events across {nb} board(s)")
    return {"nativeWidth": native["w"], "nativeHeight": native["h"],
            "content": content, "events": events}
