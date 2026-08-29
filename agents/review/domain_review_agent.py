"""按领域档案参数化的章节设计与评审代理。

原 `LegalSuspenseReviewAgent` 把法律悬疑的评分维度、硬失败代码、契约字段和
prompt 措辞写死在类里。这里把这些全部改由 `DomainProfile` 提供，法律悬疑成为
其中一个档案；`legal_suspense_review_agent` 保留为兼容入口。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.generation.ai_helper import send_prompt
from core.generation.domain_profiles import DomainProfile, get_domain_profile
from core.generation.narrative_quality import analyze_narrative_quality
from core.generation.prompt_context import analyze_chinese_prose_style
from core.generation.story_ledger import build_ledger_prompt_view, compact_json


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
    # 每条形如 {"dimension","quote","missing","change"}：这一维扣的分具体扣在
    # 哪一句上，以及改成什么才能拿满分。评审给不出它，就说明它其实没找到问题。
    upgrades: List[Dict[str, Any]] = field(default_factory=list)
    strengths: List[str] = field(default_factory=list)
    reviewer_warning: str = ""
    pass_average: float = 0.0
    blocking_dimensions: List[str] = field(default_factory=list)
    # 章节总评可由契约、盲读、现实合理性三个彼此独立的评审合成。保留原始分评审，
    # 事后才能看出究竟是“没按计划写”还是“计划写到了但读起来仍不成立”。
    component_reviews: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    waived: bool = False

    @property
    def average_score(self) -> float:
        values = [float(value) for value in self.scores.values() if isinstance(value, (int, float))]
        return sum(values) / len(values) if values else 0.0

    @property
    def soft_failure(self) -> bool:
        """未过闸，且唯一的失败原因就是平均分没到门槛。

        没有硬失败、必要维度也都达标，说明评审自己认定的是「合格但不出彩」，
        不是有缺陷。这类稿子和真正的硬伤必须区别对待：前者不该让整轮写作停机。

        门槛为 0 时判定不成立：那说明这份评审不是走 `_normalize_review` 算出来
        的，平均分根本没参与判定，失败原因在别处，不能当成差分放过。
        """
        return (
            not self.passed
            and not self.hard_failures
            and not self.blocking_dimensions
            and self.pass_average > 0
            and self.average_score < self.pass_average
        )

    @property
    def shortfall(self) -> float:
        """离门槛还差多少分；已达标时为 0。"""
        return max(self.pass_average - self.average_score, 0.0)

    @property
    def has_actionable_repair(self) -> bool:
        """评审是否给出了可据以重修的具体依据。"""
        return bool(self.hard_failures or self.repair_instructions or self.upgrades)

    @property
    def asks(self) -> List[str]:
        """把这份评审要求的改动摊平成一份可逐条核对的清单。

        重修提示词原先直接塞整份评审 JSON，其中 scores、strengths、判「通过」的
        evidence 占了大半篇幅，真正要改的那两句反而淹没在里面；模型据此重写，命中
        率自然低。这里只留「要改什么」，并且顺序固定：硬伤在前，加分项在后。
        """
        items: List[str] = []
        for failure in self.hard_failures:
            quote = str(failure.get("quote", "")).strip()
            items.append(
                f"【硬伤·{failure.get('code', '')}】{failure.get('problem', '')}"
                + (f"（原文：「{quote}」）" if quote else "")
            )
        items.extend(str(item) for item in self.repair_instructions if str(item).strip())
        for upgrade in self.upgrades:
            quote = str(upgrade.get("quote", "")).strip()
            items.append(
                f"【{upgrade.get('dimension', '')}】原文「{quote}」缺少"
                f"{upgrade.get('missing', '')}；改为：{upgrade.get('change', '')}"
            )
        return items

    def unmet_asks_from(self, requested: List[str]) -> List[str]:
        """上一轮提过、这一轮还原样提着的改动。

        同一处问题挺过它自己的修复，说明那句话没被模型听懂，再原样重复一遍不会有
        新结果。把它单独拎出来，既能在提示词里换一种说法强调，也能让重试循环据此
        判断这一轮到底有没有进展。
        """
        current = set(self.asks)
        return [ask for ask in requested if ask in current]

    def waive(self, reason: str) -> "DomainReview":
        """放行一份只差分数的稿子，并把放行理由写进记录。

        返回副本而不是就地改写，原始判定仍以 passed=False 存档，便于事后复盘。
        """
        data = asdict(self)
        data["passed"] = True
        data["waived"] = True
        data["reviewer_warning"] = reason
        return DomainReview(**data)

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


# 所有档案共有的章节契约字段。领域专属字段由 profile.contract_fields 追加。
#
# 后半段（facts_* / timeline_events / character_updates / plot_thread_updates）
# 是长程状态的单一事实源：DefaultChapterDeltaExtractor 按同名键读取它们构造
# ChapterDelta，CanonConsistencyGate 再拿 delta 与账本比对。此前这些键没有出现
# 在契约 schema 里，大模型从不产出，delta 槽位恒为空表，跨章事实与时间线校验
# 因此形同虚设。
UNIVERSAL_CONTRACT_LISTS = (
    "reader_knows_before",
    "reader_knows_after",
    "reader_must_not_know_yet",
    "scene_boundaries",
    "facts_added",
    "facts_confirmed",
    "facts_contradicted",
    "timeline_events",
    "character_updates",
    "plot_thread_updates",
)

UNIVERSAL_CONTRACT_TEXTS = (
    "chapter_function",
    "core_question",
    "concrete_anchor",
    "apparent_answer",
    "reversal",
    "attack_move",
    "defense_move",
    "personal_cost",
    "cost_character",
    "irreversible_change",
    "ending_effect",
)


class DomainReviewAgent:
    """Build chapter contracts and review plans/prose with evidence-based output."""

    def __init__(
        self,
        model: str,
        profile: Optional[DomainProfile] = None,
        logger: Optional[logging.Logger] = None,
        send_prompt_fn: Callable[..., str] = send_prompt,
    ):
        self.model = model
        self.profile = profile or get_domain_profile("general")
        self.logger = logger or logging.getLogger("domain-review")
        self.send_prompt = send_prompt_fn

    # --- profile-derived prompt fragments ---------------------------------

    @property
    def score_dimensions(self):
        return self.profile.score_dimensions

    @property
    def hard_failure_codes(self):
        return self.profile.hard_failure_codes

    def _score_template(self) -> str:
        return ", ".join(f'"{dimension}": 0' for dimension in self.profile.score_dimensions)

    # 0-4 分只给维度名不给标尺时，模型会把几乎所有维度都打成 3 分——「还行」是
    # 最安全的答案。而 3 分整份稿子的平均分正好卡在门槛下方，于是评审一边在
    # evidence 里逐条写「通过」，一边判不通过，还给不出任何修复项，重修只能原地
    # 打转。把 3 分明确定义为「可以替换成同题材任何一章」，模型就有了判断依据，
    # 4 分也有了可执行的目标。
    _SCORE_RUBRIC = """评分标尺（每个维度都按这把尺子打分，不要默认给 3 分）：
0＝该维度在本稿中完全缺席，或写成了相反的东西。
1＝有痕迹但不成立，读者无法据此得到该维度应有的效果。
2＝靠概述交代过去，没有落到具体的人、物、动作上。
3＝达标但可替换：把这一段搬到同题材任何一章都能成立，没有只属于本章的东西。
4＝不可替换：用了本章契约里的具体细节，换一章就不成立。"""

    _BLIND_SCORE_RUBRIC = """评分标尺（按真实阅读感受，不按作者意图）：
0＝该问题使正文无法成立或无法读懂。
1＝读者能猜到作者想写什么，但人物、信息或语言明显失真。
2＝能读懂，仍有明显说明书感、模板感或行为跳步。
3＝中国类型小说读者可以顺畅读下去，但表达或推进较常规。
4＝自然、具体、可信；效果来自正文自身，不依赖大纲解释。"""

    _BLIND_DIMENSIONS = (
        "opening_pull",
        "reader_orientation",
        "character_credibility",
        "scene_dynamics",
        "subtext",
        "narrative_restraint",
        "chinese_readability",
    )
    _BLIND_REQUIRED = ("reader_orientation", "character_credibility", "chinese_readability")
    _BLIND_HARD_FAILURES = frozenset(
        {"READER_CONFUSION", "CHARACTER_LOGIC_BREAK", "AI_TEMPLATE_SATURATION"}
    )
    _PLAUSIBILITY_DIMENSIONS = (
        "behavioral_logic",
        "evidence_handling",
        "procedural_plausibility",
        "technical_plausibility",
        "claim_calibration",
    )
    _PLAUSIBILITY_REQUIRED = (
        "behavioral_logic",
        "evidence_handling",
        "technical_plausibility",
    )
    _PLAUSIBILITY_HARD_FAILURES = frozenset(
        {
            "IMPOSSIBLE_MECHANISM",
            "EVIDENCE_SELF_DESTRUCTION",
            "PROCEDURAL_IMPOSSIBILITY",
            "UNSUPPORTED_PRECISION",
        }
    )

    def _contract_schema_block(self) -> str:
        lines = [
            '  "chapter_function": "advance|reveal|relationship|aftermath|transition|breather",',
            '  "core_question": "本章唯一核心问题",',
            '  "concrete_anchor": "贯穿本章的具体细节或事物",',
            '  "reader_knows_before": [],',
            '  "reader_knows_after": [],',
            '  "reader_must_not_know_yet": [],',
            '  "character_knowledge_after": {"人物名":["本章结束时新知道的事实"]},',
            '  "apparent_answer": "表面答案",',
            '  "reversal": "如何改变对已有信息的理解",',
            '  "attack_move": "一方本章行动",',
            '  "defense_move": "另一方回应",',
            '  "personal_cost": "本章个人代价；没有则留空",',
            '  "cost_character": "承担代价的人物",',
            '  "irreversible_change": "确有不可逆变化时填写；过渡、余波、关系或沉淀章可留空",',
            '  "ending_effect": "推进、代价、认知变化、关系位移或有意留白中的实际收束效果",',
            # 以下四组构成长程状态的单一事实源。字段名与 CanonConsistencyGate
            # 比对的 immutable_fields 一一对应，改动时两边必须同步。
            '  "facts_added": [{"id":"F001","fact":"事实名称（属性，不是整句）",'
            '"value":"该事实的取值","first_stated_at":"scene_1"}],',
            '  "facts_confirmed": [{"id":"F001","fact":"事实名称","value":"取值（必须与既有记录一致）"}],',
            '  "facts_contradicted": [{"id":"F001","reason":"本章为何推翻它","new_value":"新取值"}],',
            '  "timeline_events": [{"id":"TL001","event":"事件名称","time":"HH:MM 或明确时刻",'
            '"location_id":"地点"}],',
            '  "character_updates": [{"id":"CU001","character":"人物规范名",'
            '"attribute":"属性名","value":"取值","stable":true}],',
            '  "plot_thread_updates": [{"id":"PT001","thread":"线索名称",'
            '"status":"open 或 closed","deadline_chapter":0}],',
        ]
        lines.extend(
            f'  "{item.name}": {item.schema_hint},' for item in self.profile.contract_fields
        )
        lines.append(
            '  "scene_boundaries": [{"scene_number":1,"must_do":[],"must_not_do":[],"end_state":"结束状态"}]'
        )
        return "{\n" + "\n".join(lines) + "\n}"

    def _central_conflict_schema(self) -> str:
        pairs = ",".join(
            f'"{key}":"{value}"' for key, value in self.profile.central_conflict_schema.items()
        )
        return "{" + pairs + "}"

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

    # --- canon conflicts --------------------------------------------------

    def decide_fact_conflicts(
        self,
        chapter_number: int,
        conflicts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Make the model state whether a clashing value is a mistake or a reveal.

        A stable attribute with two values is ambiguous on its face: it may be a
        slip in this chapter's contract, or a deliberate reversal the story is
        building to.  Silently rewriting either one destroys information, so the
        model is asked to commit — and a reversal has to come with a reason,
        which turns a vague clash into an auditable decision a person can rule on.
        """
        listing = "\n".join(
            f"- 冲突 {index + 1}｜记录 {item.get('id')}｜字段 {item.get('field')}\n"
            f"    账本已接受：{item.get('existing')}\n"
            f"    本章声明：  {item.get('proposed')}"
            for index, item in enumerate(conflicts)
        )
        prompt = f"""你在核对第 {chapter_number} 章的章节契约与故事账本。账本记录的是前面章节已经通过验收、写进正文的状态。

以下取值互相冲突：
{listing}

对每一条冲突二选一：
- "keep_existing"：本章契约写错了，应当沿用账本里的取值。
- "contradict"：这是本章有意推翻既有设定的剧情转折，必须给出 reason 说明故事内的依据。

不要为了省事一律选 contradict——只有当推翻既有事实本身就是本章要写的转折时才选它。

只输出一个 JSON 对象，不要解释，不要代码围栏：
{{"decisions":[{{"id":"记录ID","field":"字段名","decision":"keep_existing|contradict","reason":"选 contradict 时必填"}}]}}
"""
        payload = self._call_json(prompt)
        decisions = payload.get("decisions")
        return {"decisions": decisions if isinstance(decisions, list) else []}


    # --- story bible ------------------------------------------------------

    def build_case_bible(
        self,
        parameters: Dict[str, Any],
        lore: str,
        design_context: str,
        baseline: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Establish whole-story truth once from structure and chapter outlines."""
        profile = self.profile
        existing_rules = baseline.get("domain_rules") or baseline.get("legal_system", {})
        prompt = f"""你是{profile.bible_role}。请把已经存在的全书结构和章节大纲整理成稳定的“{profile.bible_noun}”，供后续所有章节核对。只提取材料明确支持的事实，不补写新的关键转折、新设定或新结局。

作品参数：
{compact_json(parameters, 4000)}

世界观：
{lore[:12000]}

全书结构与章节大纲：
{design_context[:60000]}

已有领域基线（可按世界观明确设定调整，但必须保持同一体系）：
{compact_json(existing_rules, 5000)}

只输出一个 JSON 对象：
{{
  "central_question": "全书最终追问",
  "central_conflict": {self._central_conflict_schema()},
  "truth": [{{"id":"T001","fact":"确定事实","source":"来自哪一阶段或章节大纲","must_not_reveal_before":"允许揭露的位置"}}],
  "chronology": [{{"order":1,"event":"故事真实时间线事件","known_initially_by":[]}}],
  "domain_rules": {{"model":"统一体系说明","baseline_rules":[],"roles":[],"limits":[]}},
  "fair_play_obligations": [{{"truth_id":"T001","required_clue":"揭晓前必须出现的铺垫","deadline":"最晚埋设位置"}}]
}}
体系基线参考（可细化，不得自相矛盾）：{json.dumps(list(profile.baseline_rules), ensure_ascii=False)}
如果大纲本身存在相互冲突的真相，在对应事实中增加 "conflict" 字段说明，不要擅自选边。"""
        try:
            result = self._call_json(prompt)
        except Exception as exc:
            self.logger.warning("Could not build story bible from story design: %s", exc)
            result = dict(baseline)
            result["case_bible_warning"] = str(exc)

        normalized = dict(baseline)
        normalized.update(result)
        for key in ("truth", "chronology", "fair_play_obligations"):
            if not isinstance(normalized.get(key), list):
                normalized[key] = []
        if not isinstance(normalized.get("central_conflict"), dict):
            normalized["central_conflict"] = {}
        if not isinstance(normalized.get("domain_rules"), dict):
            normalized["domain_rules"] = existing_rules if isinstance(existing_rules, dict) else {}
        normalized["domain_profile"] = profile.key
        return normalized

    # --- chapter contract -------------------------------------------------

    def build_chapter_contract(
        self,
        chapter_number: int,
        scene_plan: str,
        parameters: Dict[str, Any],
        lore: str,
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
    ) -> Dict[str, Any]:
        profile = self.profile
        principles = "；".join(profile.design_principles)
        prompt = f"""你是{profile.designer_role}。请根据材料为第 {chapter_number} 章建立一份可执行的章节契约。

原则：{principles}。

作品参数：
{compact_json(parameters, 4000)}

{profile.bible_noun}与体系规则：
{compact_json(case_bible, 8000)}

既有故事账本：
{compact_json(build_ledger_prompt_view(suspense_ledger, chapter_number), 8000)}

世界观：
{lore[:12000]}

本章场景规划：
{scene_plan[:16000]}

只输出一个 JSON 对象，不要使用代码围栏。字段必须包括：
{self._contract_schema_block()}
不要增加场景规划和世界观中不存在的决定性信息。

id 使用规则（最重要）：
- 上面「既有故事账本」和「{profile.bible_noun}」里出现过的每一条记录都已经有
  id。本章只要再次涉及同一件事物、同一条事实、同一个事件，就必须原样沿用它
  已有的 id，不得另起新号。
- 只有本章首次出现的事物才分配新 id，且必须与既有 id 不重复。
- 判断是不是「同一件事」看内容而不是措辞：换一种说法描述同一条事实，仍然是
  同一条记录，必须用原 id。

长程状态字段的填写规则（facts_added、facts_confirmed、facts_contradicted、
timeline_events、character_updates、plot_thread_updates）：
- fact 写属性名而不是整句。例如写“保罗·米勒的死亡方式”，取值“后巷两枪”放进
  value；不要把“保罗在后巷被两枪打死”整句塞进 fact，否则换一种说法就会被当成
  另一条事实。
- 本章只要复述或依赖某条既有事实，就放进 facts_confirmed，且 value 必须与账本
  中的原值一致；只有本章确实要推翻它时才放进 facts_contradicted 并说明理由。
- timeline_events 的 time 必须是明确时刻，同一事件在全书只能有一个时间。
- character_updates 的 character 用人物规范名（与人物名单一致），不要使用别名或
  简称；一个人物的每项属性各占一条记录，不要合并成一条。
- character_updates 的 stable：工龄、年龄、籍贯、亲属关系等一经确立就不该再变的
  属性填 true；伤势、所在位置、掌握的情报等会随剧情推进变化的填 false。填 true
  的属性此后各章必须复述同一取值，数量务必与前文一致。
- plot_thread_updates 里，本章新开的线索 status 填 open 并给出 deadline_chapter
  （最晚必须闭合的章号）；本章了结的线索 status 填 closed。
- 无法从场景规划中确定的条目就留空，不要臆造。"""
        try:
            contract = self._call_json(prompt)
        except Exception as exc:
            self.logger.warning("Could not build LLM chapter contract: %s", exc)
            contract = self._fallback_contract(chapter_number, parameters, scene_plan)
            contract["contract_warning"] = str(exc)
        return self._normalize_contract(contract, chapter_number)

    def deterministic_contract(
        self,
        chapter_number: int,
        parameters: Dict[str, Any],
        scene_plan: str,
    ) -> Dict[str, Any]:
        """Build a contract without calling the model.

        Used when the quality loop is switched off: the chapter still needs a
        contract shell so acceptance can record a delta and advance the ledger.
        """
        return self._normalize_contract(
            self._fallback_contract(chapter_number, parameters, scene_plan),
            chapter_number,
        )

    def _fallback_contract(
        self,
        chapter_number: int,
        parameters: Dict[str, Any],
        scene_plan: str,
    ) -> Dict[str, Any]:
        theme = parameters.get("Theme") or parameters.get("theme") or "本章的核心问题是什么？"
        scene_count = len(re.findall(r"(?m)^#{2,6}\s*场景\s*\d+", scene_plan)) or 1
        contract: Dict[str, Any] = {
            "chapter": chapter_number,
            "core_question": theme,
            "concrete_anchor": "本章场景规划中的核心事物或行动",
            "scene_boundaries": [
                {"scene_number": number, "must_do": [], "must_not_do": [], "end_state": ""}
                for number in range(1, scene_count + 1)
            ],
        }
        for key in UNIVERSAL_CONTRACT_LISTS:
            contract.setdefault(key, [])
        for key in UNIVERSAL_CONTRACT_TEXTS:
            contract.setdefault(key, "")
        for item in self.profile.contract_fields:
            contract[item.name] = [] if item.is_list else ({} if item.is_mapping else "")
        return contract

    def _normalize_contract(self, contract: Dict[str, Any], chapter_number: int) -> Dict[str, Any]:
        normalized = dict(contract)
        normalized["chapter"] = chapter_number
        normalized["domain_profile"] = self.profile.key
        normalized.setdefault("chapter_function", "advance")
        normalized.setdefault(
            "ending_effect", str(normalized.get("irreversible_change", ""))
        )

        list_fields = list(UNIVERSAL_CONTRACT_LISTS)
        mapping_fields = ["character_knowledge_after"]
        for item in self.profile.contract_fields:
            if item.is_list:
                list_fields.append(item.name)
            elif item.is_mapping:
                mapping_fields.append(item.name)

        for key in list_fields:
            if not isinstance(normalized.get(key), list):
                normalized[key] = []
        for key in mapping_fields:
            if not isinstance(normalized.get(key), dict):
                normalized[key] = {}
        for key in UNIVERSAL_CONTRACT_TEXTS:
            normalized[key] = str(normalized.get(key, ""))
        return normalized

    # --- reviews ----------------------------------------------------------

    def review_plan(
        self,
        scene_plan: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        return self._review(
            stage="plan",
            content=scene_plan,
            contract=contract,
            case_bible=case_bible,
            suspense_ledger=suspense_ledger,
            extra_context=self.profile.review_focus.get("plan", ""),
            repairs_requested=repairs_requested,
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
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        style_warnings = analyze_chinese_prose_style(scene_content)
        extra = f"""当前场景编号：{scene_number}
当前场景规划：{scene_plan[:8000]}
上一场结尾：{previous_scene_tail[-2500:]}
下一场规划：{next_scene_plan[:5000]}
本地文风检查：{json.dumps(style_warnings, ensure_ascii=False)}
重点检查：{self.profile.review_focus.get('scene', '')}"""
        return self._review(
            stage=f"scene_{scene_number}",
            content=scene_content,
            contract=contract,
            case_bible=case_bible,
            suspense_ledger=suspense_ledger,
            extra_context=extra,
            repairs_requested=repairs_requested,
        )

    def review_chapter(
        self,
        chapter_content: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        return self._review(
            stage="chapter",
            content=chapter_content,
            contract=contract,
            case_bible=case_bible,
            suspense_ledger=suspense_ledger,
            extra_context=self.profile.review_focus.get("chapter", ""),
            repairs_requested=repairs_requested,
        )

    def review_reader_blind(
        self,
        chapter_content: str,
        previous_chapter_tail: str = "",
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        """Judge the reading experience without seeing the chapter contract.

        The ordinary reviewer knows every promised beat, so it can mistake intent
        for effect.  This reviewer receives only what an actual serial-fiction
        reader has: the previous ending and the new prose.
        """

        diagnostics = analyze_narrative_quality(chapter_content)
        context = f"""你没有章节大纲、章节契约、作者解释或标准答案，也不得猜测它们。
你是一位长期阅读中文类型小说的普通读者，只判断正文实际产生的效果。

上一章结尾（仅用于衔接判断，第一章可能为空）：
{previous_chapter_tail[-2500:]}

{diagnostics.prompt_block()}

特别检查：
- 中国读者是否能自然理解人物为什么这样说、这样做，而不是靠旁白替人物解释。
- 主题是否由情节和选择浮现，还是被总结句反复说破。
- 情绪是否总靠喉咙、胸口、冷汗等可替换的身体反应。
- 情节是否像单轨任务清单，每个冲突都立刻得到整齐答案。
- 对话、叙述和场景转换是否符合自然中文阅读节奏。
统计信号为零不等于缺陷；只有你能引用正文并说明真实阅读后果时才能扣分。"""
        return self._specialized_review(
            stage="reader_blind",
            role="中文类型小说盲读审稿人",
            content=chapter_content,
            context=context,
            score_dimensions=self._BLIND_DIMENSIONS,
            required_dimensions=self._BLIND_REQUIRED,
            hard_failure_codes=self._BLIND_HARD_FAILURES,
            pass_average=max(3.2, min(3.4, self.profile.pass_average)),
            repairs_requested=repairs_requested,
        )

    def review_plausibility(
        self,
        chapter_content: str,
        case_bible: Dict[str, Any],
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        """Review real-world mechanisms independently from contract compliance."""

        context = f"""你看不到章节契约，因此不能用“符合计划”替代现实合理性判断。
你只检查人物行为、证据保全、法律/组织程序和技术机制是否足以支撑正文中的结论。

作品已经明确声明的虚构体系规则（只有这里写明的规则才可覆盖现实常识）：
{compact_json(case_bible.get('domain_rules', {}), 7000)}

判定原则：
- 角色可以犯错，但正文必须把错误当作角色错误，而不是可靠方法。
- 精确到设备、算法、法条、鉴定能力或程序结果的说法，若既无正文依据也未在虚构规则中声明，
  不得因为听起来专业就放行；会误导核心推理时使用 UNSUPPORTED_PRECISION。
- 销毁、污染或改变唯一证物后仍从中得出原本需要该证物才能支持的结论，使用
  EVIDENCE_SELF_DESTRUCTION。
- 只报告会改变情节可信度的机制问题，不纠缠无关紧要的行业措辞。"""
        return self._specialized_review(
            stage="plausibility",
            role="现实合理性与专业机制审稿人",
            content=chapter_content,
            context=context,
            score_dimensions=self._PLAUSIBILITY_DIMENSIONS,
            required_dimensions=self._PLAUSIBILITY_REQUIRED,
            hard_failure_codes=self._PLAUSIBILITY_HARD_FAILURES,
            pass_average=max(3.2, min(3.4, self.profile.pass_average)),
            repairs_requested=repairs_requested,
        )

    def review_chapter_bundle(
        self,
        chapter_content: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        previous_chapter_tail: str = "",
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        """Run three independent chapter gates and retain each verdict."""

        reviews = {
            "contract": self.review_chapter(
                chapter_content,
                contract,
                case_bible,
                suspense_ledger,
                repairs_requested=repairs_requested,
            ),
            "reader_blind": self.review_reader_blind(
                chapter_content,
                previous_chapter_tail,
                repairs_requested=repairs_requested,
            ),
            "plausibility": self.review_plausibility(
                chapter_content,
                case_bible,
                repairs_requested=repairs_requested,
            ),
        }
        return self._merge_reviews("chapter", reviews)

    def _specialized_review(
        self,
        *,
        stage: str,
        role: str,
        content: str,
        context: str,
        score_dimensions: tuple[str, ...],
        required_dimensions: tuple[str, ...],
        hard_failure_codes: frozenset[str],
        pass_average: float,
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        template = ", ".join(f'"{dimension}": 0' for dimension in score_dimensions)
        prompt = f"""你是{role}。请独立评审下面这一章，只判断正文中可证实的问题。

{context}

待评审正文：
{content[:18000]}
{self._repair_check_block(list(repairs_requested or []))}
硬失败代码仅可使用：{', '.join(sorted(hard_failure_codes))}。
评分维度为0到4分：{', '.join(score_dimensions)}。
{self._BLIND_SCORE_RUBRIC}

任何批评必须附正文中的短引文。每个 3 分或更低的维度必须在 upgrades 中指出原句、
缺失效果和最小改法；无法引用原文就不要报告。repair_scope 请写 scene_1、scene_2 等可路由位置，
无法判断场次时写最接近问题的段落描述。

只输出 JSON：
{{
  "scores": {{{template}}},
  "hard_failures": [{{"code":"允许的代码","quote":"正文短引文","problem":"阅读或机制后果"}}],
  "evidence": [{{"dimension":"维度","quote":"正文短引文","assessment":"判断依据"}}],
  "upgrades": [{{"dimension":"维度","quote":"正文短引文","missing":"缺少的效果","change":"最小改法"}}],
  "repair_scope": "scene_1 或具体段落",
  "repair_instructions": ["可执行的最小修复"],
  "strengths": ["正文中真实成立的优点"]
}}"""
        try:
            raw = self._call_json(prompt)
            return self._normalize_review(
                stage,
                raw,
                content,
                score_dimensions=score_dimensions,
                required_dimensions=required_dimensions,
                hard_failure_codes=hard_failure_codes,
                pass_average=pass_average,
            )
        except Exception as exc:
            self.logger.error("Specialized review unavailable for %s: %s", stage, exc)
            raise DomainReviewError(f"{stage} 质量检查未能返回有效结果：{exc}") from exc

    @staticmethod
    def _merge_reviews(stage: str, reviews: Dict[str, DomainReview]) -> DomainReview:
        scores: Dict[str, float] = {}
        failures: List[Dict[str, Any]] = []
        evidence: List[Dict[str, Any]] = []
        upgrades: List[Dict[str, Any]] = []
        repairs: List[str] = []
        strengths: List[str] = []
        blocking: List[str] = []
        repair_scope = ""
        weighted_threshold = 0.0
        score_count = 0
        for name, review in reviews.items():
            scores.update({f"{name}.{key}": value for key, value in review.scores.items()})
            weighted_threshold += review.pass_average * len(review.scores)
            score_count += len(review.scores)
            for item in review.hard_failures:
                failures.append({**item, "review": name})
            for item in review.evidence:
                copied = dict(item)
                if copied.get("dimension"):
                    copied["dimension"] = f"{name}.{copied['dimension']}"
                evidence.append(copied)
            for item in review.upgrades:
                copied = dict(item)
                copied["dimension"] = f"{name}.{copied.get('dimension', '')}"
                upgrades.append(copied)
            repairs.extend(f"【{name}】{item}" for item in review.repair_instructions)
            strengths.extend(f"【{name}】{item}" for item in review.strengths)
            blocking.extend(f"{name}.{item}" for item in review.blocking_dimensions)
            if not review.passed and not repair_scope:
                repair_scope = review.repair_scope
        return DomainReview(
            stage=stage,
            passed=all(review.passed for review in reviews.values()),
            scores=scores,
            hard_failures=failures,
            evidence=evidence,
            repair_scope=repair_scope,
            repair_instructions=repairs,
            upgrades=upgrades,
            strengths=strengths,
            pass_average=weighted_threshold / score_count if score_count else 0.0,
            blocking_dimensions=blocking,
            component_reviews={name: review.to_dict() for name, review in reviews.items()},
        )

    @staticmethod
    def _repair_check_block(repairs_requested: List[str]) -> str:
        """让评审先核对上一轮要求的改动，再去找新问题。

        每一轮都从零开始重评，等于让模型每次抽一组不同的缺陷：上一轮要求改的地方
        改好了也没人确认，这一轮新挑的毛病下一轮又换一批。重修因此永远在追一个移动
        的靶子，两次重试用完也收敛不了。
        """
        if not repairs_requested:
            return ""
        listed = "\n".join(f"{index}. {item}" for index, item in enumerate(repairs_requested, 1))
        return f"""
上一轮评审要求的改动（本稿是照此修订后的结果）：
{listed}

先逐条判断上面每一项是否已经落实，把结论写进 evidence。已落实的不要再作为问题重复
提出；确实没落实的必须原样保留在 repair_instructions 里，并说明它为什么还不成立。
不要因为已经改过一轮就去挑新的、与上述条目无关的毛病。
"""

    def _review(
        self,
        stage: str,
        content: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        extra_context: str,
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        profile = self.profile
        prompt = f"""你是{profile.reviewer_role}。评审 {stage}，只判断可证实的问题，不要为了显得严格而虚构缺陷。

章节契约：
{compact_json(contract, 10000)}

{profile.bible_noun}与体系规则：
{compact_json(case_bible, 7000)}

故事账本：
{compact_json(build_ledger_prompt_view(suspense_ledger, int(contract.get("chapter", 0) or 0)), 7000)}

额外上下文：
{extra_context}

待评审内容：
{content[:18000]}
{self._repair_check_block(list(repairs_requested or []))}
硬失败代码仅可使用：{', '.join(sorted(profile.hard_failure_codes))}。
评分维度为0到4分：{', '.join(profile.score_dimensions)}。
{self._SCORE_RUBRIC}

任何批评必须附待评审内容中的短引文；没有引文的缺陷不要报告。修复建议必须限定范围，避免无关全文重写。
**每一个打了 3 分或更低的维度，都必须在 upgrades 里出现一条**，指出扣分扣在哪一句上、
缺的是什么、改成什么才能到 4 分。给不出这一条就说明该维度并没有问题，请改打 4 分。
判不通过却一条修复项都开不出来，是这份评审自己没做完，不是稿子没毛病。

只输出 JSON：
{{
  "scores": {{{self._score_template()}}},
  "hard_failures": [{{"code":"CONTINUITY_DUPLICATION","quote":"原文短引文","problem":"具体问题"}}],
  "evidence": [{{"dimension":"continuity","quote":"原文短引文","assessment":"为什么通过或失败"}}],
  "upgrades": [{{"dimension":"reversal","quote":"原文短引文","missing":"这一句缺什么","change":"改成什么才算 4 分"}}],
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

    def _normalize_review(
        self,
        stage: str,
        raw: Dict[str, Any],
        content: str = "",
        *,
        score_dimensions: Optional[tuple[str, ...]] = None,
        required_dimensions: Optional[tuple[str, ...]] = None,
        hard_failure_codes: Optional[frozenset[str]] = None,
        pass_average: Optional[float] = None,
    ) -> DomainReview:
        profile = self.profile
        dimensions = score_dimensions or profile.score_dimensions
        required = required_dimensions or profile.required_dimensions
        allowed_failures = hard_failure_codes or profile.hard_failure_codes
        threshold = profile.pass_average if pass_average is None else pass_average
        scores = {}
        raw_scores = raw.get("scores", {}) if isinstance(raw.get("scores"), dict) else {}
        for dimension in dimensions:
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
            if code in allowed_failures and (not content or (quote and quote in content)):
                hard_failures.append(failure)

        # 只认对得上维度、带真引文的加分项：没有引文就无法定位要改哪里，
        # 留着它只会让重修凭空发挥，和没给一样。
        upgrades = []
        for upgrade in raw.get("upgrades", []):
            if not isinstance(upgrade, dict):
                continue
            dimension = str(upgrade.get("dimension", ""))
            quote = str(upgrade.get("quote", "")).strip()
            change = str(upgrade.get("change", "")).strip()
            if dimension not in scores or not change:
                continue
            if content and quote and quote not in content:
                continue
            upgrades.append(
                {
                    "dimension": dimension,
                    "quote": quote,
                    "missing": str(upgrade.get("missing", "")).strip(),
                    "change": change,
                }
            )

        average = sum(scores.values()) / len(scores)
        blocking_dimensions = [
            dimension
            for dimension in required
            if scores.get(dimension, 0.0) < 3.0
        ]
        passed = (
            not hard_failures
            and average >= threshold
            and not blocking_dimensions
        )
        return DomainReview(
            stage=stage,
            passed=passed,
            scores=scores,
            hard_failures=hard_failures,
            evidence=[item for item in raw.get("evidence", []) if isinstance(item, dict)],
            repair_scope=str(raw.get("repair_scope", "")),
            repair_instructions=[str(item) for item in raw.get("repair_instructions", [])],
            upgrades=upgrades,
            strengths=[str(item) for item in raw.get("strengths", [])],
            pass_average=threshold,
            blocking_dimensions=blocking_dimensions,
        )

    # --- revisions --------------------------------------------------------

    @staticmethod
    def revision_focus(review: DomainReview) -> str:
        """当评审判不通过却没给修改依据时，替它折算出重修方向。

        评审经常一边判 passed=False、一边把 repair_instructions 留空并写
        repair_scope="none"——它认为没有硬伤，只是分数没到门槛。把这种评审原样
        回抛给模型，等于要求它「照旧再写一遍」，重修必然原地打转，重试次数白白
        耗尽后整轮写作就被一份评审自己都说没问题的稿子卡停。这里改为点名最低分
        维度和分差，让重修至少有一个可执行的着力点。
        """
        if review.has_actionable_repair or not review.scores or review.pass_average <= 0:
            return ""
        lowest = min(review.scores.values())
        weakest = [name for name, score in review.scores.items() if score <= lowest + 0.01]
        if len(weakest) == len(review.scores):
            # 各维度全打成同一个分数时逐一列出等于没说，直接点明它是一份平庸的
            # 稿子，把力气用在最能拉分的地方。
            target = f"所有维度都停在 {lowest:.1f} 分，说明整场平庸而非某一处出错。"
        else:
            target = f"最低分维度是 {'、'.join(weakest[:4])}（各 {lowest:.1f} 分），请集中提升它们。"
        return (
            f"\n重修方向（评审未列出具体修复项，按分差给出）：\n"
            f"上一稿平均 {review.average_score:.2f} 分，门槛 {review.pass_average:.2f} 分，"
            f"还差 {review.shortfall:.2f} 分。没有硬伤，问题不在错误而在力度。{target}"
            f"保持既定情节、人物关系和信息边界不变，靠更准确的动作、更具体可核实的"
            f"细节和更锋利的对白加强，不要靠拉长篇幅或增加新信息充数。\n"
        )

    def revision_brief(
        self,
        review: DomainReview,
        unmet_asks: Optional[List[str]] = None,
    ) -> str:
        """把这一轮要改的东西写成编号清单，重复出现的单独点名。

        原先的做法是把整份评审 to_dict 后塞进提示词。里面 scores、strengths 和判
        「通过」的 evidence 占了绝大部分篇幅，真正要动的那一两句混在中间，模型很
        容易照着「哪里都还行」的整体印象重写一遍，改动落不到点上。
        """
        asks = review.asks
        if not asks:
            return self.revision_focus(review)
        repeated = set(unmet_asks or [])
        lines = []
        for index, ask in enumerate(asks, 1):
            mark = "（上一轮已提出，仍未解决）" if ask in repeated else ""
            lines.append(f"{index}. {ask}{mark}")
        brief = "必须修复的问题（逐条对应，不要遗漏，也不要改动未列出的部分）：\n" + "\n".join(lines)
        if repeated:
            brief += (
                "\n\n注意：标注「仍未解决」的条目，上一轮已经按同样的说法要求过一次而没有"
                "改动到位。这一次请直接改写被引用的那句原文本身，不要只在周围补充内容。"
            )
        if review.strengths:
            kept = "；".join(str(item) for item in review.strengths[:3])
            brief += f"\n\n下列已经写好的地方保持不变，不要在修复过程中削弱它们：{kept}"
        return brief

    def revise_plan(
        self,
        scene_plan: str,
        review: DomainReview,
        contract: Dict[str, Any],
        unmet_asks: Optional[List[str]] = None,
    ) -> str:
        prompt = f"""请只修复下面场景规划中已被评审指出的问题，保持章节核心事件、人物和场景数量不变。不得增加新的决定性信息或支线。

章节契约：
{compact_json(contract, 8000)}

{self.revision_brief(review, unmet_asks)}

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
        unmet_asks: Optional[List[str]] = None,
    ) -> str:
        prompt = f"""请对场景正文进行最小范围修订，只处理评审指出的问题。不要改变已经通过的情节，不要增加新的决定性信息，不要提前完成下一场任务，只输出修订后的场景正文。

章节契约：
{compact_json(contract, 8000)}

当前场景规划：
{scene_plan[:8000]}

上一场结尾：
{previous_scene_tail[-2500:]}

下一场规划边界：
{next_scene_plan[:5000]}

{self.revision_brief(review, unmet_asks)}

原正文：
{scene_content}
"""
        return self.send_prompt(prompt, model=self.model).strip()

    def revise_for_acceptance(
        self,
        scenes: List[str],
        scene_plans: List[str],
        acceptance_report: Dict[str, Any],
        contract: Dict[str, Any],
    ) -> tuple[int, str]:
        """Choose and minimally repair one scene after final artifact rejection.

        The planning contract is immutable here: final acceptance may repair
        prose, but it may not rewrite upstream facts or thread obligations.
        """
        payload = {
            f"scene_{index}": {"plan": scene_plans[index - 1], "prose": prose}
            for index, prose in enumerate(scenes, 1)
        }
        prompt = f"""最终章节验收未通过。请只修改一个最相关的正文场景来修复验收报告指出的问题，不得修改章节契约，不得增加新事实或新支线。

不可修改的章节契约：
{compact_json(contract, 9000)}

验收报告：
{compact_json(acceptance_report, 9000)}

场景规划与正文：
{compact_json(payload, 18000)}

只输出一个 JSON 对象：
{{"scene_number": 1, "revised_scene": "修订后的完整场景正文"}}
"""
        raw = self._call_json(prompt)
        try:
            scene_number = int(raw.get("scene_number"))
        except (TypeError, ValueError):
            raise DomainReviewError("验收修订结果缺少有效 scene_number") from None
        revised_scene = str(raw.get("revised_scene", "")).strip()
        if not 1 <= scene_number <= len(scenes):
            raise DomainReviewError("验收修订结果的 scene_number 超出范围")
        if not revised_scene:
            raise DomainReviewError("验收修订结果正文为空")
        return scene_number, revised_scene
