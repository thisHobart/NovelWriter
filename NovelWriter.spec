# -*- mode: python ; coding: utf-8 -*-
"""Windows single-file desktop build: python -m PyInstaller NovelWriter.spec."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = collect_all("llm_backends")
hiddenimports += collect_submodules("core.config.genre_configs")

a = Analysis(
    [str(Path(SPECPATH) / "main.py")],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "PyQt5", "PyQt6", "PySide2",
              "torch", "tensorflow", "matplotlib", "IPython"],
    noarchive=False,
    optimize=0,
)
# Qt uses Windows' system ICU; Poppler's same-named DLL has incompatible exports.
a.binaries = [entry for entry in a.binaries
              if entry[0].lower() not in ("icuuc.dll", "icudt78.dll")]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="NovelWriter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
