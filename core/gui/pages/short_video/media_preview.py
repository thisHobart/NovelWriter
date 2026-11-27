"""Video generation for one storyboard shot: reference images, video prompt, the video card with
its parameters, and 「生成视频」 pinned at the bottom."""

from __future__ import annotations

import logging
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
    H3_MOTION_KIND,
    MAX_REFERENCE_IMAGES,
    ImageInput,
    MediaConfig,
    load_shot_drafts,
    parse_video_workflow,
    save_shot_drafts,
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

logger = logging.getLogger(__name__)

DEFAULT_VIDEO_PROMPT = (
    "以 <Picture 1> 为画面参考，保持主体外观、环境与构图一致，"
    "生成自然连贯的动态画面和轻微镜头运动。无文字、无水印。"
)
DUAL_SHOT_PROMPT = (
    "The two reference characters perform their corresponding movements in the supplied scene. "
    "Maintain distinct identities, original framing, and natural motion."
)
KIND_LABELS = {"scene": "场景", "character": "人物", "prop": "道具"}
DUAL_SCENE_WORKFLOW = "scail2_dual_scene"
DUAL_IMAGE_CAPTIONS = {"role_a": "角色 A", "role_b": "角色 B", "scene_a": "当前场景"}
# The two people's colors in the tracking preview, repeated on the 角色 A/B tags and the
# description rows so the pairing reads without opening the preview.
DUAL_MARKS = {"role_a": "#2F7FD8", "role_b": "#D6453D"}
DUAL_DESCRIPTION_MARKS = {"443.text": "role_a", "source_b_text.text": "role_b"}
DUAL_TIMELINE_ROLES = {"single_shot", "scene_transition", "action_end", "tracking_cuts", "hold_tail", "clip_ranges"}
TRACKING_ROLES = {"source_character", "output_edge"}
SINGLE_SHOT_KEY = "139.single_shot"
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
QLabel#promptHint[tone="ok"] {{ color:{theme.SUCCESS}; }}
QLabel#promptHint[tone="bad"] {{ color:{theme.DANGER}; }}
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


def field_label(text: str, mark: str = "") -> QLabel:
    """A form row's label; ``mark`` puts a dot of that color before it."""
    label = QLabel(f"<span style='color:{mark}'>●</span>&nbsp;{text}" if mark else text)
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
        self.tracking_preview_job = None
        self.tracking_preview_path = None
        self.project_dir = None
        self.shot_assets: list[dict] = []
        self.shot_duration_ms: int | None = None
        self.thumbnails: dict[str, tuple[float, QPixmap]] = {}
        self.image_fields: dict[str, SlotCard] = {}
        self.role_slots: list[SlotCard] = []
        self.driving_video = None
        self.additional_reference_slots = []
        self.prompt_image_bindings = {}
        self.parameter_fields = {}
        self.drafts = {}
        self.loaded_workflow_id = None
        # Edits are written to the project a moment after typing stops, and on every switch.
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(800)
        self.save_timer.timeout.connect(self.remember_draft)
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
        self.slots_title = section_title("参考图片")
        head.addWidget(self.slots_title)
        self.slots_note = muted()
        head.addWidget(self.slots_note)
        head.addStretch()
        slots_layout.addLayout(head)
        self.driving_video_row = QWidget()
        video_row = QHBoxLayout(self.driving_video_row)
        video_row.setContentsMargins(0, 0, 0, 0)
        self.driving_video_caption = field_label("原视频")
        video_row.addWidget(self.driving_video_caption)
        self.driving_video_label = muted("未选择")
        self.driving_video_label.setWordWrap(True)
        video_row.addWidget(self.driving_video_label, 1)
        # The dual-scene workflow's tracking preview sits on the source video's line.
        self.tracking_preview_button = small_button("提取动作预览")
        self.tracking_preview_button.setToolTip(
            "只跑人物跟踪、不生成视频，用颜色核对两人有没有对反。\n改了原片人物描述、画幅或输出长边后需要重新提取"
        )
        self.tracking_preview_button.clicked.connect(
            lambda: self.workbench.perform(self.generate_tracking_preview)
        )
        video_row.addWidget(self.tracking_preview_button)
        self.open_tracking_button = small_button("打开动作预览")
        self.open_tracking_button.clicked.connect(
            lambda: self.workbench.perform(self.open_tracking_preview)
        )
        video_row.addWidget(self.open_tracking_button)
        self.driving_video_button = small_button("选择原视频")
        self.driving_video_button.clicked.connect(
            lambda: self.workbench.perform(self.choose_driving_video)
        )
        video_row.addWidget(self.driving_video_button)
        self.driving_video_row.hide()
        slots_layout.addWidget(self.driving_video_row)
        self.tracking_status = QLabel()
        self.tracking_status.setObjectName("promptHint")
        self.tracking_status.setWordWrap(True)
        self.tracking_status.hide()
        slots_layout.addWidget(self.tracking_status)
        self.slot_grid = SlotGrid()
        slots_layout.addWidget(self.slot_grid)
        # The dual-scene workflow's pictures: the two people, then the scene, each under its own
        # heading like 「首尾帧」 below 「参考图片」.
        self.dual_inputs_section = QWidget()
        dual_layout = QVBoxLayout(self.dual_inputs_section)
        dual_layout.setContentsMargins(0, 0, 0, 0)
        dual_layout.setSpacing(SP[2])
        role_head = QHBoxLayout()
        role_head.setContentsMargins(0, SP[2], 0, 0)
        role_head.setSpacing(SP[2])
        role_head.addWidget(section_title("角色参考"))
        role_head.addWidget(muted("蓝色对应角色 A，红色对应角色 B"))
        self.swap_roles_button = mini_button("对调")
        self.swap_roles_button.setToolTip("两张角色图互换；原片人物描述不动，动作预览仍然有效")
        self.swap_roles_button.clicked.connect(lambda: self.workbench.perform(self.swap_roles))
        role_head.addWidget(self.swap_roles_button)
        role_head.addStretch()
        dual_layout.addLayout(role_head)
        self.role_grid = SlotGrid()
        dual_layout.addWidget(self.role_grid)
        scene_head = QHBoxLayout()
        scene_head.setContentsMargins(0, SP[2], 0, 0)
        scene_head.setSpacing(SP[2])
        scene_head.addWidget(section_title("场景参考"))
        scene_head.addWidget(muted("替换原视频的背景，按画幅裁切"))
        scene_head.addStretch()
        dual_layout.addLayout(scene_head)
        self.scene_grid = SlotGrid()
        dual_layout.addWidget(self.scene_grid)
        self.dual_inputs_section.hide()
        slots_layout.addWidget(self.dual_inputs_section)
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
        self.prompt_note = muted("用 <Picture N> 指代上面的参考图片")
        self.prompt_note.setWordWrap(True)
        prompt_bar.addWidget(self.prompt_note, 1)
        self.generate_prompt_button = small_button("AI 编写")
        self.generate_prompt_button.setToolTip("将本镜选中的参考图和首尾帧发给生成模型，编写视频提示词")
        self.resume_prompt_button = small_button("继续编写")
        self.copy_prompt_button = small_button("复制")
        for button in (self.generate_prompt_button, self.resume_prompt_button, self.copy_prompt_button):
            prompt_bar.addWidget(button)
        prompt_layout.addLayout(prompt_bar)
        self.prompt = QPlainTextEdit()
        self.prompt.setMinimumHeight(200)
        self.prompt.textChanged.connect(self.capture_prompt_image_bindings)
        self.prompt.textChanged.connect(self.save_timer.start)
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
            # Kept per shot, not per storyboard version: editing and saving the storyboard leaves
            # the chosen images, the video prompt and the parameters as they are.
            self.drafts = self.shot_drafts.setdefault(target.get("shot_id"), {})
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
            **({"fixed_references": True} if self.is_dual_scene else {}),
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
        remaining = len(images)

        def remove_listed_image(action: QWidgetAction, image_id: str) -> None:
            nonlocal remaining
            self.remove_image(image_id)
            menu.removeAction(action)
            action.deleteLater()
            remaining -= 1
            empty_images.setVisible(remaining == 0)

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
            row.removed.connect(
                lambda action=action, image_id=image["image_id"]: self.workbench.perform(
                    lambda: remove_listed_image(action, image_id)
                )
            )
        empty_images = menu.addAction("还没有项目图片")
        empty_images.setEnabled(False)
        empty_images.setVisible(not images)
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

    def choose_driving_video(self) -> None:
        self.require_editable_inputs()
        path, _ = QFileDialog.getOpenFileName(
            self, f"选择{self.template.driving_video_label}", self.workbench.output_dir, "视频 (*.mp4 *.mov *.webm *.mkv)"
        )
        if path:
            self.driving_video = MediaRepository(self.workbench.output_dir).import_driving_video(path)
            self.driving_video_label.setText(self.driving_video["name"])
            self.refresh_result()
            self.save_timer.start()

    def capture_prompt_image_bindings(self) -> None:
        self.prompt_image_bindings = {}
        self.refresh_reference_labels()
        self.prompt_shift.setVisible(bool(self.binding_shifts()))

    def reference_selection(self) -> dict[str, str | None]:
        frames = {
            image.node_id for image in self.template.image_inputs if image.role != "reference"
        }
        return {node: image for node, image in self.image_selection().items() if node not in frames}

    @property
    def is_dual_scene(self) -> bool:
        template = getattr(self, "template", None)
        return template is not None and template.kind == DUAL_SCENE_WORKFLOW

    def refresh_reference_labels(self) -> None:
        if self.is_dual_scene:
            slots = self.image_fields.values()
            self.slots_note.setText(f"已选 {sum(bool(slot.image_id) for slot in slots)} / {len(slots)}")
            for slot in slots:
                slot.set_reference(slot.caption)
            return
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
        values = {SINGLE_SHOT_KEY: True} if self.is_dual_scene else {}
        seed_keys = {p.key for p in self.template.parameters if p.input_name in {"noise_seed", "seed"}}
        for key, control in self.parameter_fields.items():
            if isinstance(control, QCheckBox):
                values[key] = control.isChecked()
            elif isinstance(control, QComboBox):
                values[key] = control.currentData()
            elif isinstance(control, QLineEdit):
                values[key] = int(control.text()) if key in seed_keys and not raw else control.text()
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
        """Keep what the video area shows for this shot and workflow, in memory and in the project."""
        self.save_timer.stop()
        if self.loaded_workflow_id is not None:
            previous = self.drafts.get(self.loaded_workflow_id, {}) if self.is_dual_scene else {}
            self.drafts[self.loaded_workflow_id] = {
                "images": {**previous.get("images", {}), **self.image_selection()},
                **({"driving_video_id": self.driving_video["video_id"]} if self.driving_video else {}),
                "prompt": self.prompt.toPlainText(),
                "parameters": {**previous.get("parameters", {}), **self.parameter_values(raw=True)},
                "additional_reference_slots": list(self.additional_reference_slots),
                "prompt_image_bindings": dict(self.prompt_image_bindings),
                "page_scroll": self.settings_area.verticalScrollBar().value(),
                "video_prompt_fingerprint": self.video_prompt_fingerprint,
                "video_prompt_binding": self.video_prompt_binding,
            }
            if self.project_dir:
                try:
                    save_shot_drafts(self.project_dir, self.shot_drafts)
                except OSError:
                    logger.warning("视频区内容没有写入项目", exc_info=True)

    # -- loading ---------------------------------------------------------------------------

    def refresh(self) -> None:
        if self.project_dir != self.workbench.output_dir:
            self.remember_draft()
            self.project_dir = self.workbench.output_dir
            self.loaded_workflow_id = None
            # What each shot's video area held when the project was last open.
            self.shot_drafts = load_shot_drafts(self.project_dir) if self.project_dir else {}
            self.drafts = self.shot_drafts.setdefault(self.target.get("shot_id"), {})
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
        motion = self.template.kind == H3_MOTION_KIND
        previous = self.drafts.get("minimax-first-last", {}) if motion else {}
        draft = self.drafts.get(self.loaded_workflow_id, previous)
        self.driving_video = None
        if self.template.driving_video_node is not None and draft.get("driving_video_id"):
            try:
                self.driving_video = MediaRepository(self.workbench.output_dir).driving_video(
                    draft["driving_video_id"]
                )
            except (WorkspaceError, OSError, ValueError):
                logger.warning("无法恢复原视频", exc_info=True)
        self.driving_video_row.setVisible(self.template.driving_video_node is not None)
        self.driving_video_caption.setText(self.template.driving_video_label)
        self.driving_video_button.setText(f"选择{self.template.driving_video_label}")
        self.driving_video_label.setText(self.driving_video["name"] if self.driving_video else "未选择")
        dual_scene = self.is_dual_scene
        for widget in (self.tracking_preview_button, self.open_tracking_button, self.tracking_status):
            widget.setVisible(dual_scene)
        self.slots_title.setText("动作迁移素材" if dual_scene else "参考图片")
        self.slot_grid.setVisible(not dual_scene)
        self.dual_inputs_section.setVisible(dual_scene)
        self.advanced.note.setText("输出长边、采样步数、随机种子" if dual_scene else "采样步数、随机种子")
        prompt_note = (
            "描述最终画面和动作，角色与场景按上面的素材对应"
            if dual_scene else "用 <Picture N> 指代上面的参考图片"
        )
        if motion:
            prompt_note = "<Video 1> 提供动作参考，成片时长至少覆盖所选视频"
        self.prompt_note.setText(prompt_note)
        self.additional_reference_slots = []
        image_inputs = [image for image in self.template.image_inputs if not dual_scene or image.role != "scene_b"]
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
        references, frames, roles, scenes = [], [], [], []
        for image_input in image_inputs:
            frame = not dual_scene and image_input.role != "reference"
            if dual_scene:
                caption = DUAL_IMAGE_CAPTIONS[image_input.role]
                cells = roles if image_input.role.startswith("role_") else scenes
            else:
                caption = image_input.label if frame or self.template.driving_video_node else f"图片 {len(references) + 1}"
                cells = frames if frame else references
            slot = SlotCard(
                image_input.node_id, caption, frame=frame, contain=dual_scene,
                mark=DUAL_MARKS.get(image_input.role, ""),
            )
            if dual_scene:
                slot.setToolTip(caption)
            selected_id = draft.get("images", {}).get(image_input.node_id)
            slot.image_id = selected_id if selected_id in listed else None
            slot.changed.connect(lambda: self.workbench.perform(self.refresh_result))
            slot.pick_requested.connect(
                lambda node_id=image_input.node_id: self.workbench.perform(
                    lambda: self.pick_image(node_id)
                )
            )
            cells.append(slot)
            self.image_fields[image_input.node_id] = slot
        self.add_image_button.setVisible(self.template.kind == "reference")
        self.slot_grid.set_cells([*references, self.add_image_button])
        self.role_slots = roles
        self.role_grid.set_cells(roles)
        self.scene_grid.set_cells(scenes)
        self.frame_grid.set_cells(frames)
        self.frames_head.setVisible(bool(frames))
        self.frame_grid.setVisible(bool(frames))
        self.prompt.blockSignals(True)
        default_prompt = DUAL_SHOT_PROMPT if dual_scene else (
            DEFAULT_VIDEO_PROMPT if self.template.kind == "reference" else self.template.prompt
        )
        self.prompt.setPlainText(draft.get("prompt", default_prompt))
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
            if dual_scene and parameter.role in DUAL_TIMELINE_ROLES:
                continue
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
                if parameter.role == "source_aspect":
                    control.setToolTip("跟随原视频保留完整画面。固定画幅可能裁掉画面边缘。")
            elif type(parameter.default) is bool:
                control = Switch()
                control.setChecked(value)
            elif parameter.input_name in {"noise_seed", "seed"}:
                control = QLineEdit(str(value))
                field = QHBoxLayout()
                field.setSpacing(6)
                field.addWidget(control, 1)
                again = mini_button("换一个")
                again.setFixedHeight(28)
                again.clicked.connect(lambda checked=False, c=control: self.new_seed(c))
                field.addWidget(again)
            elif type(parameter.default) is str:
                control = QLineEdit(str(value))
                # Show where a long description starts, not where it ends.
                control.setCursorPosition(0)
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
            control.setAccessibleName(parameter.label)
            if isinstance(control, (QSpinBox, QDoubleSpinBox)):
                control.setMaximumWidth(120)
            if parameter.role == "duration":
                control.valueChanged.connect(self.refresh_prompt_state)
                control.valueChanged.connect(self.refresh_duration_hint)
                field = QHBoxLayout()
                field.setSpacing(SP[2])
                field.addWidget(control)
                field.addWidget(self.duration_hint, 1)
            advanced = parameter.input_name in {"noise_seed", "seed"} or (
                type(parameter.default) is int
                and parameter.role != "duration"
                and parameter.input_name != "frame_load_cap"
            )
            form = self.advanced_form if advanced else self.parameter_form
            mark = DUAL_MARKS.get(DUAL_DESCRIPTION_MARKS.get(parameter.key, ""), "") if dual_scene else ""
            form.addRow(field_label(parameter.label, mark), field if field is not None else control)
            if parameter.role == "source_character":
                control.textChanged.connect(self.refresh_generate_button)
            for name in ("valueChanged", "currentIndexChanged", "toggled", "textChanged"):
                signal = getattr(control, name, None)
                if signal is not None:
                    signal.connect(self.save_timer.start)
            self.parameter_fields[parameter.key] = control
            if dual_scene and (parameter.role in TRACKING_ROLES or parameter.input_name == "aspect_ratio"):
                if isinstance(control, QLineEdit):
                    control.editingFinished.connect(self.refresh_tracking_preview)
                elif isinstance(control, QComboBox):
                    control.currentIndexChanged.connect(self.refresh_tracking_preview)
                else:
                    control.valueChanged.connect(self.refresh_tracking_preview)
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
        video_id = self.driving_video["video_id"] if self.driving_video else None
        jobs = repository.jobs()
        for job in reversed(jobs):
            snapshot = job["input_snapshot"]
            if snapshot.get("tracking_preview"):
                continue
            images = snapshot.get("images")
            if not isinstance(images, dict) or not images:
                continue
            job_images = {node_id: image["image_id"] for node_id, image in images.items()}
            if (
                snapshot.get("workflow_id") == self.loaded_workflow_id
                and (self.template.kind != H3_MOTION_KIND
                     or (snapshot.get("target") or {}).get("shot_id") == self.target.get("shot_id"))
                and job_images == selection
                and (snapshot.get("driving_video") or {}).get("video_id") == video_id
            ):
                self.current_job = job
                break
        job = self.current_job
        path = (
            repository.resolve(job["outputs"][0]["relative_path"])
            if job and job["outputs"]
            else None
        )
        self.video_path = path
        self.open_video_button.setText("打开片段" if job and len(job["outputs"]) > 1 else "打开视频")
        self.show_job(job, bool(selection))
        busy = self.workbench.tasks.busy("video")
        prompt_busy = self.workbench.tasks.prompt_busy(self.target)
        self.refresh_generate_button()
        self.swap_roles_button.setEnabled(any(slot.image_id for slot in self.role_slots))
        self.add_image_button.set_room(MAX_REFERENCE_IMAGES - len(self.image_fields))
        self.add_image_button.setEnabled(
            len(self.image_fields) < MAX_REFERENCE_IMAGES and not prompt_busy
        )
        for control in (self.workflows, self.slots_section, self.parameter_area, self.prompt):
            control.setEnabled(not prompt_busy)
        self.config_button.setEnabled(not self.workbench.tasks.busy("video"))
        waiting = bool(job and job["status"] not in {"succeeded", "cancelled", "failed"})
        self.query_button.setVisible(waiting)
        self.query_button.setEnabled(waiting and not busy)
        self.open_button.setEnabled(bool(self.workbench.output_dir))
        self.open_video_button.setEnabled(bool(path and path.is_file()))
        self._sync_tracking_preview(repository, jobs)
        self.refresh_prompt_state()
        self.relayout()
        self.save_timer.start()
        self.inputs_changed.emit()

    def refresh_generate_button(self) -> None:
        """「生成视频」 waits for the inputs and for running tasks. For two people, the line under the
        prompt says what is still missing, or which picture plays which person."""
        tasks = self.workbench.tasks
        prompt_busy = tasks.prompt_busy(self.target)
        if self.is_dual_scene:
            missing = self.dual_missing()
            ready = not missing
            self.prompt_status.setText(missing or self.dual_casting())
            set_tone(self.prompt_status, "warn" if missing else "")
        elif self.template.kind == H3_MOTION_KIND:
            missing_inputs = [
                image.label for image in self.template.image_inputs
                if image.role != "reference" and not self.image_fields[image.node_id].image_id
            ]
            if self.driving_video is None:
                missing_inputs.append(self.template.driving_video_label)
            missing = "请选择" + "、".join(missing_inputs) if missing_inputs else ""
            ready = not missing
        else:
            missing = ""
            ready = any(self.image_selection().values()) and (
                self.template.driving_video_node is None or self.driving_video is not None
            )
        self.generate_button.setEnabled(ready and not tasks.busy("video") and not prompt_busy)
        self.generate_button.setToolTip(
            "当前镜头的视频提示词正在编写，请完成后再生成视频" if prompt_busy else missing
        )

    def dual_missing(self) -> str:
        """What the two-person video still needs, as one sentence; empty once everything is in."""
        pick = [] if self.driving_video else ["原视频"]
        pick += [
            slot.caption if slot in self.role_slots else "场景参考图"
            for slot in self.image_fields.values()
            if not slot.image_id
        ]
        fill = [
            parameter.label
            for parameter in self.template.parameters
            if parameter.role == "source_character"
            and not self.parameter_fields[parameter.key].text().strip()
        ]
        parts = (["请选择" + "、".join(pick)] if pick else []) + (
            [("填写" if pick else "请填写") + "、".join(fill)] if fill else []
        )
        return "，".join(parts)

    def dual_casting(self) -> str:
        first, second = (picture_name(slot.image_name) for slot in self.role_slots)
        return f"蓝色 → {first}，红色 → {second}，背景换成场景参考图；提示词可以继续手动修改。"

    def swap_roles(self) -> None:
        """角色 A and 角色 B trade pictures; the descriptions stay, so the colors keep their people."""
        self.require_editable_inputs()
        first, second = self.role_slots
        first.image_id, second.image_id = second.image_id, first.image_id
        self.refresh_result()

    def refresh_tracking_preview(self) -> None:
        repository = MediaRepository(self.workbench.output_dir)
        self._sync_tracking_preview(repository, repository.jobs())

    def _sync_tracking_preview(self, repository: MediaRepository, jobs: list[dict]) -> None:
        self.tracking_preview_job = None
        self.tracking_preview_path = None
        if not self.is_dual_scene:
            return
        video_id = self.driving_video["video_id"] if self.driving_video else None
        parameters = self.parameter_values(raw=True)
        tracked = tuple(
            parameter for parameter in self.template.parameters
            if parameter.role in TRACKING_ROLES or parameter.input_name == "aspect_ratio"
        )

        def effective(settings: dict) -> dict:
            values = {p.key: settings.get(p.key, p.default) for p in tracked}
            values[SINGLE_SHOT_KEY] = settings.get(SINGLE_SHOT_KEY, False)
            for parameter in tracked:
                if parameter.role == "output_edge":
                    values[parameter.key] = min(512, values[parameter.key])
            return values

        current = effective(parameters)
        # A preview of this video made with other settings.
        outdated = False
        for job in reversed(jobs):
            snapshot = job["input_snapshot"]
            if (
                video_id is None or not snapshot.get("tracking_preview")
                or snapshot.get("workflow_id") != self.loaded_workflow_id
                or (snapshot.get("driving_video") or {}).get("video_id") != video_id
            ):
                continue
            if effective(snapshot.get("parameters", {})) != current:
                outdated = outdated or job["status"] == "succeeded"
                continue
            if self.tracking_preview_job is None:
                self.tracking_preview_job = job
            if job["status"] == "succeeded" and job["outputs"]:
                path = repository.resolve(job["outputs"][0]["relative_path"])
                if path.is_file():
                    self.tracking_preview_path = path
                    break
        busy = self.workbench.tasks.busy("video")
        done = self.tracking_preview_path is not None
        self.tracking_preview_button.setEnabled(video_id is not None and not busy)
        self.open_tracking_button.setEnabled(done)
        latest = self.tracking_preview_job
        status = latest["status"] if latest else ""
        pending = status in RUNNING or status in {"unknown", "interrupted"}
        self.tracking_preview_button.setText(
            "查询动作预览" if pending else "重新提取动作预览" if done or outdated else "提取动作预览"
        )
        if video_id is None:
            hint, tone = "选好原视频后可以提取动作预览，用颜色核对两人有没有对反。", ""
        elif status in RUNNING:
            hint, tone = f"正在提取动作预览（{JOB_STATES[status]}）：只跑人物跟踪，不生成视频。", "run"
        elif pending:
            hint, tone = f"动作预览{JOB_STATES[status]}，点「查询动作预览」查看结果。", "warn"
        elif done:
            hint = "动作预览已提取：打开后核对蓝色是否圈住原片人物 A，红色是否圈住原片人物 B。"
            tone = "ok"
        elif status == "failed":
            hint, tone = "动作预览提取失败，检查服务后重新提取。", "bad"
        elif outdated:
            hint, tone = "动作预览是按之前的设置提取的，已过期，请重新提取。", "warn"
        else:
            hint = "提取动作预览后，蓝色圈出角色 A 对应的人，红色圈出角色 B 对应的人。不提取也能生成。"
            tone = ""
        self.tracking_status.setText(hint)
        set_tone(self.tracking_status, tone)

    def show_job(self, job: dict | None, selected: bool) -> None:
        """The pill, the stand-in picture and the footer sentence for the matching job."""
        if job is None:
            frames_only = self.template.kind != "reference"
            text = (
                ("这一镜还没有生成视频" if self.target.get("shot_id") else "还没有生成视频")
                if selected
                else ("请至少选择一张图片" if frames_only else "请至少选择一张参考图片")
            )
            if self.is_dual_scene and not selected:
                text = "请选择原视频、角色 A/B 和当前场景"
            self.set_job_view("", "未生成", "生成的视频显示在这里", text, "")
            return
        status = job["status"]
        text = JOB_STATES[status]
        duration = None
        if len(job["outputs"]) > 1:
            text += f" · {len(job['outputs'])} 个片段"
        elif job["outputs"]:
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
            action = "打开片段" if len(job["outputs"]) > 1 else "打开视频"
            self.set_job_view("ok", "已保存", f"视频已保存\n点「{action}」播放", text, note)
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
        self.generate_prompt_button.setVisible(not self.is_dual_scene)
        if self.is_dual_scene:
            # No prompt writing to report: the status line says what the video still needs.
            self.resume_prompt_button.hide()
            self.prompt_shift.hide()
            return
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

    def generate_tracking_preview(self) -> None:
        require(self.is_dual_scene and self.driving_video is not None, "INVALID_INPUT", "请选择原视频")
        require(not self.workbench.tasks.busy("video"), "PROJECT_BUSY", "已有视频任务在运行")
        self.refresh_tracking_preview()
        latest = self.tracking_preview_job
        if latest and latest["status"] not in {"succeeded", "failed", "cancelled"}:
            work = MediaWork(self.workbench.output_dir, job_id=latest["job_id"])
        else:
            command = MediaService(self.workbench.output_dir).prepare_video(
                {}, "", workflow_id=self.loaded_workflow_id,
                driving_video_id=self.driving_video["video_id"], parameters=self.parameter_values(),
                target=self.target, tracking_preview=True,
            )
            work = MediaWork(self.workbench.output_dir, command)
        self.workbench.submit(work, self.tracking_preview_button)

    def open_tracking_preview(self) -> None:
        self.refresh_tracking_preview()
        path = self.tracking_preview_path
        require(path is not None, "INVALID_INPUT", "请先提取当前设置的动作预览")
        require(
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))),
            "OPEN_LOCATION_FAILED", f"无法打开动作预览：{path}",
        )

    def generate(self) -> None:
        require(not self.workbench.tasks.busy("video"), "PROJECT_BUSY", "已有视频任务在运行")
        require(
            not self.workbench.tasks.prompt_busy(self.target),
            "PROJECT_BUSY",
            "当前镜头的视频提示词正在编写，请完成后再生成视频",
        )
        self.validate_prompt_image_bindings()
        prompt = self.prompt.toPlainText().strip()
        if self.is_dual_scene and not prompt:
            prompt = DUAL_SHOT_PROMPT
        command = MediaService(self.workbench.output_dir).prepare_video(
            self.image_selection(),
            prompt,
            workflow_id=self.loaded_workflow_id,
            driving_video_id=self.driving_video["video_id"] if self.driving_video else None,
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
        if self.current_job and len(self.current_job["outputs"]) > 1:
            repository = MediaRepository(self.workbench.output_dir)
            menu = QMenu(self.open_video_button)
            paths = {}
            for number, result in enumerate(self.current_job["outputs"], 1):
                path = repository.resolve(result["relative_path"])
                action = menu.addAction(result.get("label", f"片段 {number:02d}"))
                action.setEnabled(path.is_file())
                paths[action] = path
            chosen = menu.exec(self.open_video_button.mapToGlobal(self.open_video_button.rect().bottomLeft()))
            if chosen is None:
                return
            self._play_video(paths[chosen])
            return
        require(
            self.video_path is not None and self.video_path.is_file(),
            "INVALID_INPUT",
            "这组输入还没有生成好的视频",
        )
        self._play_video(self.video_path)

    def _play_video(self, path: Path) -> None:
        require(
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))),
            "OPEN_LOCATION_FAILED",
            f"无法打开视频：{path}",
        )

    def require_editable_inputs(self) -> None:
        require(
            not self.workbench.tasks.prompt_busy(self.target),
            "PROJECT_BUSY",
            "当前镜头的视频提示词正在编写，请完成后再修改输入",
        )
