"""Local web server (M3): paste-link box + download-manager (list/progress/pause/resume).

Plan: a small FastAPI app + static front-end, launched in the user's browser. Endpoints
drive the jobs engine (create job from link, list jobs, progress stream, pause/resume).
Cross-platform; the `acdl-ui` console script calls main().
"""
from __future__ import annotations


def main() -> None:
    raise SystemExit("UI not built yet (TASKS.md M3). Use the `acdl` CLI for now.")
