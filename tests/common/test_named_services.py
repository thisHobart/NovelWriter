# -*- coding: utf-8 -*-
"""模型服务配置：服务商下拉框里自己添加、命名第三方服务，地址和密钥保存一次，之后直接切换。"""
import json
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面冒烟测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.gui import theme  # noqa: E402

ADD = "+ 添加服务商…"


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(theme.build_qss())
    yield instance


@pytest.fixture
def settings(monkeypatch, tmp_path):
    from core.common import api_settings

    path = tmp_path / "llm_settings.json"
    monkeypatch.setattr(api_settings, "settings_file", lambda: path)
    for variable in ("HOSTED_LLM_BASE_URL", "HOSTED_LLM_URL", "HOSTED_LLM_API_KEY", "HOSTED_LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    return path


def open_dialog(monkeypatch, provider="hosted-llm", names=()):
    from core.gui.model_config_dialog import ModelConfigDialog

    dialog = ModelConfigDialog(provider)
    typed = iter(names)
    monkeypatch.setattr(dialog, "ask_name", lambda title, name="": next(typed))
    return dialog


def entries(dialog):
    combo = dialog.provider_combo
    return [combo.itemText(index) for index in range(combo.count()) if combo.itemText(index)]


def choose(dialog, text):
    dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findText(text))


def fill(dialog, url, key, model):
    dialog.url_edit.setText(url)
    dialog.key_edit.setText(key)
    dialog.model_edit.setText(model)


def shown(dialog):
    return dialog.url_edit.text(), dialog.key_edit.text(), dialog.model_edit.text()


def test_add_name_and_switch_services_without_typing_again(app, monkeypatch, settings):
    from core.common import api_providers, api_settings

    api_settings.save_provider("hosted-llm", "http://127.0.0.1:8000/v1", "", "local-model")
    dialog = open_dialog(monkeypatch, names=["1pkapi", "备用中转"])
    # The one third-party service there was keeps its old label, first in the list.
    assert entries(dialog) == ["第三方 API", "DeepSeek 官方", "千问官方", ADD]
    assert shown(dialog) == ("http://127.0.0.1:8000/v1", "", "local-model")

    # 「+ 添加服务商…」 asks for a name and opens an empty service under it.
    choose(dialog, ADD)
    assert entries(dialog) == ["第三方 API", "1pkapi", "DeepSeek 官方", "千问官方", ADD]
    assert dialog.selected_name() == "1pkapi" and dialog.selected_provider() == "hosted-llm"
    assert shown(dialog) == ("", "", "")
    fill(dialog, "https://1pkapi.com/v1", "sk-one", "gpt-6.2-sol")
    choose(dialog, ADD)
    fill(dialog, "https://relay.example.com/v1", "sk-two", "relay-model")

    # Switching back and forth inside the dialog keeps what was typed for each.
    choose(dialog, "1pkapi")
    assert shown(dialog) == ("https://1pkapi.com/v1", "sk-one", "gpt-6.2-sol")
    choose(dialog, "第三方 API")
    assert shown(dialog) == ("http://127.0.0.1:8000/v1", "", "local-model")
    choose(dialog, "1pkapi")
    dialog.save_button.click()
    assert dialog.result() == dialog.DialogCode.Accepted.value
    config = api_providers.get_config("hosted-llm")
    assert (config.base_url, config.api_key, config.model) == ("https://1pkapi.com/v1", "sk-one", "gpt-6.2-sol")

    # Next time: the service in use is shown, and the others are one click away.
    dialog = open_dialog(monkeypatch)
    assert entries(dialog) == ["第三方 API", "1pkapi", "备用中转", "DeepSeek 官方", "千问官方", ADD]
    assert dialog.selected_name() == "1pkapi"
    choose(dialog, "备用中转")
    assert shown(dialog) == ("https://relay.example.com/v1", "sk-two", "relay-model")
    dialog.save_button.click()
    config = api_providers.get_config("hosted-llm")
    assert (config.base_url, config.api_key, config.model) == ("https://relay.example.com/v1", "sk-two", "relay-model")
    assert open_dialog(monkeypatch).selected_name() == "备用中转"


def test_an_official_service_leaves_the_named_ones_and_the_one_in_use_alone(app, monkeypatch, settings):
    from core.common import api_providers, api_settings

    dialog = open_dialog(monkeypatch, names=["1pkapi"])
    choose(dialog, ADD)
    fill(dialog, "https://1pkapi.com/v1", "sk-one", "gpt-6.2-sol")
    dialog.save_button.click()
    dialog = open_dialog(monkeypatch, provider="deepseek")
    assert dialog.selected_name() == "DeepSeek 官方" and not dialog.rename_button.isEnabled()
    dialog.key_edit.setText("sk-deepseek")
    dialog.save_button.click()
    assert dialog.selected_provider() == "deepseek"
    assert api_providers.get_config("deepseek").api_key == "sk-deepseek"
    assert api_providers.get_config("hosted-llm").base_url == "https://1pkapi.com/v1"
    assert api_settings.named_services()["active"] == "1pkapi"


def test_rename_refuses_a_name_already_in_the_list(app, monkeypatch, settings):
    from core.common import api_settings

    dialog = open_dialog(monkeypatch, names=["1pkapi", "第三方 API", "DeepSeek 官方", "  主力  "])
    choose(dialog, ADD)
    fill(dialog, "https://1pkapi.com/v1", "sk-one", "gpt-6.2-sol")
    dialog.rename_button.click()
    assert dialog.error_label.text() == "已有名为「第三方 API」的服务商"
    dialog.rename_button.click()
    assert dialog.error_label.text() == "已有名为「DeepSeek 官方」的服务商"
    dialog.rename_button.click()
    assert dialog.error_label.isHidden()
    assert entries(dialog)[:2] == ["第三方 API", "主力"] and dialog.selected_name() == "主力"
    assert shown(dialog) == ("https://1pkapi.com/v1", "sk-one", "gpt-6.2-sol")
    dialog.save_button.click()
    assert api_settings.named_services() == {"active": "主力", "items": [
        {"name": "第三方 API", "base_url": "http://127.0.0.1:8000/v1", "api_key": "", "model": ""},
        {"name": "主力", "base_url": "https://1pkapi.com/v1", "api_key": "sk-one", "model": "gpt-6.2-sol"},
    ]}


def test_cancelling_leaves_the_list_as_it_was_and_delete_takes_effect_on_save(app, monkeypatch, settings):
    from core.common import api_settings

    dialog = open_dialog(monkeypatch, names=["1pkapi", None])
    choose(dialog, ADD)
    fill(dialog, "https://1pkapi.com/v1", "sk-one", "gpt-6.2-sol")
    dialog.save_button.click()

    # A cancelled name prompt adds nothing and keeps the service on screen.
    dialog = open_dialog(monkeypatch, names=[None])
    choose(dialog, ADD)
    assert dialog.selected_name() == "1pkapi" and len(entries(dialog)) == 5
    # 「删除」 then 「取消」: still there next time.
    dialog.delete_button.click()
    assert entries(dialog) == ["第三方 API", "DeepSeek 官方", "千问官方", ADD]
    assert dialog.selected_name() == "第三方 API"
    dialog.cancel_button.click()
    dialog = open_dialog(monkeypatch)
    assert "1pkapi" in entries(dialog)
    # 「删除」 then 「保存配置」: gone, and the service shown is the one in use.
    dialog.delete_button.click()
    dialog.model_edit.setText("local-model")
    dialog.save_button.click()
    assert dialog.result() == dialog.DialogCode.Accepted.value
    assert [item["name"] for item in api_settings.named_services()["items"]] == ["第三方 API"]
    assert api_settings.named_services()["active"] == "第三方 API"


def test_a_wrong_address_in_another_service_blocks_saving_and_writes_nothing(app, monkeypatch, settings):
    dialog = open_dialog(monkeypatch, names=["坏地址"])
    choose(dialog, ADD)
    fill(dialog, "1pkapi.com", "sk-one", "gpt-6.2-sol")
    choose(dialog, "第三方 API")
    dialog.model_edit.setText("local-model")
    dialog.save_button.click()
    assert dialog.error_label.text() == "ValueError: 「坏地址」：接口地址 必须是完整的 HTTP/HTTPS API 地址"
    assert dialog.result() != dialog.DialogCode.Accepted.value
    assert not settings.exists()


def test_a_broken_service_list_shows_the_error_and_keeps_saving_off(app, monkeypatch, settings):
    settings.write_text(json.dumps({"version": 1, "providers": {}, "named_services": {"items": 3}}),
                        encoding="utf-8")
    dialog = open_dialog(monkeypatch)
    assert "服务商列表格式不正确" in dialog.error_label.text()
    assert not dialog.save_button.isEnabled() and not dialog.rename_button.isEnabled()
