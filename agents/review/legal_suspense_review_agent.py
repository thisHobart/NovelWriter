"""Domain review for fair-play legal suspense generation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.generation.ai_helper import send_prompt
from core.generation.prompt_context import analyze_chinese_prose_style
from core.generation.story_ledger import compact_json


SCORE_DIMENSIONS = (
    "focus_depth",
    "concrete_detail",
    "information_gap",
    "fair_play",
    "reversal",
    "legal_realism",
    "moral_gray",
    "attack_defense",
    "personal_cost",
    "continuity",
    "chinese_prose",
)

HARD_FAILURE_CODES = {
    "TRUTH_CONTRADICTION",
    "UNSEEDED_SOLUTION",
    "LEGAL_IMPOSSIBILITY",
    "CONTINUITY_DUPLICATION",
    "KNOWLEDGE_LEAK",
    "NEXT_SCENE_PREMATURE",
    "TYPE_CONFLICT",
}


class DomainReviewError(RuntimeError):
    """Raised when the quality gate itself cannot produce a valid verdict."""


@dataclass
class DomainReview:
    stage: str
    passed: bool
    scores: Dict[str, float] = field(default_factory=dict)
    hard_failures: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    repair_scope: str = ""
    repair_instructions: List[str] = field(default_factory=list)
    strengths: List[str] = field(default_factory=list)
    reviewer_warning: str = ""

    @property
    def average_score(self) -> float:
        values = [float(value) for value in self.scores.values() if isinstance(value, (int, float))]
        return sum(values) / len(values) if values else 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["average_score"] = round(self.average_score, 3)
        return data


def extract_json_object(text: str) -> Dict[str, Any]:
    """Extract the first valid JSON object from a model response."""
    if not text:
        raise ValueError("大模型未返回评审 JSON")
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("无法从大模型响应中解析 JSON 对象")


class LegalSuspenseReviewAgent:
    """Build chapter contracts and review plans/prose with evidence-based output."""

    def __init__(
        self,
        model: str,
        logger: Optional[logging.Logger] = None,
        send_prompt_fn: Callable[..., str] = send_prompt,
    ):
        self.model = model
        self.logger = logger or logging.getLogger("legal-suspense-review")
        self.send_prompt = send_prompt_fn

    def _call_json(self, prompt: str) -> Dict[str, Any]:
        response = self.send_prompt(prompt, model=self.model)
        try:
            return extract_json_object(response)
        except ValueError:
            repair_prompt = f"""你上一次没有返回可解析的 JSON。请重新执行原任务，只输出一个合法 JSON 对象，不要解释，不要使用代码围栏。

原任务：
{prompt}
"""
            repaired_response = self.send_prompt(repair_prompt, model=self.model)
            return extract_json_object(repaired_response)

    def build_case_bible(
        self,
        parameters: Dict[str, Any],
        lore: str,
        design_context: str,
        baseline: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Establish whole-story truth once from structure and chapter outlines."""
        prompt = f"""你是法律悬疑小说的案件架构编辑。请把已经存在的全书结构和章节大纲整理成稳定的“案件底稿”，供后续所有章节核对。只提取材料明确支持的事实，不补写新凶手、新证据或新结局。

作品参数：
{compact_json(parameters, 4000)}

世界观：
{lore[:12000]}

全书结构与章节大纲：
{design_context[:60000]}

已有法律基线（可按世界观明确设定调整，但必须保持同一法域）：
{compact_json(baseline.get('legal_system', {}), 5000)}

只输出一个 JSON 对象：
{{
  "central_question": "全书最终追问",
  "central_conflict": {{"legal_answer":"法律表面答案","truth_answer":"事实真相","moral_question":"合法与正义的冲突"}},
  "truth": [{{"id":"T001","fact":"确定事实","source":"来自哪一阶段或章节大纲","must_not_reveal_before":"允许揭露的位置"}}],
  "chronology": [{{"order":1,"event":"案件真实时间线事件","known_initially_by":[]}}],
  "legal_system": {{"model":"统一法域说明","baseline_rules":[],"investigation_roles":[],"trial_roles":[]}},
  "fair_play_obligations": [{{"truth_id":"T001","required_clue":"揭晓前必须出现的线索","deadline":"最晚埋设位置"}}]
}}
如果大纲本身存在相互冲突的真相，在对应事实中增加 "conflict" 字段说明，不要擅自选边。"""
        try:
            result = self._call_json(prompt)
        except Exception as exc:
            self.logger.warning("Could not build case bible from story design: %s", exc)
            result = dict(baseline)
            result["case_bible_warning"] = str(exc)

        normalized = dict(baseline)
        normalized.update(result)
        for key in ("truth", "chronology", "fair_play_obligations"):
            if not isinstance(normalized.get(key), list):
                normalized[key] = []
        if not isinstance(normalized.get("central_conflict"), dict):
            normalized["central_conflict"] = {}
        if not isinstance(normalized.get("legal_system"), dict):
            normalized["legal_system"] = baseline.get("legal_system", {})
        return normalized

    def build_chapter_contract(
        self,
        chapter_number: int,
        scene_plan: str,
        parameters: Dict[str, Any],
        lore: str,
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
    ) -> Dict[str, Any]:
        prompt = f"""你是法律悬疑小说的章节设计编辑。请根据材料为第 {chapter_number} 章建立一份可执行的章节契约。

原则：每章只追问一个问题；用一个具体细节承载深度；读者获得足够线索但暂时缺少关键关系；决定性反转必须有公平伏笔；法律程序必须符合案件规则；场景之间不得重复动作或提前完成下一场任务。

作品参数：
{compact_json(parameters, 4000)}

案件真相与法律规则：
{compact_json(case_bible, 8000)}

既有悬疑账本：
{compact_json(suspense_ledger, 8000)}

世界观：
{lore[:12000]}

本章场景规划：
{scene_plan[:16000]}

只输出一个 JSON 对象，不要使用代码围栏。字段必须包括：
{{
  "core_question": "本章唯一核心问题",
  "concrete_anchor": "贯穿本章的具体细节或证据",
  "reader_knows_before": [],
  "reader_knows_after": [],
  "reader_must_not_know_yet": [],
  "character_knowledge_after": {{"人物名":["本章结束时新知道的事实"]}},
  "apparent_answer": "表面答案",
  "reversal": "如何改变对已有信息的理解",
  "attack_move": "一方本章行动",
  "defense_move": "另一方回应",
  "personal_cost": "本章不可逆个人代价",
  "cost_character": "承担代价的人物",
  "irreversible_change": "本章结束后的不可逆变化",
  "fair_play_clues": [{{"id":"C001","surface_meaning":"表面含义","true_meaning":"真实含义","introduced_at":"scene_1","payoff_at":"后续位置"}}],
  "evidence_updates": [{{"id":"E001","item":"证物或证词","status":"发现/提取/封存/移交/质证/排除","custodian":"当前保管人","chain_risk":"证据链风险"}}],
  "legal_checkpoints": [{{"action":"关键程序行为","actor":"执行者","authority":"权限依据","risk":"程序风险"}}],
  "scene_boundaries": [{{"scene_number":1,"must_do":[],"must_not_do":[],"end_state":"结束状态"}}]
}}
不要增加场景规划和世界观中不存在的决定性证据。"""
        try:
            contract = self._call_json(prompt)
        except Exception as exc:
            self.logger.warning("Could not build LLM chapter contract: %s", exc)
            contract = self._fallback_contract(chapter_number, parameters, scene_plan)
            contract["contract_warning"] = str(exc)
        return self._normalize_contract(contract, chapter_number)

    @staticmethod
    def _fallback_contract(
        chapter_number: int,
        parameters: Dict[str, Any],
        scene_plan: str,
    ) -> Dict[str, Any]:
        theme = parameters.get("Theme") or parameters.get("theme") or "本章真相如何被发现或掩盖？"
        scene_count = len(re.findall(r"(?m)^#{2,6}\s*场景\s*\d+", scene_plan)) or 1
        return {
            "chapter": chapter_number,
            "core_question": theme,
            "concrete_anchor": "本章场景规划中的核心证据或行动",
            "reader_knows_before": [],
            "reader_knows_after": [],
            "reader_must_not_know_yet": [],
            "apparent_answer": "",
            "reversal": "",
            "attack_move": "",
            "defense_move": "",
            "personal_cost": "",
            "cost_character": "",
            "irreversible_change": "",
            "fair_play_clues": [],
            "legal_checkpoints": [],
            "scene_boundaries": [
                {"scene_number": number, "must_do": [], "must_not_do": [], "end_state": ""}
                for number in range(1, scene_count + 1)
            ],
        }

    @staticmethod
    def _normalize_contract(contract: Dict[str, Any], chapter_number: int) -> Dict[str, Any]:
        normalized = dict(contract)
        normalized["chapter"] = chapter_number
        for key in (
            "reader_knows_before",
            "reader_knows_after",
            "reader_must_not_know_yet",
            "fair_play_clues",
            "evidence_updates",
            "legal_checkpoints",
            "scene_boundaries",
        ):
            if not isinstance(normalized.get(key), list):
                normalized[key] = []
        for key in (
            "core_question",
            "concrete_anchor",
            "apparent_answer",
            "reversal",
            "attack_move",
            "defense_move",
            "personal_cost",
            "cost_character",
            "irreversible_change",
        ):
            normalized[key] = str(normalized.get(key, ""))
        if not isinstance(normalized.get("character_knowledge_after"), dict):
            normalized["character_knowledge_after"] = {}
        return normalized

    def review_plan(
        self,
        scene_plan: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
    ) -> DomainReview:
        return self._review(
            stage="plan",
            content=scene_plan,
            contract=contract,
            case_bible=case_bible,
            suspense_ledger=suspense_ledger,
            extra_context="检查场景分工是否清楚，是否存在重复行动，以及反转是否只依赖提前出现的线索。",
        )

    def review_scene(
        self,
        scene_content: str,
        scene_plan: str,
        scene_number: int,
        previous_scene_tail: str,
        next_scene_plan: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
    ) -> DomainReview:
        style_warnings = analyze_chinese_prose_style(scene_content)
        extra = f"""当前场景编号：{scene_number}
当前场景规划：{scene_plan[:8000]}
上一场结尾：{previous_scene_tail[-2500:]}
下一场规划：{next_scene_plan[:5000]}
本地文风检查：{json.dumps(style_warnings, ensure_ascii=False)}
重点检查：是否重复上一场动作、提前完成下一场任务、泄露暂不应公开的信息，以及法律或证据行为是否可信。"""
        return self._review(
            stage=f"scene_{scene_number}",
            content=scene_content,
            contract=contract,
            case_bible=case_bible,
            suspense_ledger=suspense_ledger,
            extra_context=extra,
        )

    def review_chapter(
        self,
        chapter_content: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
    ) -> DomainReview:
        return self._review(
            stage="chapter",
            content=chapter_content,
            contract=contract,
            case_bible=case_bible,
            suspense_ledger=suspense_ledger,
            extra_context="检查整章是否围绕唯一问题推进，结尾是否改变理解，攻防是否完成一轮，个人代价是否真实且不可逆。",
        )

    def _review(
        self,
        stage: str,
        content: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        extra_context: str,
    ) -> DomainReview:
        prompt = f"""你是严格但克制的法律悬疑质量编辑。评审 {stage}，只判断可证实的问题，不要为了显得严格而虚构缺陷。

章节契约：
{compact_json(contract, 10000)}

案件规则：
{compact_json(case_bible, 7000)}

悬疑账本：
{compact_json(suspense_ledger, 7000)}

额外上下文：
{extra_context}

待评审内容：
{content[:18000]}

硬失败代码仅可使用：{', '.join(sorted(HARD_FAILURE_CODES))}。
评分维度为0到4分：{', '.join(SCORE_DIMENSIONS)}。
任何批评必须附待评审内容中的短引文；没有引文的缺陷不要报告。修复建议必须限定范围，避免无关全文重写。

只输出 JSON：
{{
  "scores": {{"focus_depth": 0, "concrete_detail": 0, "information_gap": 0, "fair_play": 0, "reversal": 0, "legal_realism": 0, "moral_gray": 0, "attack_defense": 0, "personal_cost": 0, "continuity": 0, "chinese_prose": 0}},
  "hard_failures": [{{"code":"CONTINUITY_DUPLICATION","quote":"原文短引文","problem":"具体问题"}}],
  "evidence": [{{"dimension":"continuity","quote":"原文短引文","assessment":"为什么通过或失败"}}],
  "repair_scope": "scene_2_opening 或具体段落",
  "repair_instructions": ["可执行的最小修复"],
  "strengths": ["具体优点"]
}}"""
        try:
            raw = self._call_json(prompt)
            return self._normalize_review(stage, raw, content)
        except Exception as exc:
            self.logger.error("Domain review unavailable for %s: %s", stage, exc)
            raise DomainReviewError(f"{stage} 质量检查未能返回有效结果：{exc}") from exc

    @staticmethod
    def _normalize_review(
        stage: str,
        raw: Dict[str, Any],
        content: str = "",
    ) -> DomainReview:
        scores = {}
        raw_scores = raw.get("scores", {}) if isinstance(raw.get("scores"), dict) else {}
        for dimension in SCORE_DIMENSIONS:
            try:
                score = float(raw_scores.get(dimension, 3.0))
            except (TypeError, ValueError):
                score = 3.0
            scores[dimension] = max(0.0, min(4.0, score))

        hard_failures = []
        for failure in raw.get("hard_failures", []):
            if not isinstance(failure, dict):
                continue
            code = str(failure.get("code", ""))
            quote = str(failure.get("quote", "")).strip()
            if code in HARD_FAILURE_CODES and (not content or (quote and quote in content)):
                hard_failures.append(failure)

        average = sum(scores.values()) / len(scores)
        required_dimensions = ("fair_play", "legal_realism", "continuity")
        passed = (
            not hard_failures
            and average >= 3.2
            and all(scores[dimension] >= 3.0 for dimension in required_dimensions)
        )
        return DomainReview(
            stage=stage,
            passed=passed,
            scores=scores,
            hard_failures=hard_failures,
            evidence=[item for item in raw.get("evidence", []) if isinstance(item, dict)],
            repair_scope=str(raw.get("repair_scope", "")),
            repair_instructions=[str(item) for item in raw.get("repair_instructions", [])],
            strengths=[str(item) for item in raw.get("strengths", [])],
        )

    def revise_plan(
        self,
        scene_plan: str,
        review: DomainReview,
        contract: Dict[str, Any],
    ) -> str:
        prompt = f"""请只修复下面场景规划中已被评审指出的问题，保持章节核心事件、人物和场景数量不变。不得增加新的决定性证据或支线。

章节契约：
{compact_json(contract, 8000)}

评审：
{compact_json(review.to_dict(), 8000)}

原场景规划：
{scene_plan}

输出完整修订版场景规划。保持“### 场景 1：标题”的 Markdown 格式，不要附加说明。"""
        return self.send_prompt(prompt, model=self.model).strip()

    def revise_scene(
        self,
        scene_content: str,
        review: DomainReview,
        scene_plan: str,
        previous_scene_tail: str,
        next_scene_plan: str,
        contract: Dict[str, Any],
    ) -> str:
        prompt = f"""请对场景正文进行最小范围修订，只处理评审指出的问题。不要改变已经通过的情节，不要增加新证据，不要提前完成下一场任务，只输出修订后的场景正文。

章节契约：
{compact_json(contract, 8000)}

当前场景规划：
{scene_plan[:8000]}

上一场结尾：
{previous_scene_tail[-2500:]}

下一场规划边界：
{next_scene_plan[:5000]}

评审意见：
{compact_json(review.to_dict(), 8000)}

原正文：
{scene_content}
"""
        return self.send_prompt(prompt, model=self.model).strip()
