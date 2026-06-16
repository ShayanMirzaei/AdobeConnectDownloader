"""Headless CLI entry point: `acdl <url> -o out.mp4`.

Right now this resolves the recording from a link (link-only auth — no cookies needed) and
prints what it found. The parallel download engine is being ported in M1 (see TASKS.md);
until then it points you at the proven prototype `acd_fast.py`.
"""
from __future__ import annotations
import argparse
import sys

from .core.auth import AuthError, get_session_info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acdl", description="Download an Adobe Connect recording to MP4.")
    ap.add_argument("url", help="recording link, e.g. https://host/<id>/?session=<token>")
    ap.add_argument("-o", "--output", default="recording.mp4")
    ap.add_argument("--cookie", help="manual cookie header (fallback; usually unnecessary)")
    ap.add_argument("--par", type=int, default=24, help="concurrent chunks (default 24)")
    ap.add_argument("--chunk", type=int, default=300, help="seconds of content per chunk")
    args = ap.parse_args(argv)

    try:
        info = get_session_info(args.url, args.cookie)
    except AuthError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2

    print(f"✓ Authorized{f' as {info.user}' if info.user else ''}")
    if info.title:
        print(f"  Recording: {info.title}")
    print(f"  Host: {info.host}   SCO: {info.sco}")
    print(f"  Ticket minted (fresh). connect_url ready.")
    print()
    print("Download engine is being ported (TASKS.md M1). For now use the proven prototype:")
    print(f'  python3 acd_fast.py "{args.url}" --par {args.par} --chunk {args.chunk} -o {args.output}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
