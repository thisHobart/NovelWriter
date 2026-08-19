from core.gui.app import workflow_step_visual


def test_not_started_step_with_existing_files_is_not_shown_as_empty():
    visual = workflow_step_visual("not_started", True)

    assert visual["text"] == "检测到已有文件"
    assert visual["indicator"] == "◌"
    assert visual["can_view"] is True


def test_not_started_step_without_files_stays_not_started():
    visual = workflow_step_visual("not_started", False)

    assert visual["text"] == "未开始"
    assert visual["can_view"] is False


def test_persisted_status_takes_priority_over_file_presence():
    assert workflow_step_visual("in_progress", False)["text"] == "进行中"
    assert workflow_step_visual("completed", True)["text"] == "已完成"
    assert workflow_step_visual("failed", True)["text"] == "失败"
