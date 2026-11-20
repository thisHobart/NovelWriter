"""How the app window and its two features (novel writing, short video) talk to each other.

The window shows one feature at a time above the shared status bar. It knows a feature only
through ``Feature``, and a feature knows the window only through ``FeatureHost``, so either
feature can change on its own as long as it keeps these few promises.
"""

from __future__ import annotations

from typing import Protocol

from PySide6.QtWidgets import QWidget

from core.gui.widgets.status_bar import AppStatusBar


class FeatureHost(Protocol):
    """What the window offers a feature."""

    status: AppStatusBar

    def open_feature(self, name: str) -> None:
        """Switch to the named feature (``"novel"`` or ``"video"``), as 「短视频」 does."""

    def model_settings_saved(self) -> None:
        """Model-service addresses or keys were saved; every feature updates what it shows."""


class Feature(Protocol):
    """What the window needs from a feature; the feature itself is the widget it shows.

    ``side_panel`` is shown at the window's left edge while the feature is open, or ``None``.
    """

    side_panel: QWidget | None

    @property
    def busy(self) -> bool:
        """Any of the feature's tasks still runs; the window then refuses to switch away."""

    def enter(self) -> None:
        """Shown again: go back to its last page and refresh."""

    def can_leave(self) -> bool:
        """Before switching to the other feature: settle unsaved edits, ``False`` to stay."""

    def can_close(self) -> bool:
        """Before the window closes: settle unsaved edits, ``False`` to keep it open."""

    def cancel_selected(self, channel: str | None) -> None:
        """「停止」 in the status bar, for the task picked there (``channel``)."""

    def cancel_all(self) -> None:
        """The window closes while tasks run."""

    def model_settings_changed(self) -> None:
        """Model-service settings were saved, by this feature or the other one."""
