"""Deterministic structure checks, separate from semantic model reviews."""
from __future__ import annotations

from core.short_video.models import DIMENSIONS, encode, positive_seconds


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"缺少有效的 {label}")


def _rows(value, label):
    if not isinstance(value, list) or not value or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{label} 必须是非空对象数组")
    ids = []
    for row in value:
        _text(row.get("id"), f"{label} id")
        ids.append(row["id"])
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label} id 重复")
    return set(ids)


def validate_story(value: dict, requirements: dict, kind: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("内容必须是对象")
    _text(value.get("title"), "标题")
    character_ids = _rows(value.get("characters"), "人物")
    location_ids = _rows(value.get("locations"), "场景地点")
    if len(character_ids) > requirements["character_limit"] or len(location_ids) > requirements["location_limit"]:
        raise ValueError("人物或场景地点数量超过创作要求")
    for row in value["characters"]:
        for key in ("name", "motivation", "appearance"):
            _text(row.get(key), f"人物 {key}")
    for row in value["locations"]:
        _text(row.get("name"), "地点名称")
    rows_key = "beats" if kind == "outline" else "scenes"
    _rows(value.get(rows_key), rows_key)
    if kind == "outline":
        _text(value.get("conflict"), "核心冲突")
        _text(value.get("ending"), "结尾")
    for row in value[rows_key]:
        _text(row.get("action"), "可见行动")
        if not positive_seconds(row.get("duration_seconds")):
            raise ValueError("每段预计时长必须是有限正数")
        if kind == "script":
            if not isinstance(row.get("location_id"), str) or row["location_id"] not in location_ids:
                raise ValueError("场次引用了未知地点")
            cast = row.get("character_ids")
            if (not isinstance(cast, list) or not cast or any(not isinstance(c, str) for c in cast)
                    or not set(cast) <= character_ids):
                raise ValueError("场次引用了未知人物")
            dialogue = row.get("dialogue")
            if not isinstance(dialogue, list):
                raise ValueError("dialogue 必须是数组，可为空")
            for line in dialogue:
                if not isinstance(line, dict) or line.get("character_id") not in cast:
                    raise ValueError("对白人物不在本场")
                _text(line.get("text"), "对白")
            for key in ("narration", "sound"):
                if not isinstance(row.get(key), str):
                    raise ValueError(f"{key} 必须是文字，可为空")
    return value


def local_issues(value: dict, requirements: dict) -> list[dict]:
    total = sum(row["duration_seconds"] for row in value.get("scenes", value.get("beats", [])))
    target = requirements["duration_seconds"]
    if abs(total - target) <= max(3, target * 0.15):
        return []
    return [{"code": "DURATION", "severity": "hard", "quote": "",
             "suggestion": f"预计总时长 {total:g} 秒，请调整到目标 {target} 秒附近。", "target": "script"}]


def script_issues(value: dict, requirements: dict, outline: dict) -> list[dict]:
    issues = local_issues(value, requirements)
    for key in ("characters", "locations"):
        if ({row["id"]: row for row in value[key]} != {row["id"]: row for row in outline[key]}):
            issues.append({"code": "IDENTITY", "severity": "hard", "quote": "",
                           "suggestion": f"恢复确认大纲中的 {key}，保持身份、外观和地点一致。", "target": "script"})
    return issues


def validate_review(value: dict, script: dict) -> dict:
    scores = value.get("scores")
    if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
        raise ValueError("评审缺少五个评分维度")
    for score in scores.values():
        if not positive_seconds(score) or not 1 <= score <= 5:
            raise ValueError("评分必须在 1–5 之间")
    issues = value.get("issues")
    if not isinstance(issues, list):
        raise ValueError("评审 issues 必须是数组")
    script_text = encode(script) + "\n" + to_markdown(script)
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError("评审条目必须是对象")
        for key in ("code", "quote", "suggestion"):
            _text(issue.get(key), f"评审 {key}")
        if issue.get("severity") not in {"hard", "soft"} or issue.get("target") not in {"script", "outline"}:
            raise ValueError("评审问题类型无效")
        if issue["quote"] not in script_text:
            raise ValueError("评审引文未在当前脚本中找到")
    return {"scores": scores, "issues": issues}


def passed(review: dict | None, issues: list) -> bool:
    return bool(review is not None and not any(i["severity"] == "hard" for i in issues)
                and all(score >= 3 for score in review["scores"].values()))


def candidate_rank(version: dict) -> tuple:
    review = version.get("review")
    issues = version.get("issues", [])
    return (version.get("status") == "reviewed",
            -sum(i["severity"] == "hard" for i in issues),
            sum(review["scores"].values()) if review else 0)


def to_markdown(value: dict, kind: str = "script") -> str:
    names = {row["id"]: row["name"] for row in value["characters"]}
    locations = {row["id"]: row["name"] for row in value["locations"]}
    rows = value["beats" if kind == "outline" else "scenes"]
    total = sum(row["duration_seconds"] for row in rows)
    lines = [f"# {value['title']}", "", f"预计总时长：{total:g} 秒（估计，非实际成片时长）", "", "## 人物"]
    for row in value["characters"]:
        lines.append(f"- {row['name']}：{row['motivation']}；外观：{row['appearance']}")
    lines.extend(["", "## 地点", *[f"- {row['name']}" for row in value["locations"]]])
    if kind == "outline":
        lines.extend(["", f"核心冲突：{value['conflict']}"])
    for index, row in enumerate(rows, 1):
        location = f" · {locations[row['location_id']]}" if kind == "script" else ""
        lines.extend(["", f"## 第 {index} 场{location} · 预计 {row['duration_seconds']:g} 秒", "", row["action"]])
        if kind == "script":
            lines.extend(f"\n**{names[line['character_id']]}**：{line['text']}" for line in row["dialogue"])
            if row["narration"]:
                lines.append(f"\n旁白：{row['narration']}")
            if row["sound"]:
                lines.append(f"\n声音：{row['sound']}")
    if kind == "outline":
        lines.extend(["", f"结尾：{value['ending']}"])
    return "\n".join(lines) + "\n"
