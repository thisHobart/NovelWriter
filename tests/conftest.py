"""Test configuration: make the repo root importable.

NovelWriter is run from the repo root (python main.py), so absolute imports
like `core.generation.ai_helper` expect the root on sys.path.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Small independent story used by the short-video tests; never passed off as a live model result.
import pytest
from copy import deepcopy


@pytest.fixture
def video_requirements():
    from core.short_video.models import ScriptRequirements
    return ScriptRequirements(title="退回包裹", idea="快递员送来多年前寄出的包裹，收件人决定先确认寄件人的身份。").to_dict()


@pytest.fixture
def video_outline():
    return {"title": "退回包裹", "characters": [
        {"id": "p1", "name": "林青", "motivation": "确认包裹的寄件人", "appearance": "蓝色外套，短发"}],
        "locations": [{"id": "l1", "name": "公寓门口"}], "conflict": "林青想拆包裹，但寄件地址已不存在。",
        "beats": [{"id": "b1", "action": "林青接过旧包裹，发现邮戳已有十年。", "duration_seconds": 20},
                  {"id": "b2", "action": "她翻到背面，看见母亲手写的收件地址。", "duration_seconds": 20},
                  {"id": "b3", "action": "她轻轻放下剪刀，先拨通父亲的电话。", "duration_seconds": 20}],
        "ending": "她先联系家人确认，而非贸然拆开。"}


@pytest.fixture
def video_script(video_outline):
    return {"title": video_outline["title"], "characters": deepcopy(video_outline["characters"]),
            "locations": deepcopy(video_outline["locations"]), "scenes": [
        {"id": "s1", "location_id": "l1", "character_ids": ["p1"],
         "action": "林青接过包裹，把旧邮戳转向门口的灯。", "dialogue": [], "narration": "", "sound": "门锁轻响", "duration_seconds": 20},
        {"id": "s2", "location_id": "l1", "character_ids": ["p1"],
         "action": "她翻过包裹，手指停在母亲写下的地址上。", "dialogue": [], "narration": "", "sound": "纸张摩擦", "duration_seconds": 20},
        {"id": "s3", "location_id": "l1", "character_ids": ["p1"], "action": "她放下剪刀，拨通父亲的电话。",
         "dialogue": [{"character_id": "p1", "text": "爸，你还记得这个地址吗？"}],
         "narration": "", "sound": "电话接通", "duration_seconds": 20}]}


@pytest.fixture
def video_review():
    from core.short_video.models import DIMENSIONS
    return {"scores": {key: 4 for key in DIMENSIONS}, "issues": []}
