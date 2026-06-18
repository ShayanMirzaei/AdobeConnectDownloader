"""Locate ffmpeg: prefer a system install, else download a static build for this OS into a
per-user cache.

Stdlib-only (urllib / zipfile / tarfile) so the app stays dependency-light. The binary itself
is large and platform-specific, so it is never committed — it is fetched once on first need:
  - Windows / Linux : BtbN static builds (GitHub Releases, the rolling `latest` tag)
  - macOS           : evermeet.cx (x86_64; runs natively on Intel and via Rosetta on Apple Silicon)
The download lands in a per-user cache dir, so it is reused across runs and survives upgrades.
"""
from __future__ import annotations
import logging
import os
import platform
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

log = logging.getLogger("acdl.ffmpeg")

_UA = "AdobeConnectDownloader (+https://github.com/ShayanMirzaei/AdobeConnectDownloader)"
_BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
_EVERMEET = "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip"

# (system, machine.lower()) -> download URL. Static, no-auth builds with a standalone binary.
_BUILDS = {
    ("Windows", "amd64"): f"{_BTBN}/ffmpeg-master-latest-win64-gpl.zip",
    ("Linux", "x86_64"): f"{_BTBN}/ffmpeg-master-latest-linux64-gpl.tar.xz",
    ("Linux", "aarch64"): f"{_BTBN}/ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
    ("Linux", "arm64"): f"{_BTBN}/ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
    ("Darwin", "x86_64"): _EVERMEET,
    ("Darwin", "arm64"): _EVERMEET,
}


def _bin_name() -> str:
    return "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"


def _cache_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "AdobeConnectDownloader", "ffmpeg")


def _cached() -> str | None:
    p = os.path.join(_cache_dir(), _bin_name())
    return p if os.path.isfile(p) else None


def _verify(path: str) -> bool:
    try:
        r = subprocess.run([path, "-version"], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def _urlopen(url: str):
    """Open a URL, falling back to an unverified TLS context if the local CA store is missing
    (common with python.org / frozen builds). Consistent with the gateway's existing posture."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        return urllib.request.urlopen(req, timeout=60)
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
        log.warning("  TLS certificate verification unavailable; retrying without verification")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=60, context=ctx)


def _download(url: str, dest: str) -> None:
    log.info("ffmpeg not found — downloading a static build (one-time, ~30-80 MB)…")
    with _urlopen(url) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = mark = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if got - mark >= (20 << 20):
                mark = got
                log.info("  …%d MB%s", got >> 20, f" / {total >> 20} MB" if total else "")


def _extract_ffmpeg(archive: str, into: str) -> str:
    """Extract the archive and return the path to the ffmpeg binary inside it."""
    if archive.endswith((".tar.xz", ".txz")):
        with tarfile.open(archive) as t:
            t.extractall(into)
    else:
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
    want = {"ffmpeg", "ffmpeg.exe"}
    for root, _dirs, files in os.walk(into):
        for fn in files:
            if fn in want:
                return os.path.join(root, fn)
    raise RuntimeError("ffmpeg binary not found inside the downloaded archive")


def _download_ffmpeg() -> str:
    key = (platform.system(), platform.machine().lower())
    url = _BUILDS.get(key)
    if not url:
        raise RuntimeError(f"no known static ffmpeg build for {key}")
    cache = _cache_dir()
    os.makedirs(cache, exist_ok=True)
    final = os.path.join(cache, _bin_name())
    with tempfile.TemporaryDirectory() as td:
        arc = os.path.join(td, "ffmpeg.tar.xz" if url.endswith(".tar.xz") else "ffmpeg.zip")
        _download(url, arc)
        src = _extract_ffmpeg(arc, os.path.join(td, "x"))
        shutil.copy2(src, final)
    os.chmod(final, os.stat(final).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not _verify(final):
        raise RuntimeError("the downloaded ffmpeg failed to run")
    log.info("ffmpeg ready: %s", final)
    return final


def find_ffmpeg() -> str:
    """Return a path to a usable ffmpeg binary: system PATH → per-user cache → auto-download."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    cached = _cached()
    if cached and _verify(cached):
        return cached
    try:
        return _download_ffmpeg()
    except Exception as e:
        raise RuntimeError(
            f"ffmpeg not found and the automatic download failed ({e}). Please install ffmpeg "
            "(https://ffmpeg.org/download.html) and make sure it is on your PATH."
        ) from e
