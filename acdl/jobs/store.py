"""On-disk chunk store: append media frames per chunk so progress survives a crash/restart.

Each chunk's frames are written to chunks/<chunk_id>.bin as length-prefixed records
(type, ts, payload). A chunk is only marked done in the manifest once fully received, so a
half-written chunk is simply re-downloaded on resume (cheap, seek lands on a keyframe).

This replaces the prototype's in-RAM frame lists and is what turns the downloader into a
resumable download manager.
"""
from __future__ import annotations
import os
import struct


class ChunkStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, chunk_id: int) -> str:
        return os.path.join(self.root, f"{chunk_id}.bin")

    def append(self, chunk_id: int, typ: int, ts: int, payload: bytes) -> None:
        with open(self._path(chunk_id), "ab") as f:
            f.write(struct.pack(">BII", typ, ts, len(payload)))
            f.write(payload)

    def read(self, chunk_id: int):
        """Yield (typ, ts, payload) records for a chunk."""
        p = self._path(chunk_id)
        if not os.path.exists(p):
            return
        with open(p, "rb") as f:
            while True:
                head = f.read(9)
                if len(head) < 9:
                    break
                typ, ts, n = struct.unpack(">BII", head)
                yield typ, ts, f.read(n)

    def reset(self, chunk_id: int) -> None:
        p = self._path(chunk_id)
        if os.path.exists(p):
            os.remove(p)
