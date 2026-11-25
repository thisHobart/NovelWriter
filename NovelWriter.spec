# -*- mode: python ; coding: utf-8 -*-
"""Windows single-file desktop build: python -m PyInstaller NovelWriter.spec."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

datas, binaries, hiddenimports = collect_all("llm_backends")
datas += [(str(Path(SPECPATH) / "core/short_video/v2/skills/short-video-content-storyboard/SKILL.md"),
           "core/short_video/v2/skills/short-video-content-storyboard")]
datas += [(str(Path(SPECPATH) / "core/short_video/v2/workflows/minimax_h3_reference.json"),
           "core/short_video/v2/workflows")]
datas += [(str(Path(SPECPATH) / "core/short_video/v2/workflows/minimax_h3_first_last.json"),
           "core/short_video/v2/workflows")]
datas += [(str(Path(SPECPATH) / "core/short_video/v2/workflows/scail2_dual_scene.json"),
           "core/short_video/v2/workflows")]
hiddenimports += collect_submodules("mcp")
datas += copy_metadata("mcp") + copy_metadata("mcp-types")
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
