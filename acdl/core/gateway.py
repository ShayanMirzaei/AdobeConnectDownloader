"""Gateway connection: one WebSocket to wss://<host>:1443/, established the RIGHT way.

Establish protocol (docs/PROTOCOL.md §3):
  1. WS upgrade (NO Cookie header).
  2. WSFunc startHeartbeat / allowPacketDrop / fragmentVideoPacket(false).
  3. NCFunc connect{ticket,...}; wait onStatus NetConnection.Connect.Success.
  4. WAIT for onCommand {arg_0:{command:"accepted"}} — meeting registration. Until this
     arrives, all stream ops are silently ignored.
Failure modes: reused ticket -> Connect.Success but never 'accepted' (ticket is single-use
for registration); Connect.Success itself is flaky (~20-60%/try). So establish() re-mints a
FRESH SessionInfo every attempt and retries fast. Keepalive: @getStats every ~5s.

The reader handles protocol JSON internally (connect/accepted/created/index/status) and
forwards binary media frames to `media_sink`; stream-stop events go to `status_sink`.
Ported from acd_fast.py (FastDownloader._open/_establish/_heartbeat/_reader).
"""
from __future__ import annotations
import asyncio
import json
import ssl
import time
from typing import Callable, Optional

import websockets

from .auth import SessionInfo
from .protocol import UA, parse_bin

MediaSink = Callable[[str, int, int, bytes], None]   # (nsid, typ, ts, payload)
StatusSink = Callable[[str, str], None]              # (nsid, status_code)
SoSink = Callable[[dict], None]                      # raw playEvent JSON (SharedObject updates)

# SharedObject events we forward for whiteboard capture (see media/whiteboard.py).
_SO_EVENTS = {"__registerSo__", "setContentSo", "setWBSo"}


class Gateway:
    def __init__(self) -> None:
        self.ws = None
        self.connected = asyncio.Event()
        self.logged_in = asyncio.Event()
        self.created: dict[str, asyncio.Event] = {}
        self.streams: dict[str, dict] = {}     # discovered once; persists across reconnects
        self.duration: Optional[float] = None
        self.ws_closed = False
        self.last_frame_t = 0.0
        self.media_sink: Optional[MediaSink] = None
        self.status_sink: Optional[StatusSink] = None
        self.so_sink: Optional[SoSink] = None
        self._rt: Optional[asyncio.Task] = None
        self._hb: Optional[asyncio.Task] = None

    def _reset_conn(self) -> None:
        self.connected = asyncio.Event()
        self.logged_in = asyncio.Event()
        self.created = {}
        self.ws_closed = False
        self._hb = None

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj))

    async def create_netstream(self, nsid: str, timeout: float = 8.0) -> None:
        self.created.setdefault(nsid, asyncio.Event())
        await self.send({"type": "NCFunc", "method": "createNetStream", "nsId": nsid, "mediaAvailable": False})
        await asyncio.wait_for(self.created[nsid].wait(), timeout)

    async def play(self, nsid: str, stream: str, start: float, length: float,
                   reset: int = 1, media: bool = True) -> None:
        await self.send({"type": "NSFunc", "method": "play", "nsId": nsid, "streamName": stream,
                         "start": start, "length": length, "reset": reset, "mediaAvailable": media})

    async def _heartbeat(self) -> None:
        rid = 1000
        try:
            while True:
                await asyncio.sleep(5)
                rid += 1
                await self.send({"type": "NCFunc", "method": "call", "method-name": "@getStats", "responderId": rid})
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ws_closed = True

    async def _reader(self) -> None:
        try:
            while True:
                msg = await self.ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    fr = parse_bin(msg)
                    if fr and self.media_sink:
                        self.media_sink(fr.nsid, fr.typ, fr.ts, fr.payload)
                        self.last_frame_t = time.monotonic()
                    continue
                j = json.loads(msg)
                st = j.get("status") or {}
                code = st.get("code")
                desc = st.get("description")
                if code == "NetConnection.Connect.Success":
                    self.connected.set()
                if j.get("method") == "onCommand":
                    a0 = (j.get("params") or {}).get("arg_0")
                    if isinstance(a0, dict) and a0.get("command") == "accepted":
                        self.logged_in.set()
                if desc == "StreamCreated":
                    self.created.setdefault(j.get("nsId"), asyncio.Event()).set()
                if code and self.status_sink and ("Play.Stop" in code or "Play.Complete" in code
                                                  or "Play.UnpublishNotify" in code):
                    self.status_sink(j.get("nsId"), code)
                cmd = j.get("cmdString")
                if cmd == "onMetaData":
                    self.duration = j["params"]["arg_0"].get("duration")
                if cmd == "playEvent" and isinstance(j["params"].get("arg_2"), list):
                    for s in j["params"]["arg_2"]:
                        if isinstance(s, dict) and s.get("streamName"):
                            self.streams.setdefault(s["streamName"], s)
                if cmd == "playEvent" and self.so_sink:
                    a1 = j["params"].get("arg_1")
                    if a1 in _SO_EVENTS or (isinstance(a1, str) and a1.startswith("set_WB_So_")):
                        self.so_sink(j)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ws_closed = True

    async def open(self, info: SessionInfo) -> None:
        """Open one primed, registered connection. Raises on any timeout so establish() retries."""
        self._reset_conn()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.ws = await websockets.connect(
            f"wss://{info.host}:1443/", ssl=ctx, origin=f"https://{info.host}",
            additional_headers={"User-Agent": UA}, compression=None, max_size=None, open_timeout=20)
        self._rt = asyncio.create_task(self._reader())
        await self.send({"type": "WSFunc", "method": "startHeartbeat", "value": True})
        await self.send({"type": "WSFunc", "method": "allowPacketDrop", "value": False})
        await self.send({"type": "WSFunc", "method": "fragmentVideoPacket", "value": False})
        await self.send({"type": "NCFunc", "method": "connect", "url": info.connect_url,
                         "params": {"ticket": info.ticket, "reconnection": False,
                                    "swfUrl": f"https://{info.host}/common/meetinghtml/index.html",
                                    "Recording": True}})
        await asyncio.wait_for(self.connected.wait(), 5)
        await asyncio.wait_for(self.logged_in.wait(), 5)   # meeting registration — the crucial gate
        self._hb = asyncio.create_task(self._heartbeat())

    async def establish(self, mint: Callable[[], SessionInfo], attempts: int = 60,
                        on_retry: Optional[Callable[[int, int], None]] = None,
                        should_stop: Optional[Callable[[], bool]] = None) -> bool:
        """Re-mint a FRESH SessionInfo each attempt and retry fast until one registers.
        Returns False if exhausted or should_stop() becomes true (caller checks which)."""
        for attempt in range(attempts):
            if should_stop and should_stop():
                return False
            info = None
            try:
                info = await asyncio.to_thread(mint)
            except Exception:
                pass
            if info is not None:
                try:
                    await self.open(info)
                    return True
                except Exception:
                    await self.close()
            if on_retry and (attempt + 1) % 5 == 0:
                on_retry(attempt + 1, attempts)
            await asyncio.sleep(1.0)
        return False

    async def close(self) -> None:
        for t in (self._hb, self._rt):
            try:
                if t:
                    t.cancel()
            except Exception:
                pass
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
