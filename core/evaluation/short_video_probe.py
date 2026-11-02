"""Explicitly invoked paired short-video quality evaluation; offline by default.

prepare writes only requirements; outline/compare require --run-live. Generated
outlines must be edited/confirmed in the application before compare will run.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import random

from core.short_video.llm import RequestBudget, ScriptLLM
from core.short_video.models import ScriptRequirements, encode, parse_object
from core.short_video.pipeline import ScriptPipeline
from core.short_video.prompts import script_prompt
from core.short_video.repository import ScriptRepository
from core.short_video.validation import validate_story, to_markdown


CREATIVES = (
    ("对白", "退还错拿的包裹", "两位邻居都声称包裹属于自己；他们通过一个生活细节找到真正收件人。"),
    ("对白", "父女争论旧录音", "女儿要删掉旧录音腾空间，父亲反对；争论让一段被误解的话终于被听完。"),
    ("动作", "雨夜修灯", "便利店门前灯坏了，店员想关门；一位路人的动作让店员重新拿起工具。"),
    ("动作", "最后一班公交", "一个乘客追赶末班车，却为捡起陌生人的药停下；司机必须作出选择。"),
    ("信息揭示", "被擦掉的留言", "清洁工擦掉玻璃上的旧留言时，发现旁边有人每天都在补上同一个字。"),
    ("信息揭示", "两份不同日期的照片", "一家人整理照片时发现相同场景标着两个日期，背后是一次不愿说出口的补偿。"),
)


def prepare(output):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    cases = []
    for index, (category, title, idea) in enumerate(CREATIVES, 1):
        for seconds in (30, 90):
            name = f"case_{index:02d}_{seconds}s"
            requirements = ScriptRequirements(title=title, idea=idea, duration_seconds=seconds).to_dict()
            ScriptRepository(root / name).save_requirements(requirements)
            cases.append({"id": name, "category": category, "duration_seconds": seconds})
    ScriptRepository.atomic(root / "manifest.json", encode({"mode": "prepared_only", "cases": cases}))
    return cases


def load_cases(root):
    return parse_object((Path(root) / "manifest.json").read_text(encoding="utf-8"))["cases"]


def generate_outlines(root, model):
    cases = load_cases(root)
    # Refuse bulk regeneration before making the first paid request.
    if any(ScriptRepository(Path(root) / case["id"]).load()["outline"] for case in cases):
        raise ValueError("已有大纲，拒绝覆盖；请使用新的评估目录")
    for case in cases:
        ScriptPipeline(Path(root) / case["id"], model=model).run("outline")


def compare(root, model):
    root = Path(root)
    cases = load_cases(root)
    projects = [ScriptRepository(root / case["id"]) for case in cases]
    if not all(repo.outline_ready(repo.load()) for repo in projects):
        raise ValueError("请先在界面逐一打开 12 个项目，检查并确认大纲，再执行 compare")
    output = root / "comparison"
    output.mkdir(exist_ok=False)  # No accidental rerun or overwrite of paid evaluation artifacts.
    order = [True] * 6 + [False] * 6
    random.Random(20261030).shuffle(order)
    mapping = []
    for index, (case, repo) in enumerate(zip(cases, projects)):
        state = repo.load()
        baseline_folder = output / case["id"]
        baseline_folder.mkdir()
        records = []
        def record(prompt, response, error):
            records.append({"prompt": prompt, "response": response, "error": error})
            ScriptRepository.atomic(baseline_folder / "baseline_calls.private.json", encode(records))
        llm = ScriptLLM(model, RequestBudget(max_calls=1), record=record)
        raw = llm(script_prompt(state["requirements"], state["outline"]["content"]))
        direct = validate_story(parse_object(raw), state["requirements"], "script")
        result = ScriptPipeline(repo.root.parent, model=model).run("script")
        version = repo.version(result["version_id"])
        if result["status"] != "reviewed":
            raise ValueError(f"{case['id']} 新流程尚未完成评审，停止后续调用，当前成果已保留")
        candidate_first = order[index]
        for name, script in (("A", version["content"] if candidate_first else direct),
                             ("B", direct if candidate_first else version["content"])):
            ScriptRepository.atomic(baseline_folder / f"{name}.md", to_markdown(script))
        mapping.append({"id": case["id"], "A": "candidate" if candidate_first else "baseline",
                        "model": model, "candidate_run": result["run_id"], "candidate_calls": result["calls"],
                        "baseline_calls": 1, "status": result["status"]})
        ScriptRepository.atomic(output / "manifest.private.json", encode(mapping))
    ScriptRepository.atomic(output / "ratings.csv",
        "case_id,preferred,continuity_A,continuity_B,details_A,details_B,dialogue_A,dialogue_B,rhythm_A,rhythm_B,visual_A,visual_B,notes\n"
        + "\n".join(case["id"] + ",,,,,,,,,,,," for case in cases) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "outline", "compare"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", choices=("hosted-llm", "deepseek", "qwen"), default="hosted-llm")
    parser.add_argument("--run-live", action="store_true", help="明确允许真实模型请求与潜在费用")
    args = parser.parse_args(argv)
    if args.action != "prepare" and not args.run_live:
        parser.error("outline/compare 会调用真实模型，必须显式传入 --run-live")
    if args.action == "prepare":
        prepare(args.output)
        print("已创建 12 份需求，未调用模型。")
    elif args.action == "outline":
        generate_outlines(args.output, args.model)
        print("大纲已生成；请先逐一检查并确认，再运行 compare。")
    else:
        compare(args.output, args.model)
        print("配对材料已生成，私有映射不要提供给评审者；尚无人工质量结论。")


if __name__ == "__main__":
    main()
