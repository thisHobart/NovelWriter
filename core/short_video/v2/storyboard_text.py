"""Copyable video instructions rendered from the current structured storyboard."""

from __future__ import annotations

from .contracts import JsonObject
from .models import require
from .validation import recalculate_timeline


def seconds(milliseconds: int) -> str:
    return f"{milliseconds // 1000}.{milliseconds % 1000:03}".rstrip("0").rstrip(".")


def render_storyboard_text(content: JsonObject, catalog: JsonObject) -> str:
    """Keep edited timings, identities and audio without calling a model or saving."""
    require(bool(content["shots"]), "INVALID_INPUT", "请先生成分镜")
    board = recalculate_timeline(content)
    entities = {entity["asset_id"]: entity for entity in catalog["entities"]}
    scenes, people, shots, audio = [], [], [], []

    def name(reference: JsonObject, labels: JsonObject) -> str:
        entity = entities[reference["asset_id"]]
        label = labels.get(entity["asset_id"], entity["name"])
        variant = next(
            variant for variant in entity["variants"]
            if variant["variant_id"] == reference["variant_id"]
        )
        return f"{label}（{variant['label']}）"

    for shot in board["shots"]:
        labels = shot.get("character_labels", {})
        scene = name(shot["scene_ref"], labels)
        characters = [name(ref, labels) for ref in shot["characters"]]
        props = [name(ref, labels) for ref in shot["props"]]
        scenes.append(scene)
        people.extend(characters)
        interval = f"[{seconds(shot['start_ms'])}s–{seconds(shot['end_ms'])}s]"
        details = [
            f"{shot['shot_size']}，{shot['camera_movement']}",
            f"场景：{scene}",
            f"人物：{'、'.join(characters) if characters else '无出镜人物'}",
        ]
        if props:
            details.append("道具：" + "、".join(props))
        details.append(shot["action"].strip())
        shots.append(f"{interval} 镜头 {shot['order']}：" + "；".join(details))
        sounds = []
        if shot["sound"].strip():
            sounds.append("音效：" + shot["sound"].strip())
        if shot["narration"].strip():
            sounds.append("旁白：" + shot["narration"].strip())
        for line in shot["dialogue"]:
            speaker = labels.get(line["character_id"], entities[line["character_id"]]["name"])
            kind = "画外对白" if line["voice_over"] else "对白"
            sounds.append(f"{kind}／{speaker}：{line['text']}")
        audio.append(interval + " " + ("；".join(sounds) if sounds else "无指定音频"))

    overview = ["地点：" + "、".join(dict.fromkeys(scenes))]
    if people:
        overview.append("主要出镜人物：" + "、".join(dict.fromkeys(people)))
    overview.append("开场画面：" + board["shots"][0]["action"])
    if len(shots) > 1:
        overview.append("结束画面：" + board["shots"][-1]["action"])
    return "\n\n".join([
        "视觉风格：写实真人短剧，真实摄影质感，自然动作；保持人物身份、服装、场景布局和道具状态一致。",
        "场景概述：" + "；".join(overview),
        "分镜（按时间轴依次呈现，总时长 " + seconds(board["shots"][-1]["end_ms"])
        + " 秒）：\n" + "\n".join(shots),
        "镜头要求：按各镜标注的取景范围和运镜执行，按时间点切换镜头，保持动作及空间关系连续。",
        "音频：\n" + "\n".join(audio),
        "限制：不叠加文字、字幕、标志或水印；不使用动画或卡通渲染，避免过度 CG 感；不增加分镜之外的剧情、对白或音效。",
    ])
