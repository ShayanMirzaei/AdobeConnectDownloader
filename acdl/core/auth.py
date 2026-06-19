"""Authentication: turn a recording link into the ticket + connect URL the gateway needs.

Key fact (see docs/PROTOCOL.md §2): the `?session=<token>` in the link IS a BREEZESESSION
value, so the link is self-authenticating — we can mint the download ticket with NO manually
supplied cookies. We set BREEZESESSION=<token> only to also read the user's name (cosmetic).
A manual cookie string is accepted as a fallback for links that ever fail.
"""
from __future__ import annotations
import html
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .protocol import UA


class AuthError(Exception):
    """Raised when a ticket can't be obtained (expired link / not authorized)."""


@dataclass(frozen=True)
class SessionInfo:
    host: str
    ticket: str
    acct: str
    sco: str
    connect_url: str
    user: str | None = None
    title: str | None = None
    date: str | None = None   # recording date YYYY-MM-DD (best-effort; for ordering)


def token_from_url(url: str) -> str | None:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    vals = qs.get("session") or qs.get("token")
    return vals[0] if vals else None


def _http_get(url: str, cookie: str, timeout: int = 30) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    headers = {"User-Agent": UA, "Origin": "https://" + url.split("/")[2]}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode("utf-8", "replace")


def _recording_date(host: str, sco: str, cookie: str) -> str | None:
    """Best-effort recording date (YYYY-MM-DD) via the Connect XML API, for file ordering.

    Never fatal — any failure just yields None and the caller falls back to add-order.
    """
    if not sco:
        return None
    try:
        xml = _http_get(f"https://{host}/api/xml?action=sco-info&sco-id={sco}", cookie, timeout=15)
    except Exception:
        return None
    for tag in ("date-begin", "date-created", "date-modified"):
        m = re.search(rf"<{tag}>\s*(\d{{4}}-\d{{2}}-\d{{2}})", xml)
        if m:
            return m.group(1)
    return None


def get_session_info(url: str, cookie: str | None = None) -> SessionInfo:
    """Resolve a recording URL to a SessionInfo (fresh ticket each call).

    cookie:
        None  -> link-only auth (derive BREEZESESSION from the URL's ?session= token)
        str   -> explicit cookie header (manual fallback)
    """
    host = urllib.parse.urlparse(url).netloc
    if not cookie:
        token = token_from_url(url)
        cookie = f"BREEZESESSION={token}" if token else ""

    user = None
    try:  # cosmetic only — never fatal
        who = _http_get(f"https://{host}/api/xml?action=common-info", cookie)
        m = re.search(r"<name>([^<]*)</name>", who)
        user = m.group(1) if m else None
    except Exception:
        pass

    page = _http_get(url, cookie)
    dec = urllib.parse.unquote(urllib.parse.unquote(page))
    mt = re.search(r"ticket=([A-Za-z0-9]+)", dec)
    ma = re.search(r"appInstance=([0-9]+)/([0-9]+-1)/output", dec)
    if not (mt and ma):
        raise AuthError(
            "Couldn't get a ticket — the link is likely expired. "
            "Open the recording in your browser to refresh it, then copy the link again."
        )
    acct, sco = ma.groups()
    title = None
    mtitle = re.search(r"<title>([^<]*)</title>", dec)
    if mtitle:
        title = html.unescape(mtitle.group(1).strip())
    connect_url = f"rtmp://{host}:1935/?rtmp://localhost:8506/flvplayeras3app/{acct}/{sco}/output/"
    date = _recording_date(host, sco, cookie)
    return SessionInfo(host=host, ticket=mt.group(1), acct=acct, sco=sco,
                       connect_url=connect_url, user=user, title=title, date=date)
