# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
hiddenimports = collect_submodules("autoeditor")

a = Analysis(
    [str(root / "studio_engine.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "config" / "default.yaml"), "config"),
        (str(root / "config" / "references.yaml"), "config"),
        (str(root / "assets"), "assets"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="autoeditor-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
