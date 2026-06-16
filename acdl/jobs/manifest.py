"""Job manifest: persisted JSON describing a download and its progress, enabling resume.

A job dir (e.g. downloads/<sco-id>/) holds:
    manifest.json   — url, host/sco, discovered streams, chunk list with per-chunk `done`
    chunks/         — on-disk chunk data (see store.py)
    <title>.mp4     — final output

On resume: load manifest, skip chunks already marked done, re-discover only if missing.
Status fields drive the UI: queued | downloading | paused | muxing | done | error.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json
import os


@dataclass
class Chunk:
    id: int
    stream: str
    start_s: float
    len_s: float
    done: bool = False


@dataclass
class Manifest:
    url: str
    host: str = ""
    sco: str = ""
    title: str | None = None
    duration_s: float = 0.0
    status: str = "queued"
    par: int = 24
    chunk_sec: int = 300
    streams: list[dict] = field(default_factory=list)   # [{name, type, start_ms}] — for muxing
    chunks: list[Chunk] = field(default_factory=list)
    path: str | None = None   # manifest.json path on disk

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = asdict(self)
        data.pop("path", None)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "Manifest":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        chunks = [Chunk(**c) for c in data.pop("chunks", [])]
        return cls(path=path, chunks=chunks, **data)
