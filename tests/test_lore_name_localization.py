"""设定阶段必须把一份中文的人物与势力名单交给下游。

这几个用例原来跑的是「英文词库生成 + 事后改名」那条路，检验的是改名有没有漏掉
字段。现在卡片由模型直接用中文写，检验目标不变，但多了一件事：落盘前的确定性
校验必须真的拦住不合格的卡，而不是把它写进 characters.json。

这里仍然跑真实的 `_generate_factions` / `_generate_characters`，只把模型调用换成
桩——它们存在的理由是那种「导入和编译都通过、按下按钮才炸」的错误。
"""

import json

import pytest

from core.generation import lore_pipeline as lore_module
from core.generation.lore_pipeline import LorePipeline as Lore
from core.generation.lore_cast import has_latin


FACTIONS = [
    {
        "name": "海陵市公安局刑事侦查支队",
        "nature": "公权力机关",
        "type": "市级刑侦机构",
        "description": "负责海陵全市重大刑事案件的侦查。",
        "goal": "查清宏升物流的跨境转运链条",
        "resources": ["物证鉴定中心", "港区监控调阅权限"],
        "territory": {"name": "海陵老城区", "kind": "城区"},
        "conflicts": [{"with": "海陵宏升物流", "over": "对港区货运的管辖"}],
    },
    {
        "name": "海陵宏升物流",
        "nature": "商业机构",
        "type": "跨境货运企业",
        "description": "表面是港区最大的货代，实际替灰色资本转运违禁品。",
        "goal": "在专案组收网前把核心账本运出境",
        "resources": ["自有船队", "海关内线"],
        "territory": {"name": "北岸三号码头", "kind": "码头"},
        "conflicts": [{"with": "海陵市公安局刑事侦查支队", "over": "账本的去向"}],
    },
]

CHARACTERS = [
    {
        "name": "梁浩", "role": "protagonist", "gender": "男", "age": 44,
        "faction": "海陵市公安局刑事侦查支队", "profession": "刑警", "title": "重案队副队长",
        "goal": "找到能推翻伪造不在场证明的人证", "motivation": "他答应过死者的女儿",
        "flaw": "情绪失控时会动手", "strength": "能从废弃档案里还原时间线",
        "arc": "从独断专行到肯把证据交给别人核",
        "background": "服过刑，因熟悉黑产被特招进刑侦体系。",
        "description": "双鬓微霜，眼里布满血丝。",
    },
    {
        "name": "顾阳波", "role": "deuteragonist", "gender": "男", "age": 33,
        "faction": "海陵市公安局刑事侦查支队", "profession": "法医", "title": "物证鉴定中心主检",
        "goal": "证明阻断剂造成的假死可以被检出", "motivation": "他签过一份错误的鉴定书",
        "flaw": "只信自己复核过的数据", "strength": "微量物证溯源",
        "arc": "从回避责任到当庭认错",
        "background": "做过卧底，转岗后一直在补方法学的课。",
        "description": "个子不高，说话前习惯先停半秒。",
    },
    {
        "name": "郑娜敏", "role": "antagonist", "gender": "女", "age": 35,
        "faction": "海陵宏升物流", "profession": "辩护律师", "title": "外聘法律顾问",
        "goal": "让宏升的核心账本永远进不了法庭", "motivation": "她父亲的案子毁在同一套程序上",
        "flaw": "把规则当成唯一的正义", "strength": "程序瑕疵的嗅觉",
        "arc": "从确信程序至上到亲手交出证据",
        "background": "出身警察世家，父亲因程序违法被追责后转投辩护席。",
        "description": "永远穿深色套装，语速极慢。",
        "opposes": {"character": "梁浩", "blocked_goal": "找到能推翻伪造不在场证明的人证"},
    },
]

RELATIONSHIPS = [
    {"character1": "梁浩", "character2": "顾阳波",
     "type": "搭档", "description": "同一支队的老搭档，为那份错误鉴定书吵过一次。"},
    {"character1": "梁浩", "character2": "郑娜敏",
     "type": "法庭上的对手", "description": "梁浩递上去的每一份证据都被她挑出程序瑕疵。"},
]


class _Logger:
    def info(self, *args, **kwargs):
        pass

    debug = warning = info

    def error(self, *args, **kwargs):
        raise AssertionError(f"lore stage logged an error: {args}")


class _App:
    def __init__(self, output_dir):
        self.output_dir = str(output_dir)
        self.logger = _Logger()


class _UI(dict):
    def __init__(self, output_dir, **values):
        super().__init__(values)
        self.output_dir = str(output_dir)
        self.model = "hosted-llm"
        self.parameters = {
            "genre": "Mystery",
            "subgenre": "Legal Thriller",
            "Genre": "Mystery",
            "Subgenre": "Legal Thriller",
            "Novel Title": "镜中囚徒",
            "Theme": "程序正义与实体正义的撕扯",
            "Tone": "克制、冷硬",
            "female_percentage": 33,
            "male_percentage": 67,
        }


@pytest.fixture
def planner(tmp_path, monkeypatch):
    """A Lore panel wired to a temp project, with dialogs turned into failures."""
    from core.config.directory_config import get_directory_manager

    panel = object.__new__(Lore)
    panel.app = _App(tmp_path)
    panel.dir_manager = get_directory_manager(str(tmp_path), use_new_structure=True)

    def fail(title, message):
        raise AssertionError(f"{title}: {message}")

    monkeypatch.setattr(lore_module, "show_error", fail)
    monkeypatch.setattr(lore_module, "show_warning", fail, raising=False)
    return panel


def _stub_model(monkeypatch, replies):
    """把设定阶段的模型调用换成一串写好的回复。"""
    sent = []

    def send_prompt(prompt, model=None, **kwargs):
        sent.append(prompt)
        return replies[min(len(sent) - 1, len(replies) - 1)]

    monkeypatch.setattr(lore_module, "send_prompt", send_prompt)
    return sent


def _names(payload):
    """Every name-ish string in a saved lore file."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"name", "faction_name", "territory", "headquarters"} and isinstance(value, str):
                    found.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def test_generating_factions_saves_chinese_names(planner, tmp_path, monkeypatch):
    _stub_model(monkeypatch, [json.dumps({"factions": FACTIONS}, ensure_ascii=False)])
    planner._generate_factions(_UI(tmp_path, num_factions=2))

    saved = json.loads((tmp_path / "story" / "lore" / "factions.json").read_text("utf-8"))
    names = _names(saved)

    assert names, "no faction names were saved"
    assert not [name for name in names if has_latin(name)], names


def test_saved_faction_territory_is_a_plain_place_name(planner, tmp_path, monkeypatch):
    """territory 校验时是对象，落盘时摊回字符串——下游一直按字符串读。"""
    _stub_model(monkeypatch, [json.dumps({"factions": FACTIONS}, ensure_ascii=False)])
    planner._generate_factions(_UI(tmp_path, num_factions=2))

    saved = json.loads((tmp_path / "story" / "lore" / "factions.json").read_text("utf-8"))
    assert saved[0]["territory"] == "海陵老城区"
    assert saved[0]["territory_kind"] == "城区"


def test_generating_characters_saves_chinese_names(planner, tmp_path, monkeypatch):
    _stub_model(
        monkeypatch,
        [json.dumps({"characters": CHARACTERS, "relationships": RELATIONSHIPS},
                    ensure_ascii=False)],
    )
    planner._generate_characters(_UI(tmp_path, num_chars=3))

    saved = json.loads((tmp_path / "story" / "lore" / "characters.json").read_text("utf-8"))
    names = _names(saved)

    assert names, "no character names were saved"
    assert not [name for name in names if has_latin(name)], names
    assert saved["metadata"]["total_characters"] == 3
    assert len(saved["relationships"]) == 2


def test_characters_are_told_which_factions_exist(planner, tmp_path, monkeypatch):
    """人物要落在已经存在的势力里，所以势力名单必须进人物提示词。"""
    _stub_model(monkeypatch, [json.dumps({"factions": FACTIONS}, ensure_ascii=False)])
    planner._generate_factions(_UI(tmp_path, num_factions=2))

    sent = _stub_model(
        monkeypatch,
        [json.dumps({"characters": CHARACTERS, "relationships": RELATIONSHIPS},
                    ensure_ascii=False)],
    )
    planner._generate_characters(_UI(tmp_path, num_chars=3))

    assert "海陵市公安局刑事侦查支队" in sent[0]
    assert "海陵宏升物流" in sent[0]


def test_a_faction_card_that_contradicts_itself_never_reaches_disk(
    planner, tmp_path, monkeypatch
):
    """真实数据里出现过「名字是民营咨询公司、类型写着 Police Department」。

    这种卡必须在落盘前被拦下，并把失败原文当成修复指令发回去。
    """
    broken = json.loads(json.dumps(FACTIONS))
    broken[0]["name"] = "海陵岸线风险咨询"  # nature 仍是公权力机关
    sent = _stub_model(
        monkeypatch,
        [
            json.dumps({"factions": broken}, ensure_ascii=False),
            json.dumps({"factions": FACTIONS}, ensure_ascii=False),
        ],
    )
    planner._generate_factions(_UI(tmp_path, num_factions=2))

    saved = json.loads((tmp_path / "story" / "lore" / "factions.json").read_text("utf-8"))
    assert saved[0]["name"] == "海陵市公安局刑事侦查支队"
    assert len(sent) == 2
    assert "咨询" in sent[1], "重试提示词必须带上失败原文"
