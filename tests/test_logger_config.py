"""Tests for application log wiring.

撰写阶段的智能体和质量闭环用的是自己的模块级 logger（`ChapterWritingAgent`、
`chapter-generation-loop`），既不是 'NovelWriterApp' 的子 logger，自己也没有
handler。它们不接到 root 上，INFO 会被直接丢弃、ERROR 只经 logging.lastResort
打到 stderr——真正卡停写作的原因就永远进不了 application.log。
"""

import logging

import pytest

from core.config.logger_config import setup_app_logger


@pytest.fixture
def isolated_logging():
    """跑完把全局 logging 状态还原，免得污染其他测试。"""
    root = logging.getLogger()
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    app_logger = logging.getLogger("NovelWriterApp")
    saved_app_handlers = list(app_logger.handlers)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_root_handlers:
        root.addHandler(handler)
    root.setLevel(saved_root_level)
    for handler in list(app_logger.handlers):
        handler.close()
        app_logger.removeHandler(handler)
    for handler in saved_app_handlers:
        app_logger.addHandler(handler)


def _log_text(output_dir):
    logging.shutdown()
    return (output_dir / "application.log").read_text(encoding="utf-8")


def test_module_loggers_reach_the_application_log(tmp_path, isolated_logging):
    app_logger = setup_app_logger(output_dir=str(tmp_path))

    logging.getLogger("ChapterWritingAgent").error("第 5 章被质量闸门拦下")
    logging.getLogger("chapter-generation-loop").warning("场景 1 差分放行")
    app_logger.info("应用自身的记录")

    text = _log_text(tmp_path)
    assert "第 5 章被质量闸门拦下" in text
    assert "场景 1 差分放行" in text
    assert "应用自身的记录" in text


def test_records_are_not_written_twice(tmp_path, isolated_logging):
    """具名 logger 和 root 共用同一批 handler，重复初始化也不该重复落盘。"""
    app_logger = setup_app_logger(output_dir=str(tmp_path))
    app_logger.info("只应出现一次")
    setup_app_logger(output_dir=str(tmp_path))
    logging.getLogger("ChapterWritingAgent").error("重新初始化之后")

    text = _log_text(tmp_path)
    assert text.count("只应出现一次") == 1
    assert text.count("重新初始化之后") == 1


def test_http_client_chatter_is_kept_out_of_the_log(tmp_path, isolated_logging):
    setup_app_logger(output_dir=str(tmp_path))

    logging.getLogger("httpx").info("HTTP Request: POST /v1/messages 200 OK")
    logging.getLogger("httpx").warning("连接重试")

    text = _log_text(tmp_path)
    assert "HTTP Request" not in text
    assert "连接重试" in text
