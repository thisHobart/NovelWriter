"""Test configuration: make the repo root importable.

NovelWriter is run from the repo root (python main.py), so absolute imports
like `core.generation.ai_helper` expect the root on sys.path.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def configure_video_test_model(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline workspace tests supply a complete model configuration explicitly."""
    if not (request.path.name.startswith("test_video_v2_") or request.path.name == "test_short_video_ui.py"):
        return
    from core.generation import api_providers
    monkeypatch.setattr(api_providers, "get_config", lambda provider: api_providers.APIConfig(
        provider, "https://offline.example/v1", "workspace-test-model", "test-only"))
