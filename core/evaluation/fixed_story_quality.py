"""Reproducible three-chapter quality run for NovelWriter.

This is deliberately a scripted offline model run.  It exercises the real
prompt builders, review normalization, bounded revision loop, chapter assembly,
acceptance gates, ledger commit path and A/B exporter without claiming that the
scripted responses came from a live provider.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict

from agents.review.domain_review_agent import DomainReviewAgent
from core.evaluation.reader_blind_test import create_blind_test
from core.generation.chapter_generation_loop import ChapterGenerationLoop
from core.generation.domain_profiles import get_domain_profile
from core.generation.llm_trace import trace_model_call, trace_session, trace_stage
from core.generation.narrative_quality import analyze_narrative_quality, summarize_narrative_reports
from core.generation.prompt_context import analyze_chinese_prose_style
from core.generation.scene_prompt import build_scene_prompt
from core.generation.stage_pipeline import load_stage_parameters
from core.generation.story_ledger import StoryLedgerManager


SEED = 20260829
RUN_ID = "fixed_mystery_3ch_v1"
MODEL = "fixture-zh-mystery-v1"
MODE = "scripted_offline"

REQUIRED_STAGES = (
    "parameter_parsing",
    "world_and_domain_rules",
    "entities_relationships_factions_locations",
    "story_structure_and_outlines",
    "scene_planning_and_contract",
    "scene_prose_generation",
    "chapter_assembly",
    "ledger_facts_knowledge_timeline",
    "contract_compliance_review",
    "reader_blind_review",
    "plausibility_review",
    "targeted_revision_and_retry",
    "acceptance_save_and_handoff",
    "human_ab_export_and_summary_entry",
)

PARAMETERS = {
    "Output Directory": "<isolated-quality-run>",
    "Genre": "Mystery",
    "Subgenre": "Legal Thriller",
    "Story Length": "Novella",
    "Story Structure": "3-Act Structure",
    "Novel Title": "雨夜门禁",
    "Author Name": "NovelWriter 质量夹具",
    "Theme": "程序留下的空白不能被想当然填满",
    "Tone": "克制、冷峻、当代中文悬疑",
    "Quality Loop": "standard",
    "Backend": "scripted",
    "Model": MODEL,
}

LORE = """故事发生在当代中国虚构城市临川。市档案数字化中心采用普通刷卡门禁：
进入刷卡，离开按内侧开门按钮，控制器不会为离开动作生成持卡人记录。门禁日志只能证明
某张卡在某时被读卡器接受，不能单独证明持卡人身份。便携硬盘一旦被砸毁，不得再从它
得出核心结论；只有销毁前已经导出、封存并校验摘要的独立审计副本可用于后续核对。
本故事没有能替代现实证据规则的超自然或未来技术。"""

CHARACTERS = [
    {"name": "程砚", "role": "刑警", "knowledge": "熟悉现场保护，不预设电子记录能证明身份"},
    {"name": "苏澄", "role": "档案系统管理员", "knowledge": "了解门禁记录边界和审计副本"},
    {"name": "方屿", "role": "数字化中心副主任", "knowledge": "知道维护窗口，不知道审计副本已被提前封存"},
    {"name": "陈岚", "role": "遇害的档案员", "knowledge": "发现拆迁卷宗被违规替换"},
]
FACTIONS = [
    {"name": "临川市刑侦支队", "goal": "依法固定可复核证据"},
    {"name": "市档案数字化中心", "goal": "完成扫描项目并保护原件"},
]
LOCATIONS = [
    {"id": "L001", "name": "数字化中心二楼扫描室", "constraint": "门会自动回锁，出门不刷卡"},
    {"id": "L002", "name": "刑侦支队电子证据室", "constraint": "只检查封存副本，不操作损坏原件"},
]

PLANS = {
    1: """### 场景 1：雨夜现场
程砚到扫描室，确认陈岚死亡、损坏硬盘和门禁卡的位置，只做现场记录与封存。

### 场景 2：一条不能证明人的记录
苏澄导出控制器原始日志。21:17 的刷卡记录使用陈岚的卡，但程砚明确它只能证明卡，不能证明人。""",
    2: """### 场景 1：坏掉的原件
技术人员处理损坏硬盘；初稿会错误地从被砸毁的唯一硬盘恢复核心录像，评审必须定向修复为销毁前已封存的独立审计副本。

### 场景 2：门内没有第二次刷卡
苏澄解释门禁离开不记名。方屿试图把 21:17 记录说成陈岚仍活着，却暴露自己知道维护窗口。""",
    3: """### 场景 1：记录的边界
程砚把门禁日志、维护排班和独立审计副本放在一起，只作相互印证，不让任何单项记录越权证明身份。

### 场景 2：雨停之前
方屿承认用陈岚的卡制造较晚活动时间。程砚关闭线索，保留需要后续司法审查的证据边界。""",
}

INITIAL_SCENES = {
    (1, 1): """雨水顺着扫描室的窄窗往下淌。程砚站在警戒线外，先让摄影员拍门锁和地面，再走近陈岚。她倒在扫描台旁，外套口袋露出半截门禁卡；一只便携硬盘裂在墙角。

“先别通电。”程砚拦住伸手的值班员，“原样封存，谁碰过，逐个登记。”

自动闭门器把门压回锁舌，声音很轻。所谓密室，只是所有人还没弄清这扇门怎样记录进出。""",
    (1, 2): """苏澄在众人面前导出控制器原始日志，写保护后交给勘查员。屏幕上最后一条是21:17，卡号属于陈岚。

“这能证明她当时回来过？”值班员问。

程砚摇头：“只能证明这张卡被读过。谁拿着卡，日志不回答。”

陈岚的卡此刻在证物袋里。隔着透明袋，磨损的第七码像一道被人故意留下的划痕。""",
    (2, 1): """技术员戴上手套，把便携硬盘从证物袋里取出。方屿抢过硬盘砸在地上，盘片已经碎裂；技术员接上两根导线，几秒钟后便从碎片里恢复了完整录像，画面清楚拍到方屿进入扫描室。""",
    (2, 2): """苏澄把门禁说明书翻到接线图：“进门刷卡，出门按内侧按钮。离开不会留下持卡人记录。”

方屿盯着21:17那一行：“那至少说明陈岚那时还活着。”

“我们没说摄像维护发生在哪一分钟。”程砚合上笔录，“你怎么知道这条记录正好落在维护窗口？”

方屿没有回答，只把一次性纸杯捏出一道折痕。""",
    (3, 1): """桌上分开放着三份材料。第一份是门禁原始日志，第二份是当晚维护排班。第三份是独立审计副本，已在21:00前导出并封存。

“刷卡记录不证明是谁。”程砚说，“维护排班也不证明谁进了门。”

他指向副本：“这里只记着陈岚账户在20:54导出过目录。排班只能说明方屿能预见摄像中断。我们还要核对人证和原件。”

每份材料都停在自己的证明边界上。""",
    (3, 2): """窗外的雨小了。方屿看着封存单，上面写着20:54。

“她什么时候把副本交给你们的？”他问。

“你不知道副本存在。”程砚说。

方屿沉默片刻：“21:17，我拿她的卡进了门。卡后来放回她口袋。我只想把时间推到摄像恢复以后。”

他仍否认动手。程砚没有替审判下结论，只在笔录末尾写明：门禁卡线索关闭，死亡责任仍待审查。""",
}

REPAIRED_SCENE = """技术员没有碰硬盘碎片，只隔着证物袋记录破损状态。苏澄调出21:00前已经导出、封存并登记校验摘要的独立审计副本。

“坏盘不能替我们作证。”她说，“这份副本只能说明导出前系统记录过什么，不能单独证明是谁操作。”

副本里没有完整录像，只有陈岚账户在20:54导出被替换卷宗目录的记录。程砚让人核对封存人、导出设备和交接时间，没有越过记录本身能证明的范围。"""


def _contract(chapter: int) -> Dict[str, Any]:
    common: Dict[str, Any] = {
        "chapter": chapter,
        "chapter_function": ("advance", "reveal", "aftermath")[chapter - 1],
        "core_question": ("21:17 的卡是谁刷的？", "损坏硬盘之外还有什么可核对？", "方屿怎样制造了较晚活动时间？")[chapter - 1],
        "concrete_anchor": ("磨损的第七码", "独立审计副本封存单", "20:54 的封存单")[chapter - 1],
        "reader_knows_before": [],
        "reader_knows_after": [],
        "reader_must_not_know_yet": [],
        "character_knowledge_after": {},
        "apparent_answer": "",
        "reversal": "",
        "attack_move": "",
        "defense_move": "",
        "personal_cost": "",
        "cost_character": "",
        "irreversible_change": "",
        "ending_effect": "推进调查",
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
        "fair_play_clues": [],
        "evidence_updates": [],
        "legal_checkpoints": [],
        "scene_boundaries": [
            {"scene_number": 1, "must_do": [], "must_not_do": ["提前完成下一场"], "end_state": "本场任务完成"},
            {"scene_number": 2, "must_do": [], "must_not_do": [], "end_state": "留下下一章问题"},
        ],
    }
    if chapter == 1:
        common.update(
            reader_knows_after=["21:17 发生过一次陈岚门禁卡刷卡", "刷卡记录不能单独证明持卡人"],
            character_knowledge_after={"程砚": ["21:17 记录只证明卡被读取"]},
            facts_added=[{"id": "F001", "fact": "21:17 门禁事件", "value": "陈岚卡被读取", "first_stated_at": "scene_2"}],
            timeline_events=[{"id": "TL001", "event": "陈岚门禁卡被读取", "time": "21:17", "location_id": "L001"}],
            plot_thread_updates=[{"id": "PT001", "thread": "谁使用陈岚门禁卡", "status": "open", "deadline_chapter": 3}],
            fair_play_clues=[{"id": "C001", "surface_meaning": "陈岚在21:17回来", "true_meaning": "只能证明她的卡被读取", "introduced_at": "scene_2", "payoff_at": "chapter_3"}],
            evidence_updates=[{"id": "E001", "item": "门禁控制器原始日志", "origin": "L001控制器", "type": "电子记录", "status": "导出并封存", "custodian": "勘查员", "chain_risk": "只能证明卡号"}],
        )
    elif chapter == 2:
        common.update(
            reader_knows_before=["21:17 记录只证明卡被读取"],
            reader_knows_after=["离开动作不生成持卡人记录", "独立审计副本在硬盘损坏前已封存"],
            character_knowledge_after={"程砚": ["方屿预先知道摄像维护窗口"]},
            facts_added=[{"id": "F002", "fact": "门禁离开记录方式", "value": "内侧按钮不记持卡人", "first_stated_at": "scene_2"}],
            timeline_events=[{"id": "TL002", "event": "独立审计副本完成封存", "time": "21:00", "location_id": "L002"}],
            fair_play_clues=[{"id": "C002", "surface_meaning": "方屿解释21:17记录", "true_meaning": "方屿知道未公开的维护窗口", "introduced_at": "scene_2", "payoff_at": "chapter_3"}],
            evidence_updates=[{"id": "E002", "item": "独立审计副本", "origin": "21:00前导出", "type": "封存副本", "status": "校验", "custodian": "电子证据室", "chain_risk": "不能单独证明操作者"}],
        )
    else:
        common.update(
            reader_knows_before=["方屿知道维护窗口", "独立审计副本记录20:54导出"],
            reader_knows_after=["方屿在21:17使用陈岚的卡并放回她口袋"],
            character_knowledge_after={"程砚": ["方屿承认制造较晚活动时间"]},
            facts_added=[{"id": "F003", "fact": "21:17 门禁卡使用者", "value": "方屿", "first_stated_at": "scene_2"}],
            timeline_events=[{"id": "TL003", "event": "方屿使用陈岚门禁卡", "time": "21:17", "location_id": "L001"}],
            plot_thread_updates=[{"id": "PT001", "thread": "谁使用陈岚门禁卡", "status": "closed", "deadline_chapter": 3}],
            evidence_updates=[{"id": "E003", "item": "方屿询问副本交付时间的陈述", "origin": "讯问笔录", "type": "供述线索", "status": "记录", "custodian": "程砚", "chain_risk": "仍需其他证据印证"}],
        )
    return common


CASE_BIBLE = {
    "central_question": "谁利用门禁记录制造了陈岚较晚仍活动的假象？",
    "central_conflict": {"legal_answer": "门禁卡记录", "truth_answer": "方屿持卡重返现场", "moral_question": "记录边界与破案压力"},
    "truth": [
        {"id": "T001", "fact": "方屿在21:17使用陈岚的卡", "source": "第三章大纲", "must_not_reveal_before": "chapter_3"},
        {"id": "T002", "fact": "损坏硬盘不再承担核心证明", "source": "世界观规则", "must_not_reveal_before": "chapter_2"},
        {"id": "T003", "fact": "门禁内侧离开按钮不生成持卡人记录", "source": "第二章大纲", "must_not_reveal_before": "chapter_2"},
    ],
    "chronology": [
        {"order": 1, "event": "20:54陈岚账户导出目录", "known_initially_by": ["陈岚"]},
        {"order": 2, "event": "21:00前审计副本封存", "known_initially_by": ["苏澄"]},
        {"order": 3, "event": "21:17方屿刷陈岚的卡", "known_initially_by": ["方屿"]},
    ],
    "domain_rules": {
        "model": "当代中国现实证据与普通门禁体系",
        "baseline_rules": ["电子日志只证明其实际记录的事件", "损坏原件不得虚构恢复结果"],
        "roles": ["刑警固定证据", "系统管理员解释系统边界"],
        "limits": ["无超自然规则", "无未来取证技术"],
    },
    "fair_play_obligations": [
        {"truth_id": "T001", "required_clue": "方屿提前知道维护窗口", "deadline": "chapter_2"}
    ],
}


def _scores(dimensions: tuple[str, ...], value: float = 4.0) -> Dict[str, Any]:
    return {
        "scores": {dimension: value for dimension in dimensions},
        "hard_failures": [],
        "evidence": [],
        "upgrades": [],
        "repair_scope": "",
        "repair_instructions": [],
        "strengths": ["判断范围克制，证据结论没有越界"],
    }


class ScriptedModel:
    def __init__(self, profile, contracts):
        self.profile = profile
        self.contracts = contracts

    @staticmethod
    def classify(prompt: str) -> str:
        if "稳定的“案件底稿”" in prompt:
            return "world_and_domain_rules"
        if "建立一份可执行的章节契约" in prompt:
            return "scene_planning_and_contract"
        if prompt.startswith("请对场景正文进行最小范围修订"):
            return "targeted_revision_and_retry"
        # 同一场的多轮重修摊平后带着轮次标签，开头是当初的写作依据。
        if "请在你上面这一稿的基础上改" in prompt:
            return "targeted_revision_and_retry"
        if "中文类型小说盲读审稿人" in prompt:
            return "reader_blind_review"
        if "现实合理性与专业机制审稿人" in prompt:
            return "plausibility_review"
        if "评审 " in prompt:
            return "contract_compliance_review"
        if prompt.startswith("请撰写"):
            return "scene_prose_generation"
        return "unclassified_model_call"

    def __call__(self, prompt: str, model: str | None = None) -> str:
        stage = self.classify(prompt)

        def invoke() -> str:
            if stage == "world_and_domain_rules":
                return json.dumps(CASE_BIBLE, ensure_ascii=False)
            if stage == "scene_planning_and_contract":
                match = re.search(r"第\s*(\d+)\s*章", prompt)
                return json.dumps(self.contracts[int(match.group(1))], ensure_ascii=False)
            if stage == "targeted_revision_and_retry":
                return REPAIRED_SCENE
            if stage == "scene_prose_generation":
                match = re.search(r"第\s*(\d+)\s*章，场景\s*(\d+)", prompt)
                return INITIAL_SCENES[(int(match.group(1)), int(match.group(2)))]
            if stage == "reader_blind_review":
                return json.dumps(_scores(DomainReviewAgent._BLIND_DIMENSIONS), ensure_ascii=False)
            if stage == "plausibility_review":
                return json.dumps(_scores(DomainReviewAgent._PLAUSIBILITY_DIMENSIONS), ensure_ascii=False)
            if stage == "contract_compliance_review":
                reviewed_content = prompt.split("待评审内容：", 1)[-1]
                reviewed_content = reviewed_content.split("上一轮评审要求", 1)[0]
                reviewed_content = reviewed_content.split("硬失败代码", 1)[0]
                if "评审 scene_1" in prompt and "恢复了完整录像" in reviewed_content:
                    failed = _scores(self.profile.score_dimensions)
                    failed["hard_failures"] = [
                        {
                            "code": "LEGAL_IMPOSSIBILITY",
                            "quote": "恢复了完整录像",
                            "problem": "唯一硬盘已被砸碎，核心录像恢复没有现实依据，会让推理建立在不存在的证据上",
                            "change": "删除恢复录像，改用损坏前已封存且可校验的独立审计副本",
                        }
                    ]
                    failed["repair_scope"] = "scene_1"
                    failed["repair_instructions"] = ["只替换证据来源，不增加新嫌疑人或新结论"]
                    return json.dumps(failed, ensure_ascii=False)
                return json.dumps(_scores(self.profile.score_dimensions), ensure_ascii=False)
            raise RuntimeError(f"脚本模型没有覆盖该 prompt：{prompt[:80]}")

        with trace_stage(stage):
            return trace_model_call(
                prompt=prompt,
                backend="scripted",
                model=model or MODEL,
                invoke=invoke,
            )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _stage_record(
    name: str,
    *,
    inputs: Any,
    output: Any,
    calls: list[Dict[str, Any]],
    elapsed_ms: float,
    deterministic_errors: list[str] | None = None,
    model_quality_issues: list[str] | None = None,
    propagates: bool = False,
) -> Dict[str, Any]:
    errors = list(deterministic_errors or [])
    return {
        "stage": name,
        "passed": not errors,
        "input": inputs,
        "actual_prompts": [call["prompt"] for call in calls],
        "outputs": [call["response"] for call in calls],
        "parsed_output": output,
        "elapsed_ms": round(elapsed_ms, 3),
        "call_count": len(calls),
        "usage": {
            "exact_available": False,
            "estimated_prompt_tokens": sum(call["usage"]["estimated_prompt_tokens"] for call in calls),
            "estimated_completion_tokens": sum(call["usage"]["estimated_completion_tokens"] for call in calls),
        },
        "cost": {"amount_usd": None, "reason": "scripted offline run; no provider billing"},
        "deterministic_errors": errors,
        "model_output_quality_issues": list(model_quality_issues or []),
        "propagates_to_later_stages": propagates,
    }


def run_fixed_story_quality(output_dir: str | Path) -> Dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    run_started = time.perf_counter()
    stage_elapsed: Dict[str, float] = {}
    random.seed(SEED)
    profile = get_domain_profile("legal_suspense")
    contracts = {chapter: _contract(chapter) for chapter in range(1, 4)}

    stage_started = time.perf_counter()
    parameter_lines = "\n".join(f"{key}: {value}" for key, value in PARAMETERS.items()) + "\n"
    _write_text(root / "system" / "parameters.txt", parameter_lines)
    parsed_parameters = load_stage_parameters(str(root))
    parsed_parameters["output_directory"] = str(root)
    stage_elapsed["parameter_parsing"] = (time.perf_counter() - stage_started) * 1000

    stage_started = time.perf_counter()
    _write_text(root / "story" / "lore" / "generated_lore.md", LORE)
    _write_json(root / "story" / "lore" / "lore_contract.json", {"domain_rules": CASE_BIBLE["domain_rules"]})
    stage_elapsed["world_and_domain_rules"] = (time.perf_counter() - stage_started) * 1000

    stage_started = time.perf_counter()
    _write_json(root / "story" / "lore" / "characters.json", CHARACTERS)
    _write_json(root / "story" / "lore" / "factions.json", FACTIONS)
    _write_json(root / "story" / "lore" / "locations.json", LOCATIONS)
    stage_elapsed["entities_relationships_factions_locations"] = (
        time.perf_counter() - stage_started
    ) * 1000

    stage_started = time.perf_counter()
    structure_contract = {
        "sections": [
            {
                "section": "三章短弧",
                "section_index": 1,
                "total_sections": 1,
                "central_question": CASE_BIBLE["central_question"],
                "central_conflict": CASE_BIBLE["central_conflict"],
                "truths_introduced": CASE_BIBLE["truth"],
                "chronology_events": CASE_BIBLE["chronology"],
                "threads_opened": [{"id": "PT001", "thread": "谁使用陈岚门禁卡", "deadline_chapter": 3}],
                "threads_closed": [{"id": "PT001", "chapter": 3}],
            }
        ]
    }
    _write_json(root / "story" / "structure" / "structure_contract.json", structure_contract)
    _write_text(root / "story" / "structure" / "act_1.md", "第一章提出记录边界；第二章排除坏盘捷径；第三章用相互印证收束。")
    _write_text(root / "story" / "planning" / "chapter_outlines" / "chapter_outlines_fixed.md", "\n\n".join(f"## 第 {chapter} 章\n{PLANS[chapter]}" for chapter in range(1, 4)))
    stage_elapsed["story_structure_and_outlines"] = (
        time.perf_counter() - stage_started
    ) * 1000

    stage_started = time.perf_counter()
    for chapter, plan in PLANS.items():
        _write_text(root / "story" / "planning" / "detailed_scene_plans" / f"scenes_fixed_ch{chapter}.md", plan)
    stage_elapsed["scene_planning_and_contract"] = (
        time.perf_counter() - stage_started
    ) * 1000

    manager = StoryLedgerManager(str(root))
    manager.initialize(parsed_parameters)
    model = ScriptedModel(profile, contracts)
    reviewer = DomainReviewAgent(model=MODEL, profile=profile, send_prompt_fn=model)

    results = {}
    acceptances = {}
    retry_count = 0
    assembly_elapsed = 0.0
    acceptance_elapsed = 0.0
    with trace_session(root, run_id=RUN_ID, mode=MODE) as recorder:
        stage_started = time.perf_counter()
        design_context = manager.load_design_context()
        bible = reviewer.build_case_bible(parsed_parameters, LORE, design_context, manager.load_case_bible())
        bible = manager.merge_declared_story_spine(bible)
        manager.save_case_bible(bible, design_context)
        stage_elapsed["world_and_domain_rules"] += (
            time.perf_counter() - stage_started
        ) * 1000

        stage_started = time.perf_counter()
        for chapter in range(1, 4):
            contract = reviewer.build_chapter_contract(
                chapter,
                PLANS[chapter],
                parsed_parameters,
                LORE,
                manager.load_case_bible(),
                manager.load_suspense_ledger(),
            )
            manager.save_contract(chapter, contract, PLANS[chapter])
        stage_elapsed["scene_planning_and_contract"] += (
            time.perf_counter() - stage_started
        ) * 1000

        baseline_scenes: Dict[int, list[str]] = {}
        for chapter in range(1, 4):
            baseline_scenes[chapter] = [INITIAL_SCENES[(chapter, 1)], INITIAL_SCENES[(chapter, 2)]]
            loop = ChapterGenerationLoop(
                output_dir=str(root),
                model=MODEL,
                reviewer=reviewer,
                profile=profile,
                require_planning_contract=False,
            )

            def generate_scene(*, _chapter=chapter, **kwargs):
                prompt = build_scene_prompt(
                    scene_plan=kwargs["scene_plan"],
                    scene_number=kwargs["scene_number"],
                    parameters=parsed_parameters,
                    lore=LORE,
                    character_roster=json.dumps(CHARACTERS, ensure_ascii=False),
                    faction_summary=json.dumps(FACTIONS, ensure_ascii=False),
                    profile=kwargs["profile"],
                    chapter_number=_chapter,
                    structure_name="3-Act Structure",
                    novel_title="雨夜门禁",
                    contract=kwargs["contract"],
                    previous_scene_tail=kwargs["previous_scene_tail"],
                    next_scene_plan=kwargs["next_scene_plan"],
                )
                return model(prompt, model=MODEL)

            result = loop.run(
                chapter_number=chapter,
                plan_content=PLANS[chapter],
                parameters=parsed_parameters,
                lore=LORE,
                generate_scene=generate_scene,
            )
            retry_count += result.retry_count
            chapter_path = root / "story" / "content" / "chapters" / f"chapter_{chapter}.md"
            operation_started = time.perf_counter()
            _write_text(chapter_path, result.chapter_content)
            assembly_elapsed += (time.perf_counter() - operation_started) * 1000
            operation_started = time.perf_counter()
            acceptance = loop.accept_result(chapter, result, str(chapter_path))
            acceptance_elapsed += (time.perf_counter() - operation_started) * 1000
            results[chapter] = result
            acceptances[chapter] = acceptance
            _write_text(root / "baseline" / f"chapter_{chapter}.md", "\n\n---\n\n".join(baseline_scenes[chapter]))

        operation_started = time.perf_counter()
        create_blind_test(
            root / "baseline",
            root / "story" / "content" / "chapters",
            root / "ab_blind_test",
            participants=30,
            seed=SEED,
        )
        stage_elapsed["human_ab_export_and_summary_entry"] = (
            time.perf_counter() - operation_started
        ) * 1000

    stage_elapsed["chapter_assembly"] = assembly_elapsed
    stage_elapsed["acceptance_save_and_handoff"] = acceptance_elapsed
    calls_by_stage = {
        stage: [call for call in recorder.calls if call["stage"] == stage]
        for stage in REQUIRED_STAGES
    }
    chapters = [results[index].chapter_content for index in range(1, 4)]
    operation_started = time.perf_counter()
    ledger = manager.load_suspense_ledger()
    stage_elapsed["ledger_facts_knowledge_timeline"] = (
        time.perf_counter() - operation_started
    ) * 1000
    for stage, calls in calls_by_stage.items():
        if calls and stage not in stage_elapsed:
            stage_elapsed[stage] = sum(call["elapsed_ms"] for call in calls)
    stage_elapsed.setdefault("scene_prose_generation", 0.0)
    stage_elapsed.setdefault("contract_compliance_review", 0.0)
    stage_elapsed.setdefault("reader_blind_review", 0.0)
    stage_elapsed.setdefault("plausibility_review", 0.0)
    stage_elapsed.setdefault("targeted_revision_and_retry", 0.0)
    stages: Dict[str, Any] = {}
    stages["parameter_parsing"] = _stage_record(
        "parameter_parsing", inputs=PARAMETERS, output=parsed_parameters, calls=[], elapsed_ms=stage_elapsed["parameter_parsing"]
    )
    stages["world_and_domain_rules"] = _stage_record(
        "world_and_domain_rules", inputs={"lore": LORE}, output=manager.load_case_bible(), calls=calls_by_stage["world_and_domain_rules"], elapsed_ms=stage_elapsed["world_and_domain_rules"]
    )
    stages["entities_relationships_factions_locations"] = _stage_record(
        "entities_relationships_factions_locations", inputs={"seed": SEED}, output={"characters": CHARACTERS, "factions": FACTIONS, "locations": LOCATIONS}, calls=[], elapsed_ms=stage_elapsed["entities_relationships_factions_locations"]
    )
    stages["story_structure_and_outlines"] = _stage_record(
        "story_structure_and_outlines", inputs=CASE_BIBLE["central_question"], output=structure_contract, calls=[], elapsed_ms=stage_elapsed["story_structure_and_outlines"]
    )
    stages["scene_planning_and_contract"] = _stage_record(
        "scene_planning_and_contract", inputs=PLANS, output=contracts, calls=calls_by_stage["scene_planning_and_contract"], elapsed_ms=stage_elapsed["scene_planning_and_contract"]
    )
    stages["scene_prose_generation"] = _stage_record(
        "scene_prose_generation", inputs={"chapters": 3, "scenes_per_chapter": 2}, output={str(k): v.scenes for k, v in results.items()}, calls=calls_by_stage["scene_prose_generation"], elapsed_ms=stage_elapsed["scene_prose_generation"]
    )
    stages["chapter_assembly"] = _stage_record(
        "chapter_assembly", inputs={"separator": "---"}, output={str(i): len(text) for i, text in enumerate(chapters, 1)}, calls=[], elapsed_ms=stage_elapsed["chapter_assembly"]
    )
    stages["ledger_facts_knowledge_timeline"] = _stage_record(
        "ledger_facts_knowledge_timeline", inputs={"contracts": 3}, output={"revision": ledger["revision"], "facts": ledger.get("facts", []), "knowledge": ledger.get("knowledge", []), "timeline": ledger.get("timeline", [])}, calls=[], elapsed_ms=stage_elapsed["ledger_facts_knowledge_timeline"], deterministic_errors=[item.get("message", "conflict") for item in ledger.get("unresolved_conflicts", [])]
    )
    stages["contract_compliance_review"] = _stage_record(
        "contract_compliance_review", inputs={"contracts": 3, "chapters": 3}, output={str(k): v.chapter_review.component_reviews.get("contract", {}) for k, v in results.items()}, calls=calls_by_stage["contract_compliance_review"], elapsed_ms=stage_elapsed["contract_compliance_review"], model_quality_issues=["第二章初稿把被砸毁的唯一硬盘当作可恢复核心证据；已路由到最小场景修订"], propagates=False
    )
    stages["reader_blind_review"] = _stage_record(
        "reader_blind_review", inputs={"visible": "previous_chapter_tail + current_prose"}, output={str(k): v.chapter_review.component_reviews.get("reader_blind", {}) for k, v in results.items()}, calls=calls_by_stage["reader_blind_review"], elapsed_ms=stage_elapsed["reader_blind_review"]
    )
    stages["plausibility_review"] = _stage_record(
        "plausibility_review", inputs={"visible": "declared_domain_rules + current_prose"}, output={str(k): v.chapter_review.component_reviews.get("plausibility", {}) for k, v in results.items()}, calls=calls_by_stage["plausibility_review"], elapsed_ms=stage_elapsed["plausibility_review"]
    )
    stages["targeted_revision_and_retry"] = _stage_record(
        "targeted_revision_and_retry", inputs={"max_scene_retries": profile.max_scene_retries}, output={"retry_count": retry_count, "chapter_2_scene_1": results[2].scenes[0]}, calls=calls_by_stage["targeted_revision_and_retry"], elapsed_ms=stage_elapsed["targeted_revision_and_retry"]
    )
    stages["acceptance_save_and_handoff"] = _stage_record(
        "acceptance_save_and_handoff", inputs={"chapters": [1, 2, 3]}, output={str(k): {"revision": v.committed_revision, "artifact_passed": v.artifact_report.passed, "consistency_passed": v.consistency_report.passed} for k, v in acceptances.items()}, calls=[], elapsed_ms=stage_elapsed["acceptance_save_and_handoff"]
    )
    stages["human_ab_export_and_summary_entry"] = _stage_record(
        "human_ab_export_and_summary_entry", inputs={"participants": 30, "seed": SEED}, output={"packets": 30, "response_rows": 90, "summary_command": f"python -m core.evaluation.reader_blind_test summarize --test-dir {root / 'ab_blind_test'}"}, calls=[], elapsed_ms=stage_elapsed["human_ab_export_and_summary_entry"]
    )

    report = {
        "run_id": RUN_ID,
        "mode": MODE,
        "seed": SEED,
        "model": MODEL,
        "accepted_chapters": [item["chapter"] for item in ledger["accepted_chapters"]],
        "ledger_revision": ledger["revision"],
        "unresolved_conflicts": ledger.get("unresolved_conflicts", []),
        "retry_count": retry_count,
        "total_elapsed_ms": round((time.perf_counter() - run_started) * 1000, 3),
        "narrative_corpus": summarize_narrative_reports(chapters),
        "chapter_diagnostics": {
            str(index): {
                "style": analyze_chinese_prose_style(text),
                "narrative": analyze_narrative_quality(text).to_dict(),
            }
            for index, text in enumerate(chapters, 1)
        },
        "stages": stages,
    }
    run_dir = root / "system" / "quality_runs" / RUN_ID
    _write_json(run_dir / "stage_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定三章中文悬疑离线质量流程")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    report = run_fixed_story_quality(args.output_dir)
    print(json.dumps({
        "run_id": report["run_id"],
        "mode": report["mode"],
        "accepted_chapters": report["accepted_chapters"],
        "ledger_revision": report["ledger_revision"],
        "retry_count": report["retry_count"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
