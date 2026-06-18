# -*- mode: python ; coding: utf-8 -*-
"""One-file PyInstaller build of the AdobeConnectDownloader web UI.

Build from the repo root:  pyinstaller --clean packaging/AdobeConnectDownloader.spec
Produces dist/AdobeConnectDownloader (.exe on Windows). ffmpeg is NOT bundled — it is fetched
on first run (acdl/ffmpeg.py), keeping the binary small.
"""
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(SPECPATH)  # spec lives in packaging/, so ROOT is the repo root

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, "acdl", "ui", "static"), os.path.join("acdl", "ui", "static"))],
    hiddenimports=collect_submodules("acdl"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AdobeConnectDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
