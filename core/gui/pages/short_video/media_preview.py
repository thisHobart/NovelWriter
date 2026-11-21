"""Video generation for one storyboard shot: reference images, video prompt, the video card with
its parameters, and 「生成视频」 pinned at the bottom."""

from __future__ import annotations

import random
import re
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from core.common.locks import project_lock
from core.gui import icons, theme
from core.gui.theme import CONTROL_HEIGHT, SP
from core.gui.widgets.controls import VScrollArea, primary_button
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import MediaService, MediaWork
from core.short_video.v2.media_workflows import (
    MAX_REFERENCE_IMAGES,
    ImageInput,
    MediaConfig,
    parse_video_workflow,
)
from core.short_video.v2.models import WorkspaceError, fingerprint, require
from core.short_video.v2.repository import ProjectRepository
from core.short_video.v2.video_context import picture_name
from core.short_video.v2.video_prompt import snapshot_video_inputs
from core.short_video.v2.workflow import WorkflowService, WorkspaceWork

from .chat_stream import reveal
from .video_widgets import (
    WIDGET_STYLE,
    AddSlotButton,
    ColumnGrid,
    Dot,
    Fold,
    SlotCard,
    SlotGrid,
    SplitBody,
    Switch,
    mini_button,
    muted,
    section_title,
    set_tone,
)

if TYPE_CHECKING:
    from .workbench import Workbench

DEFAULT_VIDEO_PROMPT = (
    "以 <Picture 1> 为画面参考，保持主体外观、环境与构图一致，"
    "生成自然连贯的动态画面和轻微镜头运动。无文字、无水印。"
)
KIND_LABELS = {"scene": "场景", "character": "人物", "prop": "道具"}
THUMBNAIL_WIDTH = 480
JOB_STATES = {
    "prepared": "准备中",
    "submitting": "提交中",
    "queued": "正在排队",
    "running": "正在生成",
    "collecting": "正在保存结果",
    "succeeded": "视频已保存",
    "failed": "生成失败",
    "unknown": "远程状态待确认",
    "interrupted": "已停止本地等待，远程可能仍在运行",
    "cancelled": "已取消",
}
RUNNING = {"prepared", "submitting", "queued", "running", "collecting"}

MEDIA_STYLE = f"""
#videoPage {{ background:{theme.SURFACE}; }}
#videoCard {{ background:{theme.CANVAS}; border:1px solid {theme.LINE_STRONG};
    border-radius:{theme.RADIUS_CARD}px; }}
#videoFoot {{ background:{theme.CANVAS}; border-top:1px solid {theme.LINE}; }}
QLabel#videoPreview {{ border-radius:{theme.RADIUS_CARD}px; font-size:{theme.FS_SM}px;
    background:{theme.SURFACE}; border:1px dashed {theme.LINE_STRONG}; color:{theme.INK_400}; padding:0 {SP[4]}px; }}
QLabel#videoPreview[tone="run"] {{ background:#2B3138; border:0; color:#E3E6EA; }}
QLabel#videoPreview[tone="ok"] {{ background:{theme.tint(theme.SUCCESS)}; border:0; color:{theme.SUCCESS}; }}
QLabel#videoPreview[tone="bad"] {{ background:{theme.tint(theme.DANGER)}; border:1px dashed {theme.DANGER};
    color:{theme.DANGER}; }}
QLabel#videoPreview[tone="warn"] {{ background:{theme.tint(theme.WARN)}; border:0; color:{theme.WARN}; }}
QLabel#promptHint {{ font-size:{theme.FS_SM}px; color:{theme.INK_600}; }}
QLabel#promptHint[tone="warn"] {{ color:{theme.WARN}; }}
QLabel#promptHint[tone="run"] {{ color:{theme.ACCENT}; }}
QLabel#videoStatus {{ font-size:{theme.FS_SM}px; color:{theme.INK_600}; }}
QLabel#videoStatus[tone="ok"] {{ color:{theme.SUCCESS}; }}
QLabel#videoStatus[tone="bad"] {{ color:{theme.DANGER}; }}
QLabel#videoStatus[tone="run"] {{ color:{theme.ACCENT}; }}
QLabel#videoStatus[tone="warn"] {{ color:{theme.WARN}; }}
QLabel#fieldLabel {{ font-size:{theme.FS_SM}px; color:{theme.INK_600}; }}
QPushButton#small {{ height:28px; padding:0 10px; }}
"""

PICKER_STYLE = f"""
QMenu {{ background:{theme.CANVAS}; border:1px solid {theme.LINE_STRONG}; padding:4px; }}
QMenu::item {{ padding:4px 10px 4px 8px; border-radius:4px; }}
QMenu::item:selected {{ background:{theme.ACCENT_TINT}; color:{theme.INK_900}; }}
QMenu::item:disabled {{ color:{theme.INK_400}; }}
QMenu::separator {{ height:1px; background:{theme.LINE}; margin:4px 0; }}
QMenu::icon {{ padding-left:6px; }}
QPushButton#pickName {{ height:30px; border:0; border-radius:4px; background:transparent;
    text-align:left; padding:0 8px; }}
QPushButton#pickName:hover {{ background:{theme.ACCENT_TINT}; }}
QToolButton#pickRemove {{ border:0; border-radius:3px; background:transparent; }}
QToolButton#pickRemove:hover {{ background:{theme.tint(theme.DANGER)}; }}
"""


def small_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("small")
    button.setFixedHeight(28)
    return button


def field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


class AspectLabel(QLabel):
    """16:9 box, at most 360 wide, standing in for the video."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("videoPreview")
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setMaximumWidth(360)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt 命名
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt 命名
        return max(96, round(min(width, 360) * 9 / 16))


class PickRow(QWidget):
    """A project image in the picker: thumbnail and name to choose it, × to drop it from lists."""

    chosen = Signal()
    removed = Signal()

    def __init__(self, name: str, pixmap: QPixmap | None) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(2)
        self.pick = QPushButton(name)
        self.pick.setObjectName("pickName")
        self.pick.setCursor(Qt.PointingHandCursor)
        if pixmap is not None and not pixmap.isNull():
            self.pick.setIcon(QIcon(pixmap))
            self.pick.setIconSize(pixmap.size().scaled(38, 22, Qt.KeepAspectRatio))
        self.pick.clicked.connect(self.chosen)
        layout.addWidget(self.pick, 1)
        self.remove = QToolButton()
        self.remove.setObjectName("pickRemove")
        self.remove.setIcon(icons.icon("close", theme.INK_400, 10))
        self.remove.setFixedSize(22, 22)
        self.remove.setToolTip("从列表移除，图片文件保留")
        self.remove.clicked.connect(self.removed)
        layout.addWidget(self.remove)
        self.setMinimumWidth(280)


class MediaPreview(QWidget):
    inputs_changed = Signal()

    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench = workbench
        self.current_job = None
        self.target = {}
        self.shot_drafts = {}
        self.video_prompt_fingerprint = None
        self.video_prompt_binding = None
        self.video_path = None
        self.project_dir = None
        self.shot_assets: list[dict] = []
        self.shot_duration_ms: int | None = None
        self.thumbnails: dict[str, tuple[float, QPixmap]] = {}
        self.image_fields: dict[str, SlotCard] = {}
        self.additional_reference_slots = []
        self.prompt_image_bindings = {}
        self.parameter_fields = {}
        self.drafts = {}
        self.loaded_workflow_id = None
        self.setStyleSheet(WIDGET_STYLE + MEDIA_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.settings_area = VScrollArea()
        page = QWidget()
        page.setObjectName("videoPage")
        page.setAttribute(Qt.WA_StyledBackground, True)
        self.page_layout = QVBoxLayout(page)
        self.page_layout.setContentsMargins(18, 14, 18, 18)
        self.page_layout.setSpacing(0)
        self.header_slot = QVBoxLayout()
        self.header_slot.setSpacing(0)
        self.page_layout.addLayout(self.header_slot)

        # Reference images and first/last frames.
        self.slots_section = QWidget()
        slots_layout = QVBoxLayout(self.slots_section)
        slots_layout.setContentsMargins(0, 0, 0, 0)
        slots_layout.setSpacing(SP[2])
        head = QHBoxLayout()
        head.setSpacing(SP[2])
        head.addWidget(section_title("参考图片"))
        self.slots_note = muted()
        head.addWidget(self.slots_note)
        head.addStretch()
        slots_layout.addLayout(head)
        self.slot_grid = SlotGrid()
        slots_layout.addWidget(self.slot_grid)
        self.frames_head = QWidget()
        frames_head = QHBoxLayout(self.frames_head)
        frames_head.setContentsMargins(0, SP[2], 0, 0)
        frames_head.setSpacing(SP[2])
        frames_head.addWidget(section_title("首尾帧"))
        frames_head.addWidget(muted("不占 Picture 编号"))
        frames_head.addStretch()
        slots_layout.addWidget(self.frames_head)
        self.frame_grid = SlotGrid()
        slots_layout.addWidget(self.frame_grid)
        self.add_image_button = AddSlotButton()
        # In the grid from the start: shown or hidden before that, it would be its own window.
        self.slot_grid.set_cells([self.add_image_button])

        # Video prompt.
        self.prompt_section = QWidget()
        prompt_layout = QVBoxLayout(self.prompt_section)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        prompt_layout.setSpacing(SP[2])
        prompt_bar = QHBoxLayout()
        prompt_bar.setSpacing(SP[2])
        prompt_bar.addWidget(section_title("视频提示词"))
        # Wraps rather than widening the left column, so the video card fits beside it.
        prompt_note = muted("用 <Picture N> 指代上面的参考图片")
        prompt_note.setWordWrap(True)
        prompt_bar.addWidget(prompt_note, 1)
        self.generate_prompt_button = small_button("AI 编写")
        self.resume_prompt_button = small_button("继续编写")
        self.copy_prompt_button = small_button("复制")
        for button in (self.generate_prompt_button, self.resume_prompt_button, self.copy_prompt_button):
            prompt_bar.addWidget(button)
        prompt_layout.addLayout(prompt_bar)
        self.prompt = QPlainTextEdit()
        self.prompt.setMinimumHeight(200)
        self.prompt.textChanged.connect(self.capture_prompt_image_bindings)
        prompt_layout.addWidget(self.prompt)
        self.prompt_status = QLabel()
        self.prompt_status.setObjectName("promptHint")
        self.prompt_status.setWordWrap(True)
        prompt_layout.addWidget(self.prompt_status)
        self.prompt_shift = QLabel(
            "图片编号已变化：提示词里的 <Picture N> 可能指向了别的图片，核对后再生成。"
        )
        self.prompt_shift.setObjectName("promptHint")
        self.prompt_shift.setProperty("tone", "warn")
        self.prompt_shift.setWordWrap(True)
        self.prompt_shift.hide()
        prompt_layout.addWidget(self.prompt_shift)

        # Room for the page's own sections (the shot's image prompts).
        self.extra = QWidget()
        self.extra_slot = QVBoxLayout(self.extra)
        self.extra_slot.setContentsMargins(0, 0, 0, 0)
        self.extra.hide()

        self.build_video_card()
        self.columns = ColumnGrid(
            self.slots_section, self.prompt_section, self.video_card, self.extra
        )
        self.columns.rearranged.connect(self.relayout)
        self.page_layout.addWidget(self.columns)
        self.page_layout.addStretch()
        self.settings_area.setWidget(page)
        outer.addWidget(self.settings_area, 1)

        self.footer = QWidget()
        self.footer.setObjectName("videoFoot")
        self.footer.setAttribute(Qt.WA_StyledBackground, True)
        foot = QHBoxLayout(self.footer)
        foot.setContentsMargins(18, 10, 18, 10)
        foot.setSpacing(10)
        self.status_dot = Dot()
        foot.addWidget(self.status_dot)
        self.status = QLabel()
        self.status.setObjectName("videoStatus")
        self.status.setWordWrap(True)
        foot.addWidget(self.status, 1)
        self.query_button = QPushButton("查询原任务")
        self.query_button.setToolTip("远程任务可能仍在运行：查询它的结果，避免重复提交")
        foot.addWidget(self.query_button)
        self.generate_button = primary_button("生成视频")
        foot.addWidget(self.generate_button)
        outer.addWidget(self.footer)

        self.workflows.currentIndexChanged.connect(lambda: workbench.perform(self.select_workflow))
        self.add_image_button.clicked.connect(lambda: workbench.perform(self.add_reference_image))
        self.generate_button.clicked.connect(lambda: workbench.perform(self.generate))
        self.query_button.clicked.connect(lambda: workbench.perform(self.query))
        self.config_button.clicked.connect(lambda: workbench.perform(workbench.configure_comfy))
        self.generate_prompt_button.clicked.connect(lambda: workbench.perform(self.generate_prompt))
        self.resume_prompt_button.clicked.connect(lambda: workbench.perform(self.resume_prompt))
        self.copy_prompt_button.clicked.connect(self.copy_prompt)
        self.open_button.clicked.connect(lambda: workbench.perform(self.open_location))
        self.open_video_button.clicked.connect(lambda: workbench.perform(self.open_video))

    def build_video_card(self) -> None:
        self.video_card = QFrame()
        self.video_card.setObjectName("videoCard")
        card = QVBoxLayout(self.video_card)
        card.setContentsMargins(14, 12, 14, 14)
        card.setSpacing(SP[3])
        head = QHBoxLayout()
        head.setSpacing(SP[2])
        head.addWidget(section_title("视频"))
        self.pill = QLabel("未生成")
        self.pill.setObjectName("pill")
        head.addWidget(self.pill)
        head.addStretch()
        card.addLayout(head)
        # The picture with its buttons beside the parameters once the card is 560 wide.
        shown = QWidget()
        shown_layout = QVBoxLayout(shown)
        shown_layout.setContentsMargins(0, 0, 0, 0)
        shown_layout.setSpacing(SP[2])
        self.preview_box = AspectLabel()
        shown_layout.addWidget(self.preview_box)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.open_video_button = small_button("打开视频")
        self.open_video_button.setToolTip("用系统播放器打开生成的视频")
        self.open_button = small_button("打开视频位置")
        actions.addWidget(self.open_video_button)
        actions.addWidget(self.open_button)
        actions.addStretch()
        shown_layout.addLayout(actions)
        self.result_note = muted()
        self.result_note.setWordWrap(True)
        self.result_note.hide()
        shown_layout.addWidget(self.result_note)

        self.parameter_area = QWidget()
        params = QVBoxLayout(self.parameter_area)
        params.setContentsMargins(0, SP[1], 0, 0)
        params.setSpacing(SP[1])
        self.parameter_form = QFormLayout()
        self.parameter_form.setContentsMargins(0, 0, 0, 0)
        self.parameter_form.setHorizontalSpacing(10)
        self.parameter_form.setVerticalSpacing(SP[2])
        self.parameter_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.parameter_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        workflow_row = QHBoxLayout()
        workflow_row.setSpacing(6)
        self.workflows = QComboBox()
        self.workflows.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.workflows.setMinimumContentsLength(8)
        self.workflows.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.config_button = small_button("配置")
        self.config_button.setToolTip("ComfyUI 工作流设置")
        workflow_row.addWidget(self.workflows, 1)
        workflow_row.addWidget(self.config_button)
        self.parameter_form.addRow(field_label("工作流"), workflow_row)
        params.addLayout(self.parameter_form)
        self.advanced = Fold("高级参数", "采样步数、随机种子", small=True)
        self.advanced.toggled.connect(lambda _: self.relayout())
        self.advanced_form = QFormLayout()
        self.advanced_form.setContentsMargins(0, 0, 0, 0)
        self.advanced_form.setHorizontalSpacing(10)
        self.advanced_form.setVerticalSpacing(SP[2])
        self.advanced_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.advanced.body_layout.addLayout(self.advanced_form)
        params.addWidget(self.advanced)
        self.video_body = SplitBody(shown, self.parameter_area, 560)
        self.video_body.rearranged.connect(self.relayout)
        card.addWidget(self.video_body)
        self.duration_hint = self.make_duration_hint()

    def make_duration_hint(self) -> QLabel:
        """「与分镜一致」, or the storyboard's length with 「改成 N」; rebuilt with the form rows."""
        hint = QLabel()
        hint.setStyleSheet(f"font-size:{theme.FS_SM}px")
        hint.setTextFormat(Qt.RichText)
        # Wraps rather than widening the video card, so the card fits beside the prompt.
        hint.setWordWrap(True)
        hint.linkActivated.connect(lambda _: self.match_storyboard_duration())
        hint.hide()
        return hint

    # -- page integration ------------------------------------------------------------------

    def set_header(self, widget: QWidget) -> None:
        self.header_slot.addWidget(widget)

    def add_section(self, widget: QWidget) -> None:
        self.extra_slot.addWidget(widget)
        self.extra.show()

    def set_shot(self, duration_ms: int | None, assets: list[dict]) -> None:
        """The storyboard shot behind the target: its length and the assets the picker offers."""
        self.shot_duration_ms = duration_ms
        self.shot_assets = assets
        self.refresh_duration_hint()

    def relayout(self) -> None:
        """Content changed size: show what Qt would show later, then re-measure the page."""
        page = self.settings_area.widget()
        if page is None:
            return
        reveal(page)
        # Size caches up the tree still describe the old arrangement until Qt's queued layout
        # passes reach them; clear them so the page is measured from what is there now.
        for widget in page.findChildren(QWidget):
            widget.updateGeometry()
        for layout in page.findChildren(QLayout):
            layout.invalidate()
        self.settings_area.sync_content_height()

    # -- targets and drafts ----------------------------------------------------------------

    def set_target(self, target: dict) -> None:
        if target != self.target:
            self.remember_draft()
            previous = (self.target.get("storyboard_version_id"), self.target.get("shot_id"))
            self.shot_drafts[previous] = self.drafts
            key = (target.get("storyboard_version_id"), target.get("shot_id"))
            self.drafts = self.shot_drafts.setdefault(key, {})
            self.loaded_workflow_id = None
            self.target = dict(target)

    def generate_prompt(self) -> None:
        require(not self.workbench.tasks.busy("text"), "PROJECT_BUSY", "已有任务在运行")
        if not self.workbench.resolve_edits():
            return
        service = WorkflowService(self.workbench.repository, self.workbench.model())
        command = service.prepare_video_prompt(
            self.target.get("shot_id"),
            self.image_selection(),
            workflow_id=self.loaded_workflow_id,
            parameters=self.parameter_values(),
            current_prompt=self.prompt.toPlainText(),
        )
        self.workbench.submit(
            WorkspaceWork(self.workbench.repository, command, transport=self.workbench.transport),
            self.generate_prompt_button,
        )

    def resume_prompt(self) -> None:
        from core.short_video.v2.runtime import get_recovery

        task = get_recovery(ProjectRepository(self.workbench.output_dir), "video_prompt")
        inputs = snapshot_video_inputs(
            self.workbench.output_dir,
            self.image_selection(),
            self.loaded_workflow_id,
            self.parameter_values(),
        )
        require(
            task
            and task["valid"]
            and task["command"]["context"].get("video_input_binding") == inputs
            and task["command"]["shot_ids"] == [self.target.get("shot_id")],
            "STALE_INPUT",
            "当前镜头、参考图片或视频时长与中断任务不同，请重新编写",
        )
        self.workbench.resume_generation("video_prompt", self.resume_prompt_button)

    def copy_prompt(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.prompt.toPlainText())

    def chat_inputs(self) -> dict:
        """What the area shows, for a chat message: the prompt as typed, the chosen pictures by
        <Picture N>, the first/last frames, and the length and picture settings."""
        pictures, frames = [], []
        for slot in self.image_fields.values():
            name = picture_name(slot.image_name) if slot.image_id else ""
            if slot.frame:
                frames.append({"label": slot.caption, "name": name})
            elif slot.image_id:
                pictures.append({"reference": slot.reference, "name": name})
        settings = []
        template = getattr(self, "template", None)
        for parameter in template.parameters if template else ():
            control = self.parameter_fields.get(parameter.key)
            if control is None or not (
                parameter.role == "duration" or parameter.input_name in ("aspect_ratio", "megapixels")
            ):
                continue
            value = control.currentText() if isinstance(control, QComboBox) else f"{control.value():g}"
            settings.append({"label": parameter.label, "value": value, "role": parameter.role})
        return {
            "prompt": self.prompt.toPlainText(),
            "pictures": pictures,
            "frames": frames,
            "settings": settings,
        }

    # -- images ----------------------------------------------------------------------------

    def thumbnail(self, image: dict) -> QPixmap:
        """The image scaled down once for cards and menus, reloaded when the file changes."""
        path = MediaRepository(self.workbench.output_dir).resolve(image["relative_path"])
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = 0.0
        cached = self.thumbnails.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        pixmap = QPixmap(str(path))
        if not pixmap.isNull() and pixmap.width() > THUMBNAIL_WIDTH:
            pixmap = pixmap.scaledToWidth(THUMBNAIL_WIDTH, Qt.SmoothTransformation)
        self.thumbnails[str(path)] = (stamp, pixmap)
        return pixmap

    def picker_menu(self, node_id: str) -> QMenu:
        """本镜资产 first, then the project's images (× drops one from lists), then a file."""
        menu = QMenu(self)
        menu.setStyleSheet(PICKER_STYLE)

        def header(text: str) -> None:
            label = QLabel(text)
            label.setStyleSheet(
                f"font-size:{theme.FS_XS}px; color:{theme.INK_400}; padding:6px 8px 2px"
            )
            action = QWidgetAction(menu)
            action.setDefaultWidget(label)
            action.setEnabled(False)
            menu.addAction(action)

        if self.shot_assets:
            header("本镜资产")
            for asset in self.shot_assets:
                name = asset["name"] + (
                    f"（{asset['variant_label']}）" if asset.get("variant_label") else ""
                )
                for view in asset["views"]:
                    text = f"{KIND_LABELS[asset['kind']]} · {name} · {view['label']}"
                    image = view.get("image")
                    if image:
                        action = menu.addAction(QIcon(self.thumbnail(image)), text)
                        action.triggered.connect(
                            lambda checked=False, image_id=image["image_id"]: self.workbench.perform(
                                lambda: self.use_image(image_id, node_id)
                            )
                        )
                    else:
                        menu.addAction(text + "（未生成图片）").setEnabled(False)
        header("项目图片")
        images = MediaRepository(self.workbench.output_dir).images()
        for image in images:
            row = PickRow(image["name"], self.thumbnail(image))
            action = QWidgetAction(menu)
            action.setDefaultWidget(row)
            menu.addAction(action)
            row.chosen.connect(menu.close)
            row.chosen.connect(
                lambda image_id=image["image_id"]: self.workbench.perform(
                    lambda: self.choose_listed_image(node_id, image_id)
                )
            )
            row.removed.connect(menu.close)
            row.removed.connect(
                lambda image_id=image["image_id"]: self.workbench.perform(
                    lambda: self.remove_image(image_id)
                )
            )
        if not images:
            menu.addAction("还没有项目图片").setEnabled(False)
        menu.addSeparator()
        menu.addAction(
            "从文件选择…",
            lambda: self.workbench.perform(lambda: self.choose_image(node_id)),
        )
        return menu

    def pick_image(self, node_id: str) -> None:
        self.require_editable_inputs()
        slot = self.image_fields[node_id]
        menu = self.picker_menu(node_id)
        menu.exec(slot.mapToGlobal(slot.rect().bottomLeft()))

    def choose_listed_image(self, node_id: str, image_id: str) -> None:
        self.require_editable_inputs()
        require(node_id in self.image_fields, "INVALID_INPUT", "请选择有效的图片位置")
        ids = {image["image_id"] for image in MediaRepository(self.workbench.output_dir).images()}
        require(image_id in ids, "INVALID_INPUT", "图片不存在，请刷新后核对")
        self.image_fields[node_id].set_image(image_id)

    def use_image(self, image_id: str, node_id: str) -> None:
        self.require_editable_inputs()
        repository = MediaRepository(self.workbench.output_dir)
        if image_id in repository.hidden_images():
            # Choosing an asset image again brings it back into the reference list.
            repository.set_image_hidden(image_id, False)
        self.refresh()
        self.choose_listed_image(node_id, image_id)

    def choose_image(self, node_id: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.import_image(path, node_id)

    def import_image(self, path: str, node_id: str) -> None:
        self.require_editable_inputs()
        if self.project_dir != self.workbench.output_dir:
            self.refresh()
        repository = MediaRepository(self.workbench.output_dir)
        require(not QPixmap(path).isNull(), "INVALID_INPUT", f"图片无法解码：{Path(path).name}")
        image_id = repository.import_image(path)["image_id"]
        self.remember_draft()
        self.drafts[self.loaded_workflow_id]["images"][node_id] = image_id
        self.load_workflow()

    def remove_image(self, image_id: str) -> None:
        """Remove an entry from every image list; the file stays for asset views and jobs."""
        self.require_editable_inputs()
        MediaRepository(self.workbench.output_dir).set_image_hidden(image_id, True)
        for slot in self.image_fields.values():
            if slot.image_id == image_id:
                slot.image_id = None
        self.refresh_result()

    def add_reference_image(self) -> None:
        self.require_editable_inputs()
        require(
            len(self.image_fields) < MAX_REFERENCE_IMAGES,
            "INVALID_INPUT",
            "最多添加 9 个参考图片位置，请使用已有空位",
        )
        number = len(self.image_fields) + 1
        node_id = f"reference_input_{number}"
        while node_id in self.template.graph or node_id in self.image_fields:
            number += 1
            node_id = f"reference_input_{number}"
        self.additional_reference_slots.append(node_id)
        self.remember_draft()
        self.load_workflow()
        QTimer.singleShot(0, self, lambda: self.show_slot(node_id))

    def show_slot(self, node_id: str) -> None:
        slot = self.image_fields.get(node_id)
        if slot is not None:
            self.relayout()
            self.settings_area.widget().layout().activate()
            self.settings_area.ensureWidgetVisible(slot, 0, SP[4])

    def image_selection(self) -> dict[str, str | None]:
        return {node_id: slot.image_id for node_id, slot in self.image_fields.items()}

    def capture_prompt_image_bindings(self) -> None:
        self.prompt_image_bindings = {}
        self.refresh_reference_labels()
        self.prompt_shift.setVisible(bool(self.binding_shifts()))

    def reference_selection(self) -> dict[str, str | None]:
        frames = {
            image.node_id for image in self.template.image_inputs if image.role != "reference"
        }
        return {node: image for node, image in self.image_selection().items() if node not in frames}

    def refresh_reference_labels(self) -> None:
        reference_nodes = self.reference_selection()
        selected_nodes = []
        for node_id, image_id in reference_nodes.items():
            slot = self.image_fields[node_id]
            if image_id is None:
                slot.set_reference("")
            else:
                selected_nodes.append(node_id)
                slot.set_reference(f"<Picture {len(selected_nodes)}>")
        self.slots_note.setText(
            f"已选 {len(selected_nodes)} / {len(reference_nodes)} · 编号按已选顺序"
        )
        if not selected_nodes:
            self.prompt_image_bindings = {}
        for matched in re.findall(r"<Picture\s+(\d+)>", self.prompt.toPlainText(), re.IGNORECASE):
            number = int(matched)
            if number not in self.prompt_image_bindings and 1 <= number <= len(selected_nodes):
                self.prompt_image_bindings[number] = selected_nodes[number - 1]

    def binding_shifts(self) -> list[int]:
        """<Picture N> numbers in the prompt that now point at a different image position."""
        selected_nodes = [
            node_id for node_id, image_id in self.reference_selection().items() if image_id
        ]
        return [
            number
            for number, node_id in self.prompt_image_bindings.items()
            if number > len(selected_nodes) or selected_nodes[number - 1] != node_id
        ]

    def validate_prompt_image_bindings(self) -> None:
        for number in self.binding_shifts():
            raise WorkspaceError(
                "INVALID_INPUT",
                f"图片编号已变化，请核对并修改提示词中的 <Picture {number}> 后再生成",
            )

    # -- parameters ------------------------------------------------------------------------

    def parameter_values(self, *, raw: bool = False) -> dict:
        values = {}
        for key, control in self.parameter_fields.items():
            if isinstance(control, QCheckBox):
                values[key] = control.isChecked()
            elif isinstance(control, QComboBox):
                values[key] = control.currentData()
            elif isinstance(control, QLineEdit):
                values[key] = control.text() if raw else int(control.text())
            else:
                values[key] = control.value()
        return values

    def duration_control(self) -> QSpinBox | QDoubleSpinBox | None:
        template = getattr(self, "template", None)
        if template is None:
            return None
        key = next((p.key for p in template.parameters if p.role == "duration"), None)
        return self.parameter_fields.get(key)

    def refresh_duration_hint(self) -> None:
        control = self.duration_control()
        if control is None or not self.shot_duration_ms:
            self.duration_hint.hide()
            return
        seconds = self.shot_duration_ms / 1000
        if abs(control.value() - seconds) < 1e-6:
            self.duration_hint.setText(f"<span style='color:{theme.SUCCESS}'>与分镜一致</span>")
        else:
            self.duration_hint.setText(
                f"<span style='color:{theme.WARN}'>分镜 {seconds:g} 秒</span>&nbsp;&nbsp;"
                f"<a href='match' style='color:{theme.ACCENT}; text-decoration:none'>"
                f"改成 {seconds:g}</a>"
            )
        self.duration_hint.show()

    def match_storyboard_duration(self) -> None:
        control = self.duration_control()
        if control is not None and self.shot_duration_ms:
            seconds = self.shot_duration_ms / 1000
            control.setValue(round(seconds) if isinstance(control, QSpinBox) else seconds)

    def new_seed(self, field: QLineEdit) -> None:
        field.setText(str(random.randrange(1, 2**48)))

    def remember_draft(self) -> None:
        if self.loaded_workflow_id is not None:
            self.drafts[self.loaded_workflow_id] = {
                "images": self.image_selection(),
                "prompt": self.prompt.toPlainText(),
                "parameters": self.parameter_values(raw=True),
                "additional_reference_slots": list(self.additional_reference_slots),
                "prompt_image_bindings": dict(self.prompt_image_bindings),
                "page_scroll": self.settings_area.verticalScrollBar().value(),
                "video_prompt_fingerprint": self.video_prompt_fingerprint,
                "video_prompt_binding": self.video_prompt_binding,
            }

    # -- loading ---------------------------------------------------------------------------

    def refresh(self) -> None:
        if self.project_dir != self.workbench.output_dir:
            self.project_dir = self.workbench.output_dir
            self.loaded_workflow_id = None
            self.drafts.clear()
            self.shot_drafts.clear()
            self.video_path = None
            self.thumbnails.clear()
        self.remember_draft()
        self.catalog = MediaConfig.load(self.workbench.output_dir)
        self.workflows.blockSignals(True)
        self.workflows.clear()
        for workflow in self.catalog.workflows:
            self.workflows.addItem(workflow["name"], workflow["workflow_id"])
        self.workflows.setCurrentIndex(self.workflows.findData(self.catalog.selected_workflow_id))
        self.workflows.blockSignals(False)
        self.load_workflow()

    def select_workflow(self) -> None:
        self.require_editable_inputs()
        self.remember_draft()
        self.catalog.selected_workflow_id = self.workflows.currentData()
        self.catalog.save(self.workbench.output_dir)
        self.load_workflow()

    def load_workflow(self) -> None:
        selected = self.catalog.selected(self.workflows.currentData())
        self.template = parse_video_workflow(selected["graph"])
        self.loaded_workflow_id = selected["workflow_id"]
        draft = self.drafts.get(self.loaded_workflow_id, {})
        self.additional_reference_slots = []
        image_inputs = list(self.template.image_inputs)
        if self.template.kind == "reference":
            available = MAX_REFERENCE_IMAGES - len(image_inputs)
            for node_id in draft.get("additional_reference_slots", []):
                if len(self.additional_reference_slots) >= available:
                    break
                if node_id not in self.template.graph:
                    self.additional_reference_slots.append(node_id)
            for node_id in self.additional_reference_slots:
                image_inputs.append(ImageInput(node_id, f"图片节点 {len(image_inputs) + 1}"))
        listed = {image["image_id"] for image in MediaRepository(self.workbench.output_dir).images()}
        self.image_fields = {}
        references, frames = [], []
        for image_input in image_inputs:
            frame = image_input.role != "reference"
            caption = image_input.label if frame else f"图片 {len(references) + 1}"
            slot = SlotCard(image_input.node_id, caption, frame=frame)
            selected_id = draft.get("images", {}).get(image_input.node_id)
            slot.image_id = selected_id if selected_id in listed else None
            slot.changed.connect(lambda: self.workbench.perform(self.refresh_result))
            slot.pick_requested.connect(
                lambda node_id=image_input.node_id: self.workbench.perform(
                    lambda: self.pick_image(node_id)
                )
            )
            (frames if frame else references).append(slot)
            self.image_fields[image_input.node_id] = slot
        self.add_image_button.setVisible(self.template.kind == "reference")
        self.slot_grid.set_cells([*references, self.add_image_button])
        self.frame_grid.set_cells(frames)
        self.frames_head.setVisible(bool(frames))
        self.frame_grid.setVisible(bool(frames))
        self.prompt.blockSignals(True)
        self.prompt.setPlainText(
            draft.get(
                "prompt",
                DEFAULT_VIDEO_PROMPT if self.template.kind == "reference" else self.template.prompt,
            )
        )
        self.prompt.blockSignals(False)
        self.prompt_image_bindings = dict(draft.get("prompt_image_bindings", {}))
        self.video_prompt_fingerprint = draft.get("video_prompt_fingerprint")
        self.video_prompt_binding = draft.get("video_prompt_binding")
        # Removing a row deletes its widgets, the duration hint among them.
        while self.parameter_form.rowCount() > 1:
            self.parameter_form.removeRow(self.parameter_form.rowCount() - 1)
        while self.advanced_form.rowCount():
            self.advanced_form.removeRow(0)
        self.parameter_fields = {}
        self.duration_hint = self.make_duration_hint()
        for parameter in self.template.parameters:
            value = draft.get("parameters", {}).get(parameter.key, parameter.default)
            field = None
            if parameter.choices:
                control = QComboBox()
                for choice, label in parameter.choices:
                    control.addItem(label, choice)
                index = control.findData(value)
                control.setCurrentIndex(
                    index if index >= 0 else control.findData(parameter.default)
                )
            elif type(parameter.default) is bool:
                control = Switch()
                control.setChecked(value)
            elif parameter.input_name == "noise_seed":
                control = QLineEdit(str(value))
                field = QHBoxLayout()
                field.setSpacing(6)
                field.addWidget(control, 1)
                again = mini_button("换一个")
                again.setFixedHeight(28)
                again.clicked.connect(lambda checked=False, c=control: self.new_seed(c))
                field.addWidget(again)
            elif type(parameter.default) is int:
                control = QSpinBox()
                control.setRange(int(parameter.minimum), int(parameter.maximum))
                control.setValue(value)
            else:
                control = QDoubleSpinBox()
                control.setRange(parameter.minimum, parameter.maximum)
                control.setValue(value)
            if not isinstance(control, Switch):
                control.setMinimumHeight(CONTROL_HEIGHT)
            if isinstance(control, (QSpinBox, QDoubleSpinBox)):
                control.setMaximumWidth(120)
            if parameter.role == "duration":
                control.valueChanged.connect(self.refresh_prompt_state)
                control.valueChanged.connect(self.refresh_duration_hint)
                field = QHBoxLayout()
                field.setSpacing(SP[2])
                field.addWidget(control)
                field.addWidget(self.duration_hint, 1)
            advanced = parameter.input_name == "noise_seed" or (
                type(parameter.default) is int and parameter.role != "duration"
            )
            form = self.advanced_form if advanced else self.parameter_form
            form.addRow(field_label(parameter.label), field if field is not None else control)
            self.parameter_fields[parameter.key] = control
        self.advanced.setVisible(self.advanced_form.rowCount() > 0)
        self.refresh_duration_hint()
        self.refresh_result()
        position = draft.get("page_scroll")
        if position is not None:
            QTimer.singleShot(0, self, lambda: self.settings_area.verticalScrollBar().setValue(position))

    # -- result and state ------------------------------------------------------------------

    def refresh_result(self) -> None:
        root = ProjectRepository(self.workbench.output_dir).root
        with (
            project_lock(str(root / ".v2-project.lock"), timeout=5)
            if root.exists()
            else nullcontext()
        ):
            self._refresh_result()

    def _refresh_result(self) -> None:
        repository = MediaRepository(self.workbench.output_dir)
        selection = self.image_selection()
        for node_id, slot in self.image_fields.items():
            image_id = selection[node_id]
            if image_id:
                image = repository.image(image_id)
                slot.show_image(image["name"], self.thumbnail(image))
            else:
                slot.show_image("", None)
        self.refresh_reference_labels()
        selection = {
            node_id: image_id for node_id, image_id in selection.items() if image_id is not None
        }
        self.current_job = None
        for job in reversed(repository.jobs()):
            snapshot = job["input_snapshot"]
            if "images" in snapshot:
                job_images = {
                    node_id: image["image_id"] for node_id, image in snapshot["images"].items()
                }
                workflow_id = snapshot["workflow_id"]
            else:
                job_images = {snapshot["image_node"]: snapshot["image"]["image_id"]}
                workflow_id = "minimax-reference"
            if workflow_id == self.loaded_workflow_id and job_images == selection:
                self.current_job = job
                break
        job = self.current_job
        path = (
            repository.resolve(job["outputs"][0]["relative_path"])
            if job and job["outputs"]
            else None
        )
        self.video_path = path
        self.show_job(job, bool(selection))
        busy = self.workbench.tasks.busy("video")
        prompt_busy = self.workbench.tasks.prompt_busy(self.target)
        self.generate_button.setEnabled(any(selection.values()) and not busy and not prompt_busy)
        self.generate_button.setToolTip(
            "当前镜头的视频提示词正在编写，请完成后再生成视频" if prompt_busy else ""
        )
        self.add_image_button.set_room(MAX_REFERENCE_IMAGES - len(self.image_fields))
        self.add_image_button.setEnabled(
            len(self.image_fields) < MAX_REFERENCE_IMAGES and not prompt_busy
        )
        for control in (self.workflows, self.slots_section, self.parameter_area, self.prompt):
            control.setEnabled(not prompt_busy)
        self.config_button.setEnabled(not self.workbench.tasks.any_busy)
        waiting = bool(job and job["status"] not in {"succeeded", "cancelled", "failed"})
        self.query_button.setVisible(waiting)
        self.query_button.setEnabled(waiting and not busy)
        self.open_button.setEnabled(bool(self.workbench.output_dir))
        self.open_video_button.setEnabled(bool(path and path.is_file()))
        self.refresh_prompt_state()
        self.relayout()
        self.inputs_changed.emit()

    def show_job(self, job: dict | None, selected: bool) -> None:
        """The pill, the stand-in picture and the footer sentence for the matching job."""
        if job is None:
            frames_only = self.template.kind != "reference"
            text = (
                ("这一镜还没有生成视频" if self.target.get("shot_id") else "还没有生成视频")
                if selected
                else ("请至少选择一张图片" if frames_only else "请至少选择一张参考图片")
            )
            self.set_job_view("", "未生成", "生成的视频显示在这里", text, "")
            return
        status = job["status"]
        text = JOB_STATES[status]
        duration = None
        if job["outputs"]:
            duration = job["outputs"][0].get("metadata", {}).get("duration")
            if isinstance(duration, (int, float)):
                text += f"；实际时长 {duration:.2f} 秒"
        reason = ""
        if job.get("feedback"):
            reason = job["feedback"]["summary"]
        elif job.get("error"):
            from core.short_video.v2.feedback import concise

            reason = concise(f"{job['error']['type']}: {job['error']['message']}")
        if reason:
            text += "；" + reason
        if status == "unknown":
            text += "；请查询原任务，避免重复提交"
        note = f"实际时长 {duration:.2f} 秒" if isinstance(duration, (int, float)) else ""
        if status in RUNNING:
            self.set_job_view(
                "run", "生成中", f"{JOB_STATES[status]}…\n已提交到 ComfyUI，可以先去做别的", text, note
            )
        elif status == "succeeded":
            self.set_job_view("ok", "已保存", "视频已保存\n点「打开视频」播放", text, note)
        elif status == "failed":
            picture = "生成失败" + (f"：{reason}" if reason else "") + "\n检查服务后重新点「生成视频」"
            self.set_job_view("bad", "失败", picture, text, note)
        elif status in {"unknown", "interrupted"}:
            self.set_job_view("warn", "待确认", f"{JOB_STATES[status]}\n点「查询原任务」查看结果", text, note)
        else:
            self.set_job_view("", JOB_STATES[status], JOB_STATES[status], text, note)

    def set_job_view(self, tone: str, pill: str, picture: str, status: str, note: str) -> None:
        self.pill.setText(pill)
        set_tone(self.pill, tone)
        self.preview_box.setText(picture)
        set_tone(self.preview_box, tone)
        self.status.setText(status)
        set_tone(self.status, tone)
        self.status_dot.set_tone(tone)
        self.result_note.setText(note)
        self.result_note.setVisible(bool(note))

    def refresh_prompt_state(self) -> None:
        root = ProjectRepository(self.workbench.output_dir).root
        with (
            project_lock(str(root / ".v2-project.lock"), timeout=5)
            if root.exists()
            else nullcontext()
        ):
            self._refresh_prompt_state()

    def _refresh_prompt_state(self) -> None:
        from core.short_video.v2.runtime import get_recovery

        repository = ProjectRepository(self.workbench.output_dir)
        state = repository.load()
        bundle = repository.current("prompt_bundle", state=state)
        shot = next(
            (
                s
                for s in (bundle or {}).get("content", {}).get("shots", [])
                if s["shot_id"] == self.target.get("shot_id")
            ),
            None,
        )
        valid = bool(
            bundle
            and repository.is_current(bundle["id"], state=state)
            and bundle["content"]["storyboard_version_id"]
            == self.target.get("storyboard_version_id")
            and shot
        )
        inputs, reason, tone = None, "请选择当前镜头并先完成资产提示词", "warn"
        if valid:
            try:
                inputs = snapshot_video_inputs(
                    self.workbench.output_dir,
                    self.image_selection(),
                    self.loaded_workflow_id,
                    self.parameter_values(),
                )
                reason, tone = "结合当前分镜、相关资产和实际选图编写，可继续手动修改。", ""
            except (WorkspaceError, OSError, ValueError) as exc:
                reason = str(exc)
        if (
            inputs
            and shot.get("video_motion_prompt")
            and shot.get("video_prompt_binding") == inputs
        ):
            generated = fingerprint([shot["video_motion_prompt"], shot["video_prompt_binding"]])
            if generated != self.video_prompt_fingerprint:
                self.prompt.setPlainText(shot["video_motion_prompt"])
                self.video_prompt_fingerprint = generated
                self.video_prompt_binding = inputs
        if self.video_prompt_binding and (not valid or inputs != self.video_prompt_binding):
            reason, tone = "镜头、选图或视频时长已变化，请重新编写并核对当前提示词。", "warn"
        busy = self.workbench.tasks.busy("text")
        self.generate_prompt_button.setEnabled(bool(inputs) and not busy)
        task = get_recovery(repository, "video_prompt")
        can_resume = bool(
            task
            and task["valid"]
            and inputs
            and task["command"]["context"].get("video_input_binding") == inputs
            and task["command"]["shot_ids"] == [self.target.get("shot_id")]
        )
        self.resume_prompt_button.setVisible(can_resume)
        self.resume_prompt_button.setEnabled(can_resume and not busy)
        if can_resume:
            reason, tone = "上次编写未完成，可从已保存的检查点继续。", "warn"
        if self.workbench.tasks.prompt_busy(self.target):
            reason, tone = "正在编写视频提示词…完成前不能改图片和参数。", "run"
        self.prompt_status.setText(reason)
        set_tone(self.prompt_status, tone)
        self.prompt_shift.setVisible(bool(self.binding_shifts()))

    # -- actions ---------------------------------------------------------------------------

    def generate(self) -> None:
        require(not self.workbench.tasks.busy("video"), "PROJECT_BUSY", "已有视频任务在运行")
        require(
            not self.workbench.tasks.prompt_busy(self.target),
            "PROJECT_BUSY",
            "当前镜头的视频提示词正在编写，请完成后再生成视频",
        )
        self.validate_prompt_image_bindings()
        prompt = self.prompt.toPlainText().strip()
        command = MediaService(self.workbench.output_dir).prepare_video(
            self.image_selection(),
            prompt,
            workflow_id=self.loaded_workflow_id,
            parameters=self.parameter_values(),
            target=self.target,
        )
        self.workbench.submit(MediaWork(self.workbench.output_dir, command), self.generate_button)

    def query(self) -> None:
        require(not self.workbench.tasks.busy("video"), "PROJECT_BUSY", "已有视频任务在运行")
        require(self.current_job is not None, "INVALID_INPUT", "尚无视频任务")
        self.workbench.submit(
            MediaWork(self.workbench.output_dir, job_id=self.current_job["job_id"]),
            self.query_button,
        )

    def open_location(self) -> None:
        require(bool(self.workbench.output_dir), "INVALID_INPUT", "请先打开项目")
        directory = MediaRepository(self.workbench.output_dir).resolve("videos")
        directory.mkdir(parents=True, exist_ok=True)
        require(
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))),
            "OPEN_LOCATION_FAILED",
            f"无法打开视频目录：{directory}",
        )

    def open_video(self) -> None:
        require(
            self.video_path is not None and self.video_path.is_file(),
            "INVALID_INPUT",
            "这组输入还没有生成好的视频",
        )
        require(
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.video_path))),
            "OPEN_LOCATION_FAILED",
            f"无法打开视频：{self.video_path}",
        )

    def require_editable_inputs(self) -> None:
        require(
            not self.workbench.tasks.prompt_busy(self.target),
            "PROJECT_BUSY",
            "当前镜头的视频提示词正在编写，请完成后再修改输入",
        )
