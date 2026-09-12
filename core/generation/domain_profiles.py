"""按题材关键词分派的领域档案（domain profile）。

设计-生成-审阅闭环原先把「法律悬疑」硬编码在评审代理里。本模块把其中随题材
变化的部分——评分维度、硬失败代码、契约字段、底稿规则、prompt 角色措辞——
抽成数据，由 `resolve_domain_profile()` 按 Genre/Subgenre 关键词分派。

不变量：`LEGAL_SUSPENSE` 的评分维度顺序、硬失败代码集合与门槛必须与改造前
逐项相等，既有项目的评审数据才可比。`tests/test_domain_profiles.py` 守这条线。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional, Tuple

from core.generation.prompt_context import normalize_story_parameters


# --- 通用底座 -------------------------------------------------------------

# 评分维度分成「前缀 + 领域维度 + 后缀」三段拼装，保证所有档案的通用维度位置一致。
_LEAD_DIMENSIONS = ("focus_depth", "concrete_detail", "information_gap")
_TAIL_DIMENSIONS = ("continuity", "chinese_prose")

BASE_HARD_FAILURES = frozenset(
    {
        "TRUTH_CONTRADICTION",
        "CONTINUITY_DUPLICATION",
        "KNOWLEDGE_LEAK",
        "NEXT_SCENE_PREMATURE",
        "TYPE_CONFLICT",
    }
)

BASE_DESIGN_PRINCIPLES = (
    "每章设置一个主问题；可以保留不抢夺主线的关系、人物或环境支线",
    "用一个具体细节承载深度",
    "场景之间不得重复动作或提前完成下一场任务",
    "章末留下推进、代价、认知变化、关系位移或有意留白之一；过渡、余波和沉淀章不强求不可逆变化",
    "全书保持强弱节奏，不要让每章都以升级、逃亡、揭晓或爆炸式钩子收束",
)

BASE_SCENE_RULES = (
    "只推进一个明确的问题或张力，用可核实的动作和细节表现，不用抽象总结代替情节。",
    "严格控制信息差：人物只能依据其已知信息行动，铺垫出现后才允许据此推断。",
    "本场结束时应有可感知的位移（行动、认知、关系、气氛或问题加深均可）；过渡场允许以余波和留白收束。不要重复上一场已经完成的动作、介绍和环境描写。",
)


def _dimensions(*domain_dimensions: str) -> Tuple[str, ...]:
    """按固定顺序拼装完整评分维度表。"""
    return _LEAD_DIMENSIONS + tuple(domain_dimensions) + _TAIL_DIMENSIONS


@dataclass(frozen=True)
class ContractField:
    """一条领域专属的章节契约字段。

    `delta_slot` 决定该字段在验收阶段汇入哪个带稳定 id 的记录流。两个槽位
    （clue_updates / evidence_updates）分别对应账本里的 `clues` 与 `evidence`
    列表——它们是通用的「有 id、可追踪、字段一经确立不得改写」的记录容器，
    并不限定于推理题材的线索和证物。
    """

    name: str
    schema_hint: str
    delta_slot: str = ""
    # 约定：immutable_fields 的第一项是这条记录的「名字」，其余是随之固定的属性。
    immutable_fields: Tuple[str, ...] = ()
    # 自然键：id 对不上时用它判定「说的是同一件事」，防止换一个 id 就绕过一致性
    # 闸门。留空则按上述约定回落到 immutable_fields 的第一项；只有当记录的身份
    # 字段不是首个不可变字段时才需要显式指定（例如线索按表面含义认身份，而被
    # 锁定的是真实含义）。
    identity_fields: Tuple[str, ...] = ()

    @property
    def is_list(self) -> bool:
        return self.schema_hint.strip().startswith("[")

    @property
    def is_mapping(self) -> bool:
        return self.schema_hint.strip().startswith("{")


@dataclass(frozen=True)
class DomainProfile:
    """一个题材族的闭环配置。"""

    key: str
    label: str
    bible_noun: str
    bible_role: str
    designer_role: str
    reviewer_role: str
    score_dimensions: Tuple[str, ...]
    required_dimensions: Tuple[str, ...]
    pass_average: float
    hard_failure_codes: frozenset
    contract_fields: Tuple[ContractField, ...]
    rules_model: str
    baseline_rules: Tuple[str, ...]
    central_conflict_schema: Mapping[str, str]
    design_principles: Tuple[str, ...]
    review_focus: Mapping[str, str]
    scene_writing_rules: Tuple[str, ...]
    max_plan_retries: int = 2
    max_scene_retries: int = 2

    def contract_field(self, name: str) -> Optional[ContractField]:
        return next((item for item in self.contract_fields if item.name == name), None)

    def fields_for_slot(self, slot: str) -> Tuple[ContractField, ...]:
        return tuple(item for item in self.contract_fields if item.delta_slot == slot)

    def immutable_fields_for_slot(self, slot: str) -> Tuple[str, ...]:
        merged: list[str] = []
        for item in self.fields_for_slot(slot):
            for name in item.immutable_fields:
                if name not in merged:
                    merged.append(name)
        return tuple(merged)

    def identity_fields_for_slot(self, slot: str) -> Tuple[str, ...]:
        merged: list[str] = []
        for item in self.fields_for_slot(slot):
            # 未显式声明时回落到首个不可变字段（见 ContractField.identity_fields）。
            names = item.identity_fields or item.immutable_fields[:1]
            for name in names:
                if name not in merged:
                    merged.append(name)
        return tuple(merged)


# --- 各题材档案 -----------------------------------------------------------

LEGAL_SUSPENSE = DomainProfile(
    key="legal_suspense",
    label="法律悬疑",
    bible_noun="案件底稿",
    bible_role="法律悬疑小说的案件架构编辑",
    designer_role="法律悬疑小说的章节设计编辑",
    reviewer_role="严格但克制的法律悬疑质量编辑",
    score_dimensions=_dimensions(
        "fair_play",
        "reversal",
        "legal_realism",
        "moral_gray",
        "attack_defense",
        "personal_cost",
    ),
    required_dimensions=("fair_play", "legal_realism", "continuity"),
    pass_average=3.2,
    hard_failure_codes=BASE_HARD_FAILURES | {"UNSEEDED_SOLUTION", "LEGAL_IMPOSSIBILITY"},
    contract_fields=(
        ContractField(
            name="fair_play_clues",
            schema_hint=(
                '[{"id":"C001","surface_meaning":"表面含义","true_meaning":"真实含义",'
                '"introduced_at":"scene_1","payoff_at":"后续位置"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("true_meaning",),
            # 线索按「表面含义」认身份，被锁定的却是「真实含义」，
            # 不适用首个不可变字段的默认约定。
            identity_fields=("surface_meaning",),
        ),
        ContractField(
            name="evidence_updates",
            schema_hint=(
                '[{"id":"E001","item":"证物或证词","status":"发现/提取/封存/移交/质证/排除",'
                '"custodian":"当前保管人","chain_risk":"证据链风险"}]'
            ),
            delta_slot="evidence_updates",
            immutable_fields=("item", "origin", "type"),
        ),
        ContractField(
            name="legal_checkpoints",
            schema_hint=(
                '[{"action":"关键程序行为","actor":"执行者","authority":"权限依据","risk":"程序风险"}]'
            ),
        ),
    ),
    rules_model="虚构法域；具体规则以世界观为准，并在全书保持一致",
    baseline_rules=(
        "侦查机关负责调查和收集证据，不能代替检察机关提起公诉",
        "搜查住宅原则上需要法官签发的搜查令或世界观明确规定的紧急例外",
        "决定性物证必须记录来源、提取、封存、移交和检验过程",
        "法医与技术结论必须可以复核，不能只依赖权力人物的口头判断",
    ),
    central_conflict_schema={
        "legal_answer": "法律表面答案",
        "truth_answer": "事实真相",
        "moral_question": "合法与正义的冲突",
    },
    design_principles=(
        "每章只追问一个问题",
        "用一个具体细节承载深度",
        "读者获得足够线索但暂时缺少关键关系",
        "决定性反转必须有公平伏笔",
        "法律程序必须符合案件规则",
        "场景之间不得重复动作或提前完成下一场任务",
    ),
    review_focus={
        "plan": "检查场景分工是否清楚，是否存在重复行动，以及反转是否只依赖提前出现的线索。",
        "scene": "是否重复上一场动作、提前完成下一场任务、泄露暂不应公开的信息，以及法律或证据行为是否可信。",
        "chapter": "检查整章是否围绕唯一问题推进，结尾是否改变理解，攻防是否完成一轮，个人代价是否真实且不可逆。",
    },
    scene_writing_rules=(
        "如涉及反转，必须由本章契约中已安排的公平伏笔触发，并改变人物的判断或行动。",
        "法律程序须符合本章契约与案件底稿，不得让角色凭身份跳过取证、移交、质证等关键约束。",
    ),
)


DETECTIVE_MYSTERY = DomainProfile(
    key="detective_mystery",
    label="侦探推理",
    bible_noun="案件底稿",
    bible_role="推理小说的案件架构编辑",
    designer_role="推理小说的章节设计编辑",
    reviewer_role="严格但克制的推理小说质量编辑",
    score_dimensions=_dimensions(
        "fair_play",
        "reversal",
        "deduction_logic",
        "suspect_pressure",
        "personal_cost",
    ),
    required_dimensions=("fair_play", "deduction_logic", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES | {"UNSEEDED_SOLUTION", "DEDUCTION_LEAP"},
    contract_fields=(
        ContractField(
            name="fair_play_clues",
            schema_hint=(
                '[{"id":"C001","surface_meaning":"表面含义","true_meaning":"真实含义",'
                '"introduced_at":"scene_1","payoff_at":"后续位置"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("true_meaning",),
            # 线索按「表面含义」认身份，被锁定的却是「真实含义」，
            # 不适用首个不可变字段的默认约定。
            identity_fields=("surface_meaning",),
        ),
        ContractField(
            name="evidence_updates",
            schema_hint=(
                '[{"id":"E001","item":"物证或证词","status":"发现/提取/检验/排除",'
                '"custodian":"当前保管人","chain_risk":"可靠性风险"}]'
            ),
            delta_slot="evidence_updates",
            immutable_fields=("item", "origin", "type"),
        ),
        ContractField(
            name="suspect_states",
            schema_hint=(
                '[{"id":"S001","suspect":"人物名","alibi":"当前不在场说明",'
                '"suspicion":"上升/下降/不变","pressure":"本章承受的压力"}]'
            ),
        ),
    ),
    rules_model="虚构侦查体系；调查权限与技术手段以世界观为准，并在全书保持一致",
    baseline_rules=(
        "关键推断必须建立在读者已经见过的信息上，不得依赖叙述者独有的知识",
        "决定性物证必须交代来源和保管过程",
        "巧合可以制造麻烦，但不得用于解开谜题",
        "侦查者的权限和资源在全书保持一致",
    ),
    central_conflict_schema={
        "apparent_answer": "表面结论",
        "truth_answer": "事实真相",
        "moral_question": "追查真相所要付出的代价",
    },
    design_principles=(
        *BASE_DESIGN_PRINCIPLES,
        "读者获得足够线索但暂时缺少关键关系",
        "决定性反转必须有公平伏笔",
    ),
    review_focus={
        "plan": "检查场景分工是否清楚，线索埋设与回收位置是否合理，推断是否只依赖已出现的信息。",
        "scene": "是否重复上一场动作、提前给出结论、泄露暂不应公开的信息，以及推断链条是否完整。",
        "chapter": "检查整章是否围绕唯一疑问推进，嫌疑格局是否发生变化，推断是否可被读者复核。",
    },
    scene_writing_rules=(
        "如涉及反转，必须由已经出现过的线索触发，并改变人物的判断或行动。",
        "推断必须写出中间环节，不得由人物直接宣布结论。",
        # 下面四条各对应一个长期低分的评审维度。写法照潜台词那条：给一个写的时候
        # 能当场核的判据，而不是一句抽象禁令。复发形态是从本书 100 多条「重修后
        # 该维度确实涨分」的改法里数出来的，各自二三十条，说的都是同一件事。
        # 证据处理：物证被拿来当依据，却没交代它怎么来的（29 条）。
        "正文里被当作依据的每一件物证或数据，必须先交代它是谁在哪一步取得并移交的——"
        "封签、编号、流转底单、原始数据校验，至少写出一样。凭空出现的东西不能拿来定性。",
        # 技术可信：一个现场动作直接换来实验室级结论（29 条）。
        "现场靠肉眼、触摸、翻看得到的只能写成现象。凡是带百分比、浓度、型号、分型的"
        "定性，必须先写出它来自哪一次检测或哪一份报告；写不出来源就改写成现象。",
        # 断言分寸：结论强过手上的证据（26 条）。
        "结论不得超出手上证据能排他证明的范围。没有排他依据时写「与……吻合」"
        "「不排除」，不要写「就是」「一定」「唯一」。",
        # 程序可信：人物做了不属于他职权的事（24 条）。
        "每一条命令、每一次拘捕、每一份文书，只能由本场在场且确有此职权的人发出。"
        "换人做就要先写出授权是怎么来的，不能靠身份跳过。",
    ),
)


THRILLER = DomainProfile(
    key="thriller",
    label="惊悚",
    bible_noun="威胁底稿",
    bible_role="惊悚小说的威胁架构编辑",
    designer_role="惊悚小说的章节设计编辑",
    reviewer_role="严格但克制的惊悚小说质量编辑",
    score_dimensions=_dimensions(
        "tension_escalation",
        "time_pressure",
        "antagonist_pressure",
        "stakes_cost",
        "reversal",
    ),
    required_dimensions=("tension_escalation", "stakes_cost", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES | {"STAKES_DEFLATION", "IMPLAUSIBLE_ESCAPE"},
    contract_fields=(
        ContractField(
            name="planted_setups",
            schema_hint=(
                '[{"id":"P001","setup":"本章埋下的手段或信息","introduced_at":"scene_1",'
                '"payoff_at":"后续位置"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("setup",),
        ),
        ContractField(
            name="threat_updates",
            schema_hint=(
                '[{"id":"T001","threat":"威胁来源","capability":"已展示的能力",'
                '"proximity":"与主角的距离","status":"升级/维持/受挫"}]'
            ),
            delta_slot="evidence_updates",
            immutable_fields=("threat", "capability"),
        ),
        ContractField(
            name="deadline_state",
            schema_hint='{"deadline":"本章生效的时限","remaining":"剩余余地","consequence":"逾期后果"}',
        ),
    ),
    rules_model="虚构行动体系；机构权限、技术手段与地理限制以世界观为准，并在全书保持一致",
    baseline_rules=(
        "对手必须具备已经展示过的能力和资源，不得临时膨胀",
        "主角脱身必须付出代价或依赖此前交代过的手段",
        "时限一经设定就必须真实生效，不得随剧情需要延长",
        "机构程序、装备与通信条件在全书保持一致",
    ),
    central_conflict_schema={
        "apparent_threat": "表面威胁",
        "truth_answer": "真正的威胁与其目的",
        "moral_question": "主角为阻止它必须付出的代价",
    },
    design_principles=(
        *BASE_DESIGN_PRINCIPLES,
        "每章的压力必须高于上一章，或改变压力的性质",
        "主角的每一次脱身都要缩小其后续选择空间",
    ),
    review_focus={
        "plan": "检查压力是否逐场升级，时限是否真实生效，脱身手段是否已经提前交代。",
        "scene": "是否重复上一场动作、临时赋予对手新能力、让主角无代价脱身，以及紧迫感是否由具体处境产生。",
        "chapter": "检查整章张力是否单调重复，结尾是否收紧处境，代价是否真实且不可逆。",
    },
    scene_writing_rules=(
        "对手的能力必须限于此前已经展示的范围，不得为制造危机临时增强。",
        "主角每一次化解危机都必须付出可见的代价或消耗已交代的资源。",
    ),
)


HORROR = DomainProfile(
    key="horror",
    label="恐怖",
    bible_noun="恐惧底稿",
    bible_role="恐怖小说的异常体系架构编辑",
    designer_role="恐怖小说的章节设计编辑",
    reviewer_role="严格但克制的恐怖小说质量编辑",
    score_dimensions=_dimensions(
        "dread_escalation",
        "unknown_preservation",
        "sensory_body",
        "psychological_toll",
    ),
    required_dimensions=("dread_escalation", "unknown_preservation", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES | {"DREAD_DEFLATION", "THREAT_OVEREXPLAINED"},
    contract_fields=(
        ContractField(
            name="anomaly_rule_uses",
            schema_hint=(
                '[{"id":"A001","rule":"本章动用的异常规则","cost":"代价","witnessed_by":[],'
                '"introduced_at":"scene_1"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("rule", "cost"),
        ),
        ContractField(
            name="dread_beats",
            schema_hint=(
                '[{"id":"D001","signal":"可感知的异常征兆","channel":"视觉/听觉/触觉/嗅觉",'
                '"explained":false}]'
            ),
            delta_slot="evidence_updates",
            immutable_fields=("signal",),
        ),
        ContractField(
            name="sanity_updates",
            schema_hint='[{"character":"人物名","toll":"本章承受的心理或身体损伤","cumulative":"累积状态"}]',
        ),
    ),
    rules_model="虚构超自然或异常体系；其规则、代价与边界以世界观为准，并在全书保持一致",
    baseline_rules=(
        "异常现象必须遵守已经确立的规则，不得临时新增能力解围",
        "恐惧来源保留未知空间；每解释一次就失去一次威慑",
        "人物的心理与身体损伤应当累积，不得在下一章自动复原",
        "安全区域一经确立就必须稳定，破例需要付出代价",
    ),
    central_conflict_schema={
        "apparent_threat": "表面异常",
        "truth_answer": "真实来源",
        "moral_question": "人物无法承受的认知",
    },
    design_principles=(
        *BASE_DESIGN_PRINCIPLES,
        "恐惧靠具体可感的细节积累，不靠形容词堆叠",
        "每章至多解释一层未知，并同时打开新的未知",
    ),
    review_focus={
        "plan": "检查恐惧是否逐场累积，异常规则是否一致，是否过早解释恐惧来源。",
        "scene": "是否重复上一场惊吓、把异常解释得过于完整、损伤是否被无故复原，以及体感细节是否具体。",
        "chapter": "检查整章恐惧是否升级，未知是否仍有余地，人物心理代价是否真实累积。",
    },
    scene_writing_rules=(
        "异常必须遵守底稿已确立的规则；不得为脱困临时赋予新能力。",
        "写出具体的体感与环境细节承载恐惧，不用抽象形容词直接宣布气氛。",
    ),
)


SCIFI = DomainProfile(
    key="scifi",
    label="科幻",
    bible_noun="设定底稿",
    bible_role="科幻小说的设定架构编辑",
    designer_role="科幻小说的章节设计编辑",
    reviewer_role="严格但克制的科幻小说质量编辑",
    score_dimensions=_dimensions(
        "worldbuilding_consistency",
        "novum_payoff",
        "extrapolation_logic",
        "tech_rule_discipline",
    ),
    required_dimensions=("worldbuilding_consistency", "tech_rule_discipline", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES | {"TECH_RULE_VIOLATION", "WORLD_CONTRADICTION"},
    contract_fields=(
        ContractField(
            name="tech_rule_uses",
            schema_hint=(
                '[{"id":"R001","capability":"本章动用的技术能力","limit":"能力边界",'
                '"cost":"代价","introduced_at":"scene_1"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("capability", "limit", "cost"),
        ),
        ContractField(
            name="world_facts",
            schema_hint=(
                '[{"id":"W001","fact":"本章确立的世界事实","scope":"适用范围","source":"来源"}]'
            ),
            delta_slot="evidence_updates",
            immutable_fields=("fact", "scope"),
        ),
    ),
    rules_model="虚构技术与社会体系；技术能力、成本与限制以世界观为准，并在全书保持一致",
    baseline_rules=(
        "每项技术必须有明确的能力边界和代价，并在全书表现一致",
        "解决冲突的技术手段必须在使用前已经出现",
        "社会与制度的推演要与技术前提相符",
        "不得为单章需要临时改写物理或技术规则",
    ),
    central_conflict_schema={
        "apparent_answer": "表面问题",
        "truth_answer": "根本矛盾",
        "moral_question": "技术前提带来的代价",
    },
    design_principles=(
        *BASE_DESIGN_PRINCIPLES,
        "新奇设定必须在被使用之前出现，并交代其限制",
        "推演结论要能从已确立的前提推出",
    ),
    review_focus={
        "plan": "检查技术前提是否一致，关键手段是否提前出现，推演是否能从设定推出。",
        "scene": "是否重复上一场动作、越过技术能力边界、引入世界观未定义的设定，以及细节是否具体可核实。",
        "chapter": "检查整章是否围绕单一问题推进，设定是否自洽，技术代价是否真实体现。",
    },
    scene_writing_rules=(
        "技术表现必须限于底稿已确立的能力边界与代价，不得临时扩展。",
        "只使用世界观已经定义的技术、机构与地点，不得自行新增。",
    ),
)


FANTASY = DomainProfile(
    key="fantasy",
    label="奇幻",
    bible_noun="设定底稿",
    bible_role="奇幻小说的设定架构编辑",
    designer_role="奇幻小说的章节设计编辑",
    reviewer_role="严格但克制的奇幻小说质量编辑",
    score_dimensions=_dimensions(
        "magic_rule_consistency",
        "magic_cost",
        "worldbuilding_consistency",
        "wonder",
    ),
    required_dimensions=("magic_rule_consistency", "worldbuilding_consistency", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES | {"MAGIC_RULE_VIOLATION", "WORLD_CONTRADICTION"},
    contract_fields=(
        ContractField(
            name="magic_rule_uses",
            schema_hint=(
                '[{"id":"M001","ability":"本章动用的能力","limit":"限制与禁忌",'
                '"cost":"代价","introduced_at":"scene_1"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("ability", "limit", "cost"),
        ),
        ContractField(
            name="world_facts",
            schema_hint='[{"id":"W001","fact":"本章确立的世界事实","scope":"适用范围","source":"来源"}]',
            delta_slot="evidence_updates",
            immutable_fields=("fact", "scope"),
        ),
    ),
    rules_model="虚构魔法与秩序体系；能力、代价与禁忌以世界观为准，并在全书保持一致",
    baseline_rules=(
        "魔法必须有明确的代价和限制，并在全书表现一致",
        "关键解围手段必须提前出现，不得临时新增神器或血脉",
        "势力、种族与地理设定一经确立就不得随意改写",
        "力量的提升需要交代来源和代价",
    ),
    central_conflict_schema={
        "apparent_answer": "表面冲突",
        "truth_answer": "根本矛盾",
        "moral_question": "力量所要求的代价",
    },
    design_principles=(
        *BASE_DESIGN_PRINCIPLES,
        "魔法的使用必须伴随可见的代价",
        "奇观要通过人物的具体感受呈现，而非罗列设定",
    ),
    review_focus={
        "plan": "检查魔法规则是否一致，关键手段是否提前出现，代价是否落到具体人物身上。",
        "scene": "是否重复上一场动作、突破已确立的魔法限制、引入世界观未定义的设定或种族。",
        "chapter": "检查整章是否围绕单一问题推进，力量的代价是否真实，世界设定是否自洽。",
    },
    scene_writing_rules=(
        "魔法与超凡能力必须遵守底稿已确立的限制与代价，不得临时突破。",
        "只使用世界观已经定义的地域、势力与种族，不得自行新增。",
    ),
)


ROMANCE = DomainProfile(
    key="romance",
    label="爱情",
    bible_noun="关系底稿",
    bible_role="爱情小说的人物关系架构编辑",
    designer_role="爱情小说的章节设计编辑",
    reviewer_role="严格但克制的爱情小说质量编辑",
    score_dimensions=_dimensions(
        "relationship_progression",
        "emotional_turn",
        "obstacle_credibility",
        "interiority",
    ),
    required_dimensions=("relationship_progression", "obstacle_credibility", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES | {"RELATIONSHIP_REGRESSION", "OBSTACLE_CONTRIVANCE"},
    contract_fields=(
        ContractField(
            name="relationship_beats",
            schema_hint=(
                '[{"id":"B001","pair":"人物A-人物B","move":"本章的关系变化",'
                '"trigger":"触发事件","direction":"靠近/疏远/僵持"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("pair", "trigger"),
        ),
        ContractField(
            name="promise_updates",
            schema_hint=(
                '[{"id":"Q001","promise":"许下或打破的承诺","by":"承诺者","status":"许下/兑现/违背"}]'
            ),
            delta_slot="evidence_updates",
            immutable_fields=("promise", "by"),
        ),
    ),
    rules_model="人物关系与社会环境；身份、责任与外部压力以世界观为准，并在全书保持一致",
    baseline_rules=(
        "关系变化必须由具体事件推动，不得只靠叙述宣布",
        "障碍必须来自人物处境或性格，不得靠可以一句话澄清的误会拖延",
        "亲密与疏远都要有可观察的行为依据",
        "两人各自的目标独立于这段关系存在",
    ),
    central_conflict_schema={
        "apparent_answer": "表面障碍",
        "truth_answer": "真正的障碍",
        "moral_question": "双方各自必须放弃的东西",
    },
    design_principles=(
        *BASE_DESIGN_PRINCIPLES,
        "每章关系状态必须发生可指认的变化",
        "情绪通过行动和反应呈现，不由叙述者直接总结",
    ),
    review_focus={
        "plan": "检查关系推进是否有具体事件支撑，障碍是否可信，是否靠误会拖延。",
        "scene": "是否重复上一场的情绪、用叙述代替行动、障碍是否可以被一句话解除。",
        "chapter": "检查整章关系是否真正推进，情绪转折是否有依据，人物是否保有各自的目标。",
    },
    scene_writing_rules=(
        "关系变化必须由本场发生的具体事件推动，不得由叙述者直接宣布感情进展。",
        "障碍必须来自处境或性格；不要使用一句话即可澄清的误会。",
    ),
)


HISTORICAL = DomainProfile(
    key="historical",
    label="历史小说",
    bible_noun="时代底稿",
    bible_role="历史小说的时代考证编辑",
    designer_role="历史小说的章节设计编辑",
    reviewer_role="严格但克制的历史小说质量编辑",
    score_dimensions=_dimensions(
        "period_accuracy",
        "material_detail",
        "social_constraint",
    ),
    required_dimensions=("period_accuracy", "social_constraint", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES | {"ANACHRONISM", "SOCIAL_RULE_VIOLATION"},
    contract_fields=(
        ContractField(
            name="period_anchors",
            schema_hint=(
                '[{"id":"H001","detail":"本章使用的时代细节","category":"器物/称谓/制度/货币",'
                '"basis":"依据"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("detail", "category"),
        ),
        ContractField(
            name="social_constraints",
            schema_hint=(
                '[{"id":"L001","constraint":"约束人物的制度或身份限制","affects":"受限人物",'
                '"consequence":"违反后果"}]'
            ),
            delta_slot="evidence_updates",
            immutable_fields=("constraint",),
        ),
    ),
    rules_model="特定历史时期的制度、器物与语言；细节以世界观与时代设定为准，并在全书保持一致",
    baseline_rules=(
        "器物、称谓、机构与货币必须符合设定年代",
        "人物受当时的法律与社会等级约束，越界必须付出代价",
        "不得出现设定年代尚未存在的技术、制度或观念",
        "重大历史事件的时间与结果不得改动",
    ),
    central_conflict_schema={
        "apparent_answer": "表面冲突",
        "truth_answer": "时代根源",
        "moral_question": "个人意志与时代限制的冲突",
    },
    design_principles=(
        *BASE_DESIGN_PRINCIPLES,
        "用具体的器物与日常细节承载时代感",
        "人物的选择必须在当时的制度约束内成立",
    ),
    review_focus={
        "plan": "检查时代细节是否有依据，人物行动是否受当时制度约束，是否出现年代错置。",
        "scene": "是否重复上一场动作、出现年代错置的器物或观念、人物是否无代价地突破身份限制。",
        "chapter": "检查整章时代细节是否一致，社会约束是否真实生效，冲突是否有时代根源。",
    },
    scene_writing_rules=(
        "器物、称谓、制度与货币必须符合设定年代，不确定时使用朴素表达而非杜撰。",
        "人物的行动必须受当时的身份与制度约束；越界要写出代价。",
    ),
)


WESTERN = DomainProfile(
    key="western",
    label="西部",
    bible_noun="边疆底稿",
    bible_role="西部小说的边疆秩序架构编辑",
    designer_role="西部小说的章节设计编辑",
    reviewer_role="严格但克制的西部小说质量编辑",
    score_dimensions=_dimensions(
        "frontier_order",
        "violence_cost",
        "landscape_presence",
    ),
    required_dimensions=("violence_cost", "frontier_order", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES,
    contract_fields=(
        ContractField(
            name="frontier_stakes",
            schema_hint=(
                '[{"id":"F001","stake":"本章争夺的利害","claimant":"争夺方",'
                '"enforcement":"依靠的秩序或武力"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("stake",),
        ),
        ContractField(
            name="violence_ledger",
            schema_hint=(
                '[{"id":"V001","act":"本章发生的暴力","by":"施加者","cost":"留下的后果"}]'
            ),
            delta_slot="evidence_updates",
            immutable_fields=("act", "by"),
        ),
    ),
    rules_model="边疆秩序与执法体系；管辖、武力与通信条件以世界观为准，并在全书保持一致",
    baseline_rules=(
        "执法与私刑的边界必须清楚，越界要留下后果",
        "暴力必须留下伤亡、名声或法律上的实际后果",
        "距离、天气与补给对行动构成真实限制",
        "枪械、马匹与通信条件的能力在全书保持一致",
    ),
    central_conflict_schema={
        "apparent_answer": "表面纠纷",
        "truth_answer": "真正的利害",
        "moral_question": "秩序与暴力之间的代价",
    },
    design_principles=(
        *BASE_DESIGN_PRINCIPLES,
        "地理与天气应当参与情节，而不只是背景",
        "每一次动武都要改变人物此后的处境",
    ),
    review_focus={
        "plan": "检查利害是否具体，暴力是否留下后果，地理与补给限制是否生效。",
        "scene": "是否重复上一场动作、暴力是否无后果、距离与补给限制是否被无视。",
        "chapter": "检查整章利害是否推进，秩序与暴力的代价是否真实，地理是否参与情节。",
    },
    scene_writing_rules=(
        "每一次动武都必须留下伤亡、名声或法律上的具体后果。",
        "距离、天气与补给必须对人物行动构成真实限制。",
    ),
)


GENERAL = DomainProfile(
    key="general",
    label="通用",
    bible_noun="故事底稿",
    bible_role="小说的设定架构编辑",
    designer_role="小说的章节设计编辑",
    reviewer_role="严格但克制的小说质量编辑",
    score_dimensions=_dimensions(
        "plot_progression",
        "character_agency",
        "stakes_cost",
    ),
    required_dimensions=("plot_progression", "continuity"),
    pass_average=3.0,
    hard_failure_codes=BASE_HARD_FAILURES,
    contract_fields=(
        ContractField(
            name="setup_payoffs",
            schema_hint=(
                '[{"id":"S001","setup":"本章埋下的信息或手段","introduced_at":"scene_1",'
                '"payoff_at":"后续位置"}]'
            ),
            delta_slot="clue_updates",
            immutable_fields=("setup",),
        ),
        ContractField(
            name="world_facts",
            schema_hint='[{"id":"W001","fact":"本章确立的世界事实","scope":"适用范围","source":"来源"}]',
            delta_slot="evidence_updates",
            immutable_fields=("fact", "scope"),
        ),
    ),
    rules_model="作品自身的世界规则；设定、能力与限制以世界观为准，并在全书保持一致",
    baseline_rules=(
        "世界规则一经确立就不得随意更改",
        "人物的能力和资源在全书保持一致",
        "冲突的解决必须依赖已经出现的信息或手段",
        "重要变化必须落到具体人物的处境上",
    ),
    central_conflict_schema={
        "apparent_answer": "表面问题",
        "truth_answer": "根本矛盾",
        "moral_question": "人物必须付出的核心代价",
    },
    design_principles=BASE_DESIGN_PRINCIPLES,
    review_focus={
        "plan": "检查场景分工是否清楚，情节是否真正推进，关键手段是否提前出现。",
        "scene": "是否重复上一场动作、提前完成下一场任务、人物是否失去主动性。",
        "chapter": "检查整章是否围绕单一问题推进，结尾是否改变处境，代价是否真实。",
    },
    scene_writing_rules=(),
    max_scene_retries=1,
)


DOMAIN_PROFILES: Dict[str, DomainProfile] = {
    profile.key: profile
    for profile in (
        LEGAL_SUSPENSE,
        DETECTIVE_MYSTERY,
        THRILLER,
        HORROR,
        SCIFI,
        FANTASY,
        ROMANCE,
        HISTORICAL,
        WESTERN,
        GENERAL,
    )
}

DEFAULT_PROFILE_KEY = GENERAL.key


# --- 关键词分派 -----------------------------------------------------------

# 按顺序匹配，首次命中即返回。更专的题材排在更泛的题材之前：例如
# "Legal Thriller" 无论挂在 Mystery 还是 Thriller 主类型下，都应落到法律悬疑。
KEYWORD_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("legal_suspense", ("legal", "法律", "法庭", "courtroom", "律师", "诉讼", "庭审")),
    (
        "detective_mystery",
        (
            "mystery", "detective", "sleuth", "cozy", "forensic", "procedural", "whodunit",
            "推理", "侦探", "悬疑", "刑侦", "法医", "警察程序",
        ),
    ),
    ("thriller", ("thriller", "espionage", "spy", "techno", "medical", "惊悚", "间谍", "特工")),
    ("horror", ("horror", "gothic", "slasher", "cosmic", "恐怖", "哥特", "惊魂")),
    (
        "scifi",
        (
            "sci-fi", "scifi", "science fiction", "cyberpunk", "biopunk", "space opera",
            "post-apocalyptic", "time travel", "科幻", "赛博", "太空", "末日",
        ),
    ),
    (
        "fantasy",
        ("fantasy", "sorcery", "mythic", "fairy tale", "奇幻", "魔法", "剑与魔法", "神话", "童话"),
    ),
    ("romance", ("romance", "romantic", "regency", "爱情", "言情", "摄政")),
    (
        "historical",
        (
            "historical", "medieval", "renaissance", "colonial", "civil war", "ancient history",
            "world war", "历史", "中世纪", "文艺复兴", "殖民", "内战",
        ),
    ),
    ("western", ("western", "outlaw", "frontier", "cattle drive", "西部", "边疆", "亡命徒")),
)

# GUI 参数里可以直接指定档案，绕过关键词分派。
PROFILE_OVERRIDE_KEYS = ("Domain Profile", "domain_profile")


def get_domain_profile(key: str) -> DomainProfile:
    """按 key 取档案；未知 key 回落到通用档案。"""
    return DOMAIN_PROFILES.get(str(key or "").strip().lower(), GENERAL)


def match_profile_key(text: str) -> Optional[str]:
    """在一段文本里按关键词表找出第一个命中的档案 key。"""
    haystack = (text or "").lower()
    if not haystack.strip():
        return None
    for profile_key, keywords in KEYWORD_RULES:
        if any(keyword in haystack for keyword in keywords):
            return profile_key
    return None


def resolve_domain_profile(parameters: Optional[Dict[str, Any]]) -> DomainProfile:
    """按作品参数分派领域档案。

    优先级：显式覆盖 > 子类型关键词 > 主类型关键词 > 主题/基调关键词 > 通用。
    子类型先于主类型判断，"Legal Thriller" 这类跨类型子题材才能落到正确档案。
    """
    params = normalize_story_parameters(parameters)

    for override_key in PROFILE_OVERRIDE_KEYS:
        override = params.get(override_key)
        if override and str(override).strip().lower() in DOMAIN_PROFILES:
            return DOMAIN_PROFILES[str(override).strip().lower()]

    for source in (
        params.get("Subgenre"),
        params.get("Genre"),
        f"{params.get('Theme', '')} {params.get('Tone', '')}",
    ):
        matched = match_profile_key(str(source or ""))
        if matched:
            return DOMAIN_PROFILES[matched]
    return GENERAL


# --- 质量闭环档位 ---------------------------------------------------------

QUALITY_LOOP_OFF = "off"
QUALITY_LOOP_STANDARD = "standard"
QUALITY_LOOP_STRICT = "strict"
QUALITY_LOOP_MODES = (QUALITY_LOOP_OFF, QUALITY_LOOP_STANDARD, QUALITY_LOOP_STRICT)
DEFAULT_QUALITY_LOOP_MODE = QUALITY_LOOP_STANDARD

# 参数文件与 GUI 参数字典里的键名。
QUALITY_LOOP_KEYS = ("Quality Loop", "quality_loop")

# GUI 显示的中文档位与内部值的双向映射。
QUALITY_LOOP_LABELS = {
    QUALITY_LOOP_OFF: "关闭",
    QUALITY_LOOP_STANDARD: "标准",
    QUALITY_LOOP_STRICT: "严格",
}
QUALITY_LOOP_FROM_LABEL = {label: mode for mode, label in QUALITY_LOOP_LABELS.items()}

# 严格档在档案门槛上再加的分数。
STRICT_PASS_AVERAGE_BONUS = 0.2


def resolve_quality_loop_mode(parameters: Optional[Dict[str, Any]]) -> str:
    """从作品参数里取质量闭环档位，未设置时用标准档。

    接受内部值（off/standard/strict）和 GUI 中文标签（关闭/标准/严格）两种写法，
    参数文件与 GUI 参数字典的键名差异也一并容纳。
    """
    source = parameters or {}
    for key in QUALITY_LOOP_KEYS:
        raw = source.get(key)
        if raw in (None, ""):
            continue
        value = str(raw).strip()
        if value in QUALITY_LOOP_MODES:
            return value
        if value in QUALITY_LOOP_FROM_LABEL:
            return QUALITY_LOOP_FROM_LABEL[value]
        if value.lower() in QUALITY_LOOP_MODES:
            return value.lower()
    return DEFAULT_QUALITY_LOOP_MODE


def apply_quality_loop_mode(profile: DomainProfile, mode: str) -> DomainProfile:
    """按档位调整档案门槛。

    标准档使用档案自带的门槛与重试次数；严格档提高平均分门槛并把重试拉满。
    关闭档不改门槛——它在循环里直接跳过评审阶段，门槛无从生效。
    """
    if mode != QUALITY_LOOP_STRICT:
        return profile
    return replace(
        profile,
        pass_average=min(4.0, profile.pass_average + STRICT_PASS_AVERAGE_BONUS),
        max_plan_retries=max(profile.max_plan_retries, 2),
        max_scene_retries=max(profile.max_scene_retries, 2),
    )
