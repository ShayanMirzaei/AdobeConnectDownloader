"""Gateway connection: one WebSocket to wss://<host>:1443/, established the RIGHT way.

Establish protocol (docs/PROTOCOL.md §3) — the hard-won part:
  1. WS upgrade (NO Cookie header).
  2. WSFunc startHeartbeat/allowPacketDrop/fragmentVideoPacket(false).
  3. NCFunc connect{ticket,...}; wait onStatus NetConnection.Connect.Success.
  4. WAIT for onCommand {arg_0:{command:"accepted"}} — meeting registration. Until this
     arrives, all stream ops are silently ignored.
Two failure modes: reused ticket -> Connect.Success but never 'accepted' (ticket is
single-use for registration), and Connect.Success itself is flaky (~20-60%/try). So the
caller's establish loop MUST re-mint a fresh ticket every attempt and retry fast.
Keepalive: send NCFunc call @getStats every ~5s or the gateway drops at ~90-130s.

Reference implementation to port: acd_fast.py (FastDownloader._open / _establish / _heartbeat).
"""
from __future__ import annotations
from typing import Awaitable, Callable

from .auth import SessionInfo


class Gateway:
    """One live gateway connection. Emits parsed MediaFrames to a sink callback."""

    def __init__(self, on_frame: Callable[[object], None]):
        self.on_frame = on_frame
        raise NotImplementedError("M1: port from acd_fast.py")

    async def open(self, info: SessionInfo) -> None:
        """Connect + connect-RPC + wait Connect.Success + wait 'accepted' + start keepalive.
        Raises on any timeout so the establish loop retries with a fresh ticket."""
        raise NotImplementedError

    async def establish(self, mint: "Callable[[], Awaitable[SessionInfo]]", attempts: int = 60) -> bool:
        """Retry open() with a FRESH SessionInfo (mint()) each attempt until registered."""
        raise NotImplementedError

    async def send(self, obj: dict) -> None: ...
    async def create_netstream(self, nsid: str, timeout: float = 8.0) -> None: ...
    async def close(self) -> None: ...
