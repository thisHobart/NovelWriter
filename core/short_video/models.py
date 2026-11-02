"""Public inputs and JSON helpers for short-video projects."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math


DIMENSIONS = ("continuity", "details", "language", "visual", "rhythm")
DIMENSION_LABELS = dict(zip(DIMENSIONS, ("连贯性", "细节", "语言", "视频表达", "节奏")))


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def parse_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    value = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(
        ValueError(f"不允许数值 {value}")))
    if not isinstance(value, dict):
        raise ValueError("需要 JSON 对象")
    return value


@dataclass(frozen=True)
class ScriptRequirements:
    title: str = "未命名短视频"
    idea: str = ""
    audience: str = "普通中文观众"
    genre: str = "剧情"
    tone: str = "自然、克制"
    duration_seconds: int = 60
    ending: str = "回应核心冲突"
    character_limit: int = 3
    location_limit: int = 2
    required: str = ""
    forbidden: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "ScriptRequirements":
        data = {name: value[name] for name in cls.__dataclass_fields__ if name in value}
        result = cls(**data)
        for name in ("title", "idea", "audience", "genre", "tone", "ending", "required", "forbidden"):
            if not isinstance(getattr(result, name), str):
                raise ValueError(f"{name} 必须是文字")
        if not result.title.strip() or not result.idea.strip():
            raise ValueError("请填写标题和创意")
        for name, lower, upper in (("duration_seconds", 30, 90), ("character_limit", 1, 8),
                                   ("location_limit", 1, 6)):
            number = getattr(result, name)
            if isinstance(number, bool) or not isinstance(number, int) or not lower <= number <= upper:
                raise ValueError(f"{name} 必须是 {lower}–{upper} 之间的整数")
        return result

    def to_dict(self) -> dict:
        return asdict(self)


def example_requirements() -> ScriptRequirements:
    """Editable UI seed for a new project; loading it does not save or generate anything."""
    return ScriptRequirements(
        title="雨夜借伞",
        idea=("雨夜，便利店门口。刚下班的林青伸手借最后一把共享伞，店员小周却把伞收回，"
              "林青以为他不愿借。小周翻过伞柄，露出松动的接头，用柜台旁的胶带固定好后递给她。"
              "林青撑开伞，主动把伞沿移向正在锁门的小周，等他一同离开。"
              "围绕这次小误会写一个完整故事，信息通过动作逐步揭示。"),
        audience="喜欢日常温情故事的中文短视频观众",
        genre="都市生活 / 温情剧情",
        tone="自然、克制，有生活细节，轻微幽默，不刻意煽情",
        duration_seconds=60,
        ending="林青主动为锁门的小周遮雨，用一个动作回应开头的误会，结尾温暖但不说教。",
        character_limit=2,
        location_limit=1,
        required=("仅林青和小周两位出镜人物；地点始终是便利店门口。\n"
                  "共享伞、松动的伞柄接头、柜台旁的胶带；先展示收伞造成的误会，再揭示原因。\n"
                  "修伞和递伞有具体可见动作；对白简短、口语化；标明分场预计时长。"),
        forbidden=("不增加其他出镜人物或地点，不加入突然出现的道具。\n"
                   "不靠内心独白或大段旁白解释动机；不写鸡汤口号、夸张反转或镜头制作提示词。"),
    )


def positive_seconds(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)
