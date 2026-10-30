"""真实 Qt 窗口→后台任务→生产管线→事务保存→重新打开。

仅替换 OpenAI SDK 客户端（以及其配置）；不替换任务、生成器、校验器或仓库。
故障用例另外注入磁盘故障或可控时钟，不发起付费请求。
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from core.generation import api_providers
from core.generation.lore_repository import LoreRepository, CHARACTERS, FACTIONS, BODY, CONTRACT, encode
from core.generation.lore_run import list_receipts, RequestBudget, BudgetExhausted, current_content
from core.generation.design_contract import LORE_CONTRACT_START, LORE_CONTRACT_END
from core.gui.main_window import MainWindow
from core.gui.pages.lore_editor import LoreEditor
from core.gui.pages.lore_results import GenerationDraftEditor
from core.gui import params_store
from test_lore_cast import _character, _antagonist, _faction
from test_parameter_consistency import review_reply
from test_generation_parameter_ui import choose


CAST = {"characters": [_character(profession="外交官"), _antagonist()], "relationships": []}
CONTRACT_DATA = {"era": "未来", "technology_level": "城市科技", "world_constraints": ["不得太空战斗"],
                 "canonical_characters": [{"id": "C1", "name": "梁浩"}, {"id": "C2", "name": "郑娜敏"}],
                 "canonical_factions": [{"id": "F1", "name": _faction()["name"]}], "canonical_locations": []}
WORLD = "# 未来城市\n外交官通过谈判化解争端。\n" + LORE_CONTRACT_START + encode(CONTRACT_DATA) + LORE_CONTRACT_END


def response_for(prompt):
    if prompt.startswith("你是作品参数符合性检查员"):
        return review_reply(prompt)
    if "设计 1 个势力" in prompt:
        return encode({"factions": [_faction()]})
    if "设计 2 位主要人物" in prompt:
        return encode(CAST)
    if "现在生成世界观设定" in prompt:
        return WORLD
    if "完善关键人物" in prompt:
        return "人物在城中成长，逐渐认识和平协商的价值。"
    return "1. 静默之城\n2. 谈判者"


class ModelBoundary:
    def __init__(self):
        self.calls = []
        self.options = []
        self.reply = response_for

    def client(self, **options):
        self.options.append(options)
        owner = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            @property
            def chat(self):
                return SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def create(self, **kwargs):
                prompt = kwargs["messages"][-1]["content"]
                owner.calls.append(prompt)
                text = owner.reply(prompt)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

        return Client()

    def async_client(self, **options):
        synchronous = self.client(**options)
        class AsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def create(self, **kwargs):
                return synchronous.create(**kwargs)

            @property
            def chat(self):
                return SimpleNamespace(completions=SimpleNamespace(create=self.create))
        return AsyncClient()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def model(monkeypatch):
    boundary = ModelBoundary()
    monkeypatch.setattr(api_providers, "OpenAI", boundary.client)
    monkeypatch.setattr(api_providers, "AsyncOpenAI", boundary.async_client)
    monkeypatch.setattr(api_providers, "get_config", lambda *args: api_providers.APIConfig("hosted-llm", "http://offline/v1", "offline", "unused"))
    return boundary


@pytest.fixture
def window(qapp, tmp_path, model, monkeypatch):
    params_store.save(str(tmp_path), {"novel_title": "循环验收", "genre": "Sci-Fi", "subgenre": "Space Opera",
                                    "theme": "和平与外交", "protagonist_type": "Diplomat", "conflict_scale": "Galactic",
                                    "space_combat": False, "interstellar_travel": False, "multiple_species": False,
                                    "num_characters": 2, "num_factions": 1, "quality_loop": "off"})
    w = MainWindow()
    w.parameters_page.load(str(tmp_path))
    w.parameters_page.output_dir_edit.setText(str(tmp_path))
    w.refresh_all()
    w._navigate("lore")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    # 加载默认参数后保存一次，避免把其他电脑的运行配置混入本地窗口。
    assert w.parameters_page.save()
    yield w
    if w.runner.busy:
        w.runner.cancel_current()
        wait_for(qapp, lambda: w.runner.current is None)
    w.close()
    w.deleteLater()
    qapp.processEvents()


def wait_for(app, condition, timeout=30):
    deadline = time.monotonic() + timeout
    while not condition():
        app.processEvents()
        QTest.qWait(5)
        assert time.monotonic() < deadline, "后台任务未按预期退出"
    app.processEvents()


def click_step(w, qapp, action):
    button = w.lore_page.step_buttons.get(action)
    if button is not None:
        QTest.mouseClick(button, Qt.LeftButton)
    else:
        QTest.mouseClick(w.lore_page.generate_button if action == "full" else w.lore_page.titles_button, Qt.LeftButton)
    wait_for(qapp, lambda: w.runner.current is None)
    assert not w.lore_page.generation_busy


def latest(w):
    return list_receipts(w.output_dir())[0]


def test_characters_success_edit_reopen_and_next_prompt(window, qapp, model, tmp_path):
    click_step(window, qapp, "characters")
    result = latest(window)
    assert result["status"] == "complete" and result["calls"] == 2
    assert all(o["max_retries"] == 0 and 0 < o["timeout"] <= 120 for o in model.options)
    for prompt in model.calls:
        assert "外交官" in prompt and "太空战斗：不得引入" in prompt
    editor = LoreEditor(str(tmp_path), CHARACTERS, ("characters", 0))
    editor.field.children_fields["profession"].control.setText("和平谈判特使")
    assert editor.save()
    reopened = LoreEditor(str(tmp_path), CHARACTERS, ("characters", 0))
    assert reopened.field.children_fields["profession"].control.text() == "和平谈判特使"
    # 后续真实管线读取磁盘最新人物；人工内容预检也属于相同预算。
    click_step(window, qapp, "lore")
    assert any("和平谈判特使" in p and "现在生成世界观设定" in p for p in model.calls)
    assert latest(window)["status"] == "complete"
    assert (tmp_path / CONTRACT).exists()


@pytest.mark.parametrize("improving,calls", [(False, 4), (True, 6)])
def test_failed_checks_deliver_editable_current_and_one_manual_repair(window, qapp, model, tmp_path, improving, calls):
    def fail(prompt):
        if prompt.startswith("你是作品参数符合性检查员"):
            report = json.loads(review_reply(prompt, fail_key="space_combat", evidence="舰队交战"))
            if improving:
                # 每次减少一个明确违背；仍有改善时可以用完两次修复。
                failures = {"space_combat", "interstellar_travel", "multiple_species"}
                remaining = sorted(failures)[:max(1, 4 - len(model.calls) // 2)]
                for check in report["checks"]:
                    check["status"] = "fail" if check["parameter_key"] in remaining else "pass"
                    if check["status"] == "fail":
                        check.update(evidence="舰队交战", reason="仍存在该项违背")
            return encode(report)
        return encode({**CAST, "note": "舰队交战"})
    model.reply = fail
    click_step(window, qapp, "characters")
    record = latest(window)
    assert record["calls"] == calls == len(model.calls)
    assert record["status"] == "needs_changes" and record["disposition"] == "current"
    assert record["exit_reason"] == ("attempt_limit" if improving else "no_improvement")
    assert record["issues"] and "舰队交战" in record["issues"][0]
    assert "已生成，待修改" == window.status._state._text.text()
    assert (tmp_path / CHARACTERS).exists()
    assert window.lore_page.step_buttons["characters"].isEnabled()
    assert all(b.isEnabled() for b in window.lore_page.result_buttons)
    previous = len(model.calls)
    repair = next(b for b in window.lore_page.result_buttons if b.text() == "再修复一次")
    repair.click()
    wait_for(qapp, lambda: window.runner.current is None)
    assert len(model.calls) - previous == 2
    assert latest(window)["repairs"] == 1
    assert list((tmp_path / "system/lore_history").glob("*/backup.json"))
    restarted = MainWindow()
    restarted.parameters_page.load(str(tmp_path))
    restarted.parameters_page.output_dir_edit.setText(str(tmp_path))
    restarted.refresh_all()
    restarted._navigate("lore")
    assert list_receipts(restarted.output_dir())[0]["status"] == "needs_changes"
    assert "舰队交战" in current_content(str(tmp_path), latest(window))
    restarted.close()


@pytest.mark.parametrize("broken", ["not-json", "network"])
def test_review_unavailable_still_delivers(window, qapp, model, broken):
    def reply(prompt):
        if prompt.startswith("你是作品参数符合性检查员"):
            if broken == "network":
                raise RuntimeError("检查服务不可用")
            return "无法解析的检查报告"
        return encode(CAST)
    model.reply = reply
    click_step(window, qapp, "characters")
    assert latest(window)["status"] == "unchecked"
    assert latest(window)["calls"] == 2
    assert window.status._state._text.text() == "已生成，检查未完成"


def test_unparseable_candidate_old_file_preserved_and_offline_adoption(window, qapp, model, tmp_path):
    (tmp_path / CHARACTERS).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / CHARACTERS).write_text(encode(CAST), encoding="utf-8")
    model.reply = lambda _: '{"characters":[{"name":"梁浩","description":"谈判者的可提取正文"}],'
    click_step(window, qapp, "characters")
    record = latest(window)
    assert record["status"] == "draft" and "谈判者" in record["candidate"]
    assert json.loads((tmp_path / CHARACTERS).read_text(encoding="utf-8")) == CAST
    before = len(model.calls)
    editor = GenerationDraftEditor(str(tmp_path), record["id"])
    assert "谈判者的可提取正文" in editor.extracted.toPlainText()
    competing = GenerationDraftEditor(str(tmp_path), record["id"])
    competing.field.setPlainText("另一个窗口尚未保存的草稿")
    editor.field.setPlainText(encode({**CAST, "extra": "手动修复的字段"}))
    assert editor.adopt()
    assert len(model.calls) == before
    assert json.loads((tmp_path / CHARACTERS).read_text(encoding="utf-8"))["extra"] == "手动修复的字段"
    assert latest(window)["disposition"] == "current"
    assert not competing.save() and competing.dirty
    assert "其他窗口" in competing.message.text()


@pytest.mark.parametrize("reply_kind", ["empty", "timeout"])
def test_all_transport_and_empty_retries_share_six_request_limit(window, qapp, model, reply_kind):
    def unavailable(_):
        if reply_kind == "timeout":
            raise TimeoutError("模拟网络超时")
        return ""
    model.reply = unavailable
    click_step(window, qapp, "characters")
    assert len(model.calls) == latest(window)["calls"] == 6
    assert latest(window)["status"] == "failed"
    assert window.status._state._text.text() == "任务失败"
    assert window.lore_page.step_buttons["characters"].isEnabled()


def test_cancel_while_reviewing_retains_candidate_and_rejects_double_start(window, qapp, model, tmp_path):
    entered, release = threading.Event(), threading.Event()
    def reply(prompt):
        if prompt.startswith("你是作品参数符合性检查员"):
            entered.set()
            assert release.wait(8)
        return response_for(prompt)
    model.reply = reply
    QTest.mouseClick(window.lore_page.step_buttons["characters"], Qt.LeftButton)
    try:
        wait_for(qapp, entered.is_set)
        window.lore_page._run_step("characters")
        window.status._stop.click()
    finally:
        release.set()
    wait_for(qapp, lambda: window.runner.current is None)
    assert len(model.calls) == 2
    record = latest(window)
    assert record["status"] == "draft" and record["exit_reason"] == "cancelled"
    assert record["candidate"] and not (tmp_path / CHARACTERS).exists()


def test_external_write_during_generation_becomes_draft(window, qapp, model, tmp_path):
    def reply(prompt):
        if prompt.startswith("你是作品参数符合性检查员"):
            (tmp_path / CHARACTERS).write_text(encode({**CAST, "external": True}), encoding="utf-8")
        return response_for(prompt)
    model.reply = reply
    click_step(window, qapp, "characters")
    assert latest(window)["exit_reason"] == "external_change"
    assert json.loads((tmp_path / CHARACTERS).read_text(encoding="utf-8"))["external"] is True


@pytest.mark.parametrize("action,kind", [("factions", "factions"), ("lore", "world"), ("enhance", "background"), ("titles", "titles"), ("full", "titles")])
def test_each_remaining_function_and_full_lore_runs_production_stack(window, qapp, model, tmp_path, action, kind):
    repo = LoreRepository(tmp_path)
    repo._transaction({CHARACTERS: encode(CAST), FACTIONS: encode([_faction()]), BODY: "已有城市设定"}, "fixture")
    window.refresh_all()
    click_step(window, qapp, action)
    records = list_receipts(str(tmp_path))
    assert records and records[0]["kind"] == kind
    assert all(r["status"] == "complete" and r["calls"] <= 6 and r["disposition"] == "current" for r in records)
    if action == "titles":
        assert len(model.calls) == 1 and records[0]["calls"] == 1
    if action == "full":
        assert {r["kind"] for r in records} == {"factions", "characters", "world", "background", "titles"}
        assert len(records) == 6
    for record in records:
        assert current_content(str(tmp_path), record)


def test_time_budget_and_remaining_timeout_without_wall_clock_wait(window, qapp, model, monkeypatch):
    import core.generation.parameter_compliance as compliance
    now = [0.0]
    monkeypatch.setattr(compliance, "RequestBudget", lambda **kw: RequestBudget(clock=lambda: now[0], **kw))
    def reply(prompt):
        now[0] += 110
        if prompt.startswith("你是作品参数符合性检查员"):
            return review_reply(prompt, fail_key="space_combat", evidence="舰队交战")
        return encode({**CAST, "note": "舰队交战"})
    model.reply = reply
    click_step(window, qapp, "characters")
    assert len(model.calls) == 3
    assert [o["timeout"] for o in model.options] == [120, 120, 80]
    assert latest(window)["exit_reason"] == "budget_exhausted"
    assert latest(window)["disposition"] == "current"


@pytest.mark.parametrize("action", ["factions", "lore", "enhance"])
def test_remaining_functions_deliver_despite_repeated_rule_failure(window, qapp, model, tmp_path, action):
    LoreRepository(tmp_path)._transaction({CHARACTERS: encode(CAST), BODY: "现有世界", CONTRACT: encode(CONTRACT_DATA)}, "fixture")
    def reply(prompt):
        if prompt.startswith("你是作品参数符合性检查员"):
            return review_reply(prompt, fail_key="space_combat", evidence="舰队交战")
        raw = response_for(prompt)
        if action == "factions":
            return encode({"factions": [_faction(description="舰队交战")]})
        return "舰队交战。\n" + raw
    model.reply = reply
    window.refresh_all()
    click_step(window, qapp, action)
    records = list_receipts(str(tmp_path))
    assert records and all(r["status"] == "needs_changes" and r["calls"] <= 6 and r["disposition"] == "current" for r in records)
    assert all(r["exit_reason"] == "no_improvement" for r in records)


def test_save_failure_rolls_back_and_keeps_candidate(window, qapp, model, tmp_path, monkeypatch):
    repo = LoreRepository(tmp_path)
    repo._transaction({CHARACTERS: encode({**CAST, "old": True})}, "fixture")
    original = LoreRepository._atomic
    failed = []
    def atomic(path, content):
        if path == tmp_path / CHARACTERS and not failed:
            failed.append(True)
            raise OSError("模拟磁盘写入失败")
        return original(path, content)
    monkeypatch.setattr(LoreRepository, "_atomic", staticmethod(atomic))
    click_step(window, qapp, "characters")
    assert failed and window.status._state._text.text() == "任务失败"
    assert latest(window)["candidate"] and latest(window)["exit_reason"] == "save_failed"
    assert json.loads((tmp_path / CHARACTERS).read_text(encoding="utf-8"))["old"] is True


def test_invalid_raw_then_empty_replies_retains_last_nonempty_candidate(window, qapp, model):
    model.reply = lambda _: "待修复原始人物内容" if len(model.calls) == 1 else ""
    click_step(window, qapp, "characters")
    assert latest(window)["status"] == "draft"
    assert latest(window)["candidate"] == "待修复原始人物内容"
    assert latest(window)["calls"] == 6


def test_world_without_registry_keeps_old_registry_and_delivers_body(window, qapp, model, tmp_path):
    repo = LoreRepository(tmp_path)
    repo._transaction({CHARACTERS: encode(CAST), CONTRACT: encode(CONTRACT_DATA)}, "fixture")
    model.reply = lambda _: "新的世界观正文，没有登记表契约。"
    click_step(window, qapp, "lore")
    assert latest(window)["status"] == "needs_changes"
    assert (tmp_path / BODY).read_text(encoding="utf-8") == "新的世界观正文，没有登记表契约。"
    assert json.loads((tmp_path / CONTRACT).read_text(encoding="utf-8")) == CONTRACT_DATA


@pytest.mark.parametrize("choice,expected", [("保存并生成", "新主题"), ("使用已保存参数", "和平与外交"), ("取消", None)])
def test_parameter_choice_through_production_pipeline(window, qapp, model, monkeypatch, choice, expected):
    window.parameters_page.theme_edit.setText("新主题")
    choose(monkeypatch, choice)
    click_step(window, qapp, "characters")
    if expected is None:
        assert not model.calls
    else:
        assert expected in model.calls[0]
        assert latest(window)["status"] == "complete"
    assert window.parameters_page.theme_edit.text() == "新主题"


def test_draft_edit_conflict_keeps_text_and_disk_content(window, qapp, model, tmp_path):
    model.reply = lambda _: "无法解析的原始人物内容"
    click_step(window, qapp, "characters")
    editor = GenerationDraftEditor(str(tmp_path), latest(window)["id"])
    editor.field.setPlainText(encode(CAST))
    (tmp_path / BODY).write_text("外部新增设定", encoding="utf-8")
    assert not editor.adopt() and editor.dirty
    assert "仍保留" in editor.message.text()
    assert not (tmp_path / CHARACTERS).exists()


def test_real_sdk_deadline_cancels_slow_request(qapp, tmp_path, monkeypatch):
    import asyncio
    import httpx
    from openai import AsyncOpenAI
    from core.generation import ai_helper
    config = api_providers.APIConfig("hosted-llm", "https://offline.example/v1", "offline", "unused")
    monkeypatch.setattr(api_providers, "get_config", lambda *args: config)
    cancelled = []
    async def slow_transport(request):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        pytest.fail("慢速请求应被墙钟预算中断")
    monkeypatch.setattr(api_providers, "AsyncOpenAI", lambda **kw: AsyncOpenAI(**kw,
                        http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow_transport))))
    ai_helper.set_backend("api", "hosted-llm")
    budget = RequestBudget(seconds=0.08)
    started = time.monotonic()
    with budget.activate(), pytest.raises(TimeoutError):
        ai_helper.send_prompt("测试整次请求超时", model="hosted-llm")
    assert cancelled and time.monotonic() - started < 2
    assert budget.calls == 1
    with pytest.raises(BudgetExhausted):
        budget.check()


def test_interrupted_record_is_recovered_as_draft_on_reopen(window, qapp, tmp_path):
    from core.generation.lore_run import save_receipt
    identity = "a" * 32
    save_receipt(tmp_path, {"id": identity, "kind": "characters", "candidate": "中断时已返回的内容", "status": "running"})
    window.lore_page.refresh()
    record = latest(window)
    assert record["status"] == "draft" and record["exit_reason"] == "interrupted"
    assert record["candidate"] == "中断时已返回的内容"


def test_world_second_write_failure_rolls_back_body_and_receipt(window, qapp, model, tmp_path, monkeypatch):
    repo = LoreRepository(tmp_path)
    repo._transaction({BODY: "旧世界正文", CONTRACT: encode(CONTRACT_DATA), CHARACTERS: encode(CAST)}, "fixture")
    atomic = LoreRepository._atomic
    failed = []
    def write(path, content):
        if path == tmp_path / CONTRACT and not failed:
            failed.append(True)
            raise OSError("登记表保存故障")
        return atomic(path, content)
    monkeypatch.setattr(LoreRepository, "_atomic", staticmethod(write))
    click_step(window, qapp, "lore")
    assert (tmp_path / BODY).read_text(encoding="utf-8") == "旧世界正文"
    assert json.loads((tmp_path / CONTRACT).read_text(encoding="utf-8")) == CONTRACT_DATA
    assert latest(window)["disposition"] == "draft" and latest(window)["exit_reason"] == "save_failed"


def test_background_rename_then_repair_reads_latest_manual_version(window, qapp, model, tmp_path):
    LoreRepository(tmp_path)._transaction({CHARACTERS: encode(CAST), BODY: "现有世界"}, "fixture")
    window.refresh_all()
    click_step(window, qapp, "enhance")
    editor = LoreEditor(str(tmp_path), CHARACTERS, ("characters", 0))
    editor.field.children_fields["name"].control.setText("林远")
    assert editor.save()
    target = "story/lore/background_protagonist_林远.md"
    background = LoreEditor(str(tmp_path), target)
    background.field.setPlainText("人工保存的成长经历，保留这句独有的纪念。")
    assert background.save()
    record = next(r for r in list_receipts(tmp_path) if r.get("subject", {}).get("name") == "林远")
    assert record["target"] == target and record["files"] == [target]
    assert "独有的纪念" in current_content(tmp_path, record)
    before = len(model.calls)
    window.lore_page._repair_generation_result(record["id"])
    wait_for(qapp, lambda: window.runner.current is None)
    generated = [p for p in model.calls[before:] if not p.startswith("你是作品参数符合性检查员")]
    assert len(generated) == 1 and "独有的纪念" in generated[0] and "林远" in generated[0]
    assert latest(window)["subject"]["name"] == "林远"
    assert not (tmp_path / "story/lore/background_protagonist_梁浩.md").exists()
    count = len(list_receipts(tmp_path))
    window.lore_page._regenerate_generation_result(latest(window)["id"])
    wait_for(qapp, lambda: window.runner.current is None)
    assert len(list_receipts(tmp_path)) == count + 1
    assert latest(window)["subject"]["name"] == "林远"


def test_repairs_reserve_request_for_recheck(window, qapp, model, tmp_path):
    # 人工上游预检消耗一次，初稿+检查两次，修复+检查两次：只剩一次时不再修复。
    repo = LoreRepository(tmp_path)
    repo._transaction({FACTIONS: encode([_faction()])}, "fixture")
    snapshot = repo.load_snapshot()
    data = json.loads(snapshot.files[FACTIONS])
    data[0]["description"] = "人工修改的组织说明"
    repo.save(snapshot, {FACTIONS: encode(data)})
    reviews = []
    def reply(prompt):
        if prompt.startswith("你是作品参数符合性检查员"):
            if "## 候选内容\n## " in prompt:
                return review_reply(prompt)
            reviews.append(prompt)
            report = json.loads(review_reply(prompt, fail_key="space_combat", evidence="舰队交战"))
            if len(reviews) == 1:
                next(c for c in report["checks"] if c["parameter_key"] == "interstellar_travel").update(
                    status="fail", evidence="舰队交战", reason="仍有违背")
            return encode(report)
        return encode({**CAST, "note": "舰队交战"})
    model.reply = reply
    window.refresh_all()
    click_step(window, qapp, "characters")
    assert len(model.calls) == latest(window)["calls"] == 5
    assert latest(window)["exit_reason"] == "budget_exhausted"
    assert latest(window)["status"] == "needs_changes"
    assert "修复和复查" in latest(window)["issues"][-1]
