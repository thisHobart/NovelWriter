"""Novel writing and short video are two features of one app: neither imports the other.

Both may use the common layer, which knows neither of them. Only the app shell (main window
and launcher) puts the features together, and only through each feature's entry module.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SHORT_VIDEO = ("core/short_video", "core/gui/pages/short_video")
COMMON = (
    "core/common",
    "core/utils",
    "core/gui/__init__.py",
    "core/gui/feature.py",
    "core/gui/icons.py",
    "core/gui/input_behavior.py",
    "core/gui/model_config_dialog.py",
    "core/gui/task_runner.py",
    "core/gui/theme.py",
    "core/gui/widgets",
)
SHELL = ("main.py", "core/gui/app.py", "core/gui/main_window.py")
# Scripts that start the whole app and use it like a person would; they may open the shell.
DRIVERS = ("core/evaluation/lore_acceptance_probe.py",)
SOURCES = ("main.py", "core", "agents", "tools")
# The shell may reach into a feature only here.
ENTRIES = {
    "short_video": ("core.gui.pages.short_video.workbench",),
    "novel": ("core.gui.pages.novel.workspace",),
}


def area_of_path(relative: str) -> str:
    def under(prefixes):
        return any(relative == p or relative.startswith(p.rstrip("/") + "/") for p in prefixes)

    if under(SHORT_VIDEO):
        return "short_video"
    if under(SHELL):
        return "shell"
    if under(COMMON):
        return "common"
    return "novel"


def module_path(module: str) -> str | None:
    """The repo-relative file or package a dotted module name refers to, if it is ours."""
    base = ROOT.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate.relative_to(ROOT).as_posix()
    return None


def imports_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
    if path.name == "__init__.py":
        package = ".".join(path.relative_to(ROOT).parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join(part for part in [*parts, node.module or ""] if part)
            else:
                base = node.module or ""
            yield base
            for alias in node.names:
                yield f"{base}.{alias.name}"


def crossings():
    found = set()
    for source in SOURCES:
        start = ROOT / source
        files = [start] if start.is_file() else start.rglob("*.py")
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            mine = area_of_path(relative)
            for module in imports_of(path):
                target = module_path(module)
                if target is None:
                    continue
                theirs = area_of_path(target)
                if theirs == mine or theirs == "common":
                    continue
                if mine == "shell" and module in ENTRIES.get(theirs, ()):
                    continue
                if relative in DRIVERS and theirs == "shell":
                    continue
                name = target.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
                found.add((relative, name))
    return found


def test_features_only_meet_in_the_common_layer_and_the_shell_entries():
    found = crossings()
    assert found == set(), "coupling between features:\n" + "\n".join(
        sorted(f"{a} -> {b}" for a, b in found)
    )
