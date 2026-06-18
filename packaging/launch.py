"""PyInstaller entry point for the packaged app — launches the local web UI.

Kept as a top-level script (not a relative import) so PyInstaller analyses it cleanly; the
real logic lives in acdl.ui.server.main().
"""
from acdl.ui.server import main

if __name__ == "__main__":
    main()
