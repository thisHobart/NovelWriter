# -*- coding: utf-8 -*-
"""拿一章真实正文，单独跑一次质量评审，看提示词改动有没有效。

改评审提示词只能靠真实模型验证，但从写作跑到评审要烧掉一整章的调用，一轮几分钟。
这里只跑评审那一次：正文、契约、案情圣经都从待复审记录和账本里取，跑一次就知道
这一版提示词能不能拿到合规的回复，以及卡在哪一条规则上。

用法：
    python tools/try_review_prompt.py 20 plausibility
    python tools/try_review_prompt.py 20 all
    python tools/try_review_prompt.py 20 plausibility --replay 某份存档.json

`--replay` 拿评审目录里存下来的原始回复重放，不发起任何调用，用来验证解析与校验
这一侧的改动。每次真实调用都会把回复原样存进 quality/prompt_trials/，跑坏了随时
可以回头看模型到底写了什么。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.review.domain_review_agent import DomainReviewAgent  # noqa: E402
from core.generation import pending_review  # noqa: E402
from core.generation.ai_helper import set_backend  # noqa: E402
from core.generation.domain_profiles import (  # noqa: E402
    apply_quality_loop_mode,
    resolve_domain_profile,
    resolve_quality_loop_mode,
)
from core.generation.story_ledger import StoryLedgerManager  # noqa: E402

STAGES = ("contract", "reader_blind", "plausibility")


def _parameters(output_dir: str) -> dict:
    """读项目参数（`system/parameters.txt`，一行一个 `键: 值`）。"""
    path = os.path.join(output_dir, "system", "parameters.txt")
    values = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if ":" in line:
                key, _, value = line.partition(":")
                values[key.strip()] = value.strip()
    return values


def _site(output_dir: str, chapter_number: int) -> dict:
    """这一章的正文与契约：优先用待复审记录，没有就用已定稿的章节文件。"""
    record = pending_review.load(output_dir, chapter_number)
    if record is not None and record.prose.strip():
        snapshot = record.snapshot or {}
        return {
            "prose": record.prose,
            "contract": snapshot.get("contract") or {},
            "source": "待复审记录",
        }
    path = os.path.join(
        output_dir, "story", "content", "chapters", f"chapter_{chapter_number}.md"
    )
    with open(path, "r", encoding="utf-8") as handle:
        return {"prose": handle.read(), "contract": {}, "source": path}


def _previous_tail(output_dir: str, chapter_number: int) -> str:
    path = os.path.join(
        output_dir, "story", "content", "chapters", f"chapter_{chapter_number - 1}.md"
    )
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()[-2500:]


def _save(output_dir: str, chapter_number: int, stage: str, responses: list) -> str:
    directory = os.path.join(output_dir, "quality", "prompt_trials")
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, f"chapter_{chapter_number}_{stage}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"stage": stage, "responses": responses}, handle,
                  ensure_ascii=False, indent=2)
    return path


def _run(agent, stage: str, site: dict, case_bible: dict, suspense: dict,
         previous_tail: str):
    if stage == "reader_blind":
        return agent.review_reader_blind(site["prose"], previous_tail)
    if stage == "plausibility":
        return agent.review_plausibility(site["prose"], case_bible)
    return agent.review_chapter(site["prose"], site["contract"], case_bible, suspense)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("chapter", type=int)
    parser.add_argument("stage", choices=(*STAGES, "all"))
    parser.add_argument("--output-dir", default="current_work")
    parser.add_argument("--replay", default="",
                        help="重放存档里的原始回复，不调用大模型")
    args = parser.parse_args()

    output_dir = args.output_dir
    parameters = _parameters(output_dir)
    site = _site(output_dir, args.chapter)
    ledger = StoryLedgerManager(output_dir)
    case_bible = ledger.load_case_bible()
    suspense = ledger.load_suspense_ledger()
    previous_tail = _previous_tail(output_dir, args.chapter)

    profile = apply_quality_loop_mode(
        resolve_domain_profile(parameters), resolve_quality_loop_mode(parameters)
    )
    locked = ledger.locked_profile()
    if locked:
        profile = apply_quality_loop_mode(
            locked, resolve_quality_loop_mode(parameters)
        )

    print(f"项目 {output_dir} · 第 {args.chapter} 章 · 正文来自{site['source']}")
    print(f"档案 {profile.label}（{profile.key}）· 门槛 {profile.pass_average} · "
          f"正文 {len(site['prose'])} 字")

    responses: list = []
    if args.replay:
        with open(args.replay, "r", encoding="utf-8") as handle:
            saved = json.load(handle).get("responses", [])
        replies = iter(saved)
        send = lambda *a, **k: next(replies)  # noqa: E731
        print(f"重放 {args.replay}（{len(saved)} 次回复），不调用大模型\n")
    else:
        set_backend(parameters.get("Backend", "api"), parameters.get("Model"))
        from core.generation.ai_helper import send_prompt

        def send(prompt, model=None):
            reply = send_prompt(prompt, model=model)
            responses.append(reply)
            return reply

        print(f"后端 {parameters.get('Backend')} · 模型 {parameters.get('Model')}\n")

    stages = STAGES if args.stage == "all" else (args.stage,)
    failures = 0
    for stage in stages:
        agent = DomainReviewAgent(model=parameters.get("Model", ""),
                                  profile=profile, send_prompt_fn=send)
        started = len(responses)
        print(f"—— {stage} " + "-" * 40)
        try:
            review = _run(agent, stage, site, case_bible, suspense, previous_tail)
        except Exception as error:  # noqa: BLE001 - 这里就是要看清楚失败长什么样
            failures += 1
            print(f"失败：{error}")
        else:
            print(f"判定：{'通过' if review.passed else '未通过'} · "
                  f"平均 {review.average_score:.2f} / 门槛 {review.pass_average:.2f}")
            print(f"分数：{review.scores}")
            print(f"硬伤 {len(review.hard_failures)} 处 · 改法 {len(review.upgrades)} 条 · "
                  f"位置 {review.repair_scope or '未写'}")
            if review.reviewer_warning:
                print(f"警告：{review.reviewer_warning}")
            for item in review.asks[:4]:
                print(f"  · {item[:100]}")
        finally:
            print(f"调用 {len(responses) - started} 次")
            if not args.replay and len(responses) > started:
                print(f"回复存档：{_save(output_dir, args.chapter, stage, responses[started:])}")
        print()

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
