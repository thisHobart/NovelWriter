from copy import deepcopy
import pytest

from core.short_video.models import fingerprint
from core.short_video.repository import ScriptRepository


def prepare(repo, requirements, outline):
    repo.save_requirements(requirements)
    repo.save_outline(outline)
    repo.confirm_outline()


def test_reads_are_lazy_and_novel_files_untouched(tmp_path):
    novel = tmp_path / "story" / "content" / "chapter_1.md"
    novel.parent.mkdir(parents=True)
    novel.write_text("小说正文", encoding="utf-8")
    repo = ScriptRepository(tmp_path)
    repo.load()
    repo.recover()
    assert not repo.root.exists()
    assert novel.read_text(encoding="utf-8") == "小说正文"


def test_confirmation_staleness_and_restart(tmp_path, video_requirements, video_outline, video_script):
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    state = repo.load()
    version = repo.save_manual(video_script, repo.source(state))
    repo.finalize(version["id"])
    reopened = ScriptRepository(tmp_path)
    assert reopened.version()["finalized"]
    changed = dict(video_requirements, tone="紧张")
    reopened.save_requirements(changed)
    assert not reopened.outline_ready(reopened.load())
    assert reopened.version()["source"] != reopened.source(reopened.load())
    with pytest.raises(ValueError, match="过期"):
        reopened.finalize(version["id"])
    assert len(reopened.load()["versions"]) == 1


def test_edit_outline_invalidates_confirmation_but_identical_save_preserves(tmp_path, video_requirements, video_outline):
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    repo.save_outline(video_outline)
    assert repo.outline_ready(repo.load())
    video_outline["ending"] = "她决定当面询问父亲。"
    repo.save_outline(video_outline)
    assert not repo.outline_ready(repo.load())


def test_optimistic_outline_save_rejects_other_window(tmp_path, video_requirements, video_outline):
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    old_hash = fingerprint(repo.load()["outline"])
    changed = deepcopy(video_outline)
    changed["ending"] = "等待家人到来"
    repo.save_outline(changed)
    with pytest.raises(ValueError, match="其他窗口"):
        repo.save_outline(video_outline, expected=old_hash)


def test_failed_replace_preserves_old_project(tmp_path, monkeypatch, video_requirements):
    from core.short_video import repository
    repo = ScriptRepository(tmp_path)
    repo.save_requirements(video_requirements)
    original = repo.path.read_bytes()
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(repository.os, "replace", fail)
    with pytest.raises(OSError):
        repo.save_requirements(dict(video_requirements, tone="新基调"))
    assert repo.path.read_bytes() == original
    assert not list(repo.root.glob(".script-*"))


def test_recover_run_does_not_make_requests(tmp_path, video_requirements):
    repo = ScriptRepository(tmp_path)
    repo.save_requirements(video_requirements)
    run = repo.start_run("outline", repo.load(), "hosted-llm")
    repo.record_call(run, "prompt", "未解析草稿", None)
    ScriptRepository(tmp_path).recover()
    assert repo.load()["runs"][0]["status"] == "interrupted"
    assert repo.load()["runs"][0]["calls"][0]["response"] == "未解析草稿"


def test_manual_versions_and_export(tmp_path, video_requirements, video_outline, video_script):
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    source = repo.source(repo.load())
    first = repo.save_manual(video_script, source)
    video_script["scenes"][0]["action"] = "她把包裹放到灯下。"
    second = repo.save_manual(video_script, source)
    assert first["id"] != second["id"]
    assert repo.version(first["id"])["content"]["scenes"][0]["action"] != video_script["scenes"][0]["action"]
    with pytest.raises(ValueError, match="数据目录"):
        repo.export(second["id"], repo.path)
    output = tmp_path / "稿件.md"
    repo.export(second["id"], output)
    text = output.read_text(encoding="utf-8")
    assert "候选稿" in text and "预计总时长" in text and "林青" in text


def test_confirmation_cannot_approve_an_unseen_external_edit(tmp_path, video_requirements, video_outline):
    repo = ScriptRepository(tmp_path)
    repo.save_requirements(video_requirements)
    repo.save_outline(video_outline)
    displayed = fingerprint(repo.load()["outline"])
    video_outline["ending"] = "新的结尾"
    repo.save_outline(video_outline)
    with pytest.raises(ValueError, match="重新载入"):
        repo.confirm_outline(expected=displayed)
    assert not repo.outline_ready(repo.load())


def test_run_snapshots_do_not_recursively_copy_history(tmp_path, video_requirements, video_outline):
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    for _ in range(3):
        run = repo.start_run("script", repo.load(), "hosted-llm")
        repo.finish_run(run, "complete")
    assert all(set(run["snapshot"]) == {"requirements", "outline"} for run in repo.load()["runs"])


def test_manual_save_and_selection_survive_without_a_second_write(tmp_path, monkeypatch,
                                                                 video_requirements, video_outline, video_script):
    from core.short_video import repository
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    replace = repository.os.replace
    writes = []
    def one_replace(*args):
        writes.append(args)
        if len(writes) > 1:
            raise OSError("second transaction unavailable")
        replace(*args)
    monkeypatch.setattr(repository.os, "replace", one_replace)
    saved = repo.save_manual(video_script, repo.source(repo.load()))
    assert repo.version()["id"] == saved["id"]
    assert len(repo.load()["versions"]) == 1


def test_manual_save_checks_current_source_inside_transaction(tmp_path, monkeypatch,
                                                             video_requirements, video_outline, video_script):
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    source = repo.source(repo.load())
    update = repo.update
    def concurrent_change(operation):
        ScriptRepository(tmp_path).save_requirements(dict(video_requirements, tone="其他窗口的新基调"))
        return update(operation)
    monkeypatch.setattr(repo, "update", concurrent_change)
    with pytest.raises(ValueError, match="创作依据已变化"):
        repo.save_manual(video_script, source)
    assert not repo.load()["versions"]


def test_completing_run_preserves_concurrent_manual_selection(tmp_path, monkeypatch,
                                                            video_requirements, video_outline, video_script):
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    source = repo.source(repo.load())
    run_id = repo.start_run("script", repo.load(), "hosted-llm")
    candidate = repo.add_version(video_script, source, run_id=run_id)
    update = repo.update
    authors = []
    def concurrent_save(operation):
        authors.append(ScriptRepository(tmp_path).save_manual(video_script, source))
        return update(operation)
    monkeypatch.setattr(repo, "update", concurrent_save)
    repo.complete_run(run_id, candidate["id"], "unchecked", 1)
    assert repo.version()["id"] == authors[0]["id"]
    assert repo.load()["runs"][-1]["selected_version"] == candidate["id"]


@pytest.mark.parametrize("damage", ["outline", "version", "run", "selection", "requirements"])
def test_damaged_nested_data_is_rejected_without_modification(tmp_path, video_requirements,
                                                            video_outline, video_script, damage):
    from core.short_video.models import encode
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    repo.save_manual(video_script, repo.source(repo.load()))
    run_id = repo.start_run("script", repo.load(), "hosted-llm")
    state = repo.load()
    if damage == "outline":
        state["outline"]["content"]["characters"] = None
    elif damage == "version":
        state["versions"][0]["status"] = "unknown"
    elif damage == "run":
        state["runs"][0].pop("calls")
    elif damage == "selection":
        state["selected_version"] = "missing"
    else:
        state["requirements"].pop("title")
    repo.path.write_text(encode(state), encoding="utf-8")
    original = repo.path.read_bytes()
    with pytest.raises(ValueError, match="损坏或不完整"):
        repo.recover()
    assert repo.path.read_bytes() == original


def test_stale_artifacts_remain_readable_after_stricter_new_limits(tmp_path, video_requirements,
                                                                video_outline, video_script):
    repo = ScriptRepository(tmp_path)
    video_outline["characters"].append({"id": "p2", "name": "父亲", "motivation": "与女儿和解", "appearance": "白衬衫"})
    video_script["characters"] = deepcopy(video_outline["characters"])
    prepare(repo, video_requirements, video_outline)
    repo.save_manual(video_script, repo.source(repo.load()))
    repo.save_requirements(dict(video_requirements, character_limit=1))
    assert len(ScriptRepository(tmp_path).version()["content"]["characters"]) == 2
    assert not repo.outline_ready(repo.load())


def test_recovery_respects_another_threads_live_generation(tmp_path, video_requirements):
    from threading import Event, Thread
    repo = ScriptRepository(tmp_path)
    repo.save_requirements(video_requirements)
    run_id = repo.start_run("outline", repo.load(), "hosted-llm")
    acquired, release = Event(), Event()
    def generating():
        with repo.generation_lock():
            acquired.set()
            release.wait(5)
    worker = Thread(target=generating)
    worker.start()
    try:
        assert acquired.wait(5)
        ScriptRepository(tmp_path).recover()
        assert repo.load()["runs"][0]["status"] == "running"
    finally:
        release.set()
        worker.join(5)
    repo.recover()
    assert repo.load()["runs"][0]["status"] == "interrupted"


def test_writing_video_in_legacy_novel_root_preserves_novel_files(tmp_path, video_requirements,
                                                              video_outline, video_script):
    from core.gui import params_store
    legacy = tmp_path / "parameters.txt"
    legacy.write_text("Novel Title: 原有小说\nGenre: Fantasy\n", encoding="utf-8")
    chapter = tmp_path / "story" / "content" / "chapter_1.md"
    chapter.parent.mkdir(parents=True)
    chapter.write_text("原有小说第一章。", encoding="utf-8")
    original = {path: path.read_bytes() for path in (legacy, chapter)}
    before = params_store.load(str(tmp_path))
    repo = ScriptRepository(tmp_path)
    prepare(repo, video_requirements, video_outline)
    version = repo.save_manual(video_script, repo.source(repo.load()))
    repo.finalize(version["id"])
    repo.export(version["id"], tmp_path / "短视频.md")
    assert all(path.read_bytes() == data for path, data in original.items())
    assert params_store.load(str(tmp_path)) == before
    assert before["Novel Title"] == "原有小说"
    assert ScriptRepository(tmp_path).version()["finalized"]
