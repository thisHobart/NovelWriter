"""NovelWriter 的简体中文本地化辅助函数。

业务层继续使用既有英文标识，以兼容旧项目文件、类型配置和工作流；
界面与 Prompt 通过本模块转换为中文显示。
"""

from __future__ import annotations


ZH_LABELS = {
    # 类型
    "Sci-Fi": "科幻",
    "Fantasy": "奇幻",
    "Horror": "恐怖",
    "Mystery": "悬疑推理",
    "Romance": "爱情",
    "Thriller": "惊悚",
    "Western": "西部",
    "Historical Fiction": "历史小说",
    # 篇幅
    "Short Story": "短篇小说",
    "Novella": "中篇小说",
    "Novel (Standard)": "长篇小说（标准）",
    "Novel (Epic)": "长篇小说（史诗）",
    # 结构
    "3-Act Structure": "三幕式结构",
    "6-Act Structure": "六幕式结构",
    "Fichtean Curve": "菲希特曲线",
    "Freytag's Pyramid": "弗莱塔格金字塔",
    "Seven-Point Structure": "七点故事结构",
    "Hero's Journey": "英雄之旅",
    "Hero's Journey (Simplified)": "英雄之旅（简化版）",
    "Save the Cat!": "救猫咪结构",
    "Episodic Structure": "单元剧结构",
    # 常见结构阶段
    "Act 1: Setup": "第一幕：铺垫",
    "Act 2: Confrontation": "第二幕：对抗",
    "Act 3: Resolution": "第三幕：结局",
    "Beginning": "开端",
    "Rising Action": "情节上升",
    "First Climax": "第一次高潮",
    "Solution Finding": "寻找解决方案",
    "Second Climax": "第二次高潮",
    "Resolution": "结局",
    "Inciting Incident": "诱发事件",
    "Climax": "高潮",
    "Falling Action": "情节回落",
    "Denouement": "收尾",
    "Hook": "钩子",
    "Plot Point 1": "情节点一",
    "Pinch Point 1": "压力点一",
    "Midpoint": "中点",
    "Pinch Point 2": "压力点二",
    "Plot Point 2": "情节点二",
    "Departure": "启程",
    "Initiation": "启蒙",
    "Return": "归来",
    "The Ordinary World": "平凡世界",
    "The Call to Adventure": "冒险召唤",
    "Refusal of the Call": "拒绝召唤",
    "Meeting the Mentor": "遇见导师",
    "Crossing the Threshold": "跨越门槛",
    "Tests, Allies, and Enemies": "考验、盟友与敌人",
    "Approach to the Inmost Cave": "逼近洞穴深处",
    "The Ordeal": "严峻考验",
    "Reward (Seizing the Sword)": "获得奖赏（夺取宝剑）",
    "The Road Back": "归途",
    "The Resurrection": "复活",
    "Return with the Elixir": "携万能药归来",
    "Opening Image": "开场画面",
    "Theme Stated": "主题呈现",
    "Set-up": "铺垫",
    "Catalyst": "催化事件",
    "Debate": "争论",
    "Break into Two": "进入第二幕",
    "B Story": "B 故事",
    "Fun and Games": "游戏时间",
    "Bad Guys Close In": "坏人逼近",
    "All Is Lost": "一无所有",
    "Dark Night of the Soul": "灵魂黑夜",
    "Break into Three": "进入第三幕",
    "Finale": "终场",
    "Final Image": "终场画面",
    "Episode 1: Introduction": "第一单元：引入",
    "Episode 2: Rising Action": "第二单元：情节上升",
    "Episode 3: Midpoint/Turning Point": "第三单元：中点／转折点",
    "Episode 4: Climax Actions": "第四单元：高潮行动",
    "Episode 5: Resolution/Lead to Next": "第五单元：结局／引向下一单元",
    "Complete Short Story": "完整短篇小说",
    # 人物角色
    "Protagonist": "主角",
    "Deuteragonist": "第二主角",
    "Antagonist": "反派",
    "Unknown Character": "未知人物",
    "Unknown Role": "未知角色",
    # 动态参数名称
    "Protagonist Type": "主角类型",
    "Conflict Scale": "冲突规模",
}


SUBGENRE_ZH_LABELS = {
    "Space Opera": "太空歌剧",
    "Hard Sci-Fi": "硬科幻",
    "Cyberpunk": "赛博朋克",
    "Time Travel": "时间旅行",
    "Post-Apocalyptic": "后启示录",
    "Biopunk": "生物朋克",
    "High Fantasy": "史诗奇幻",
    "Dark Fantasy": "黑暗奇幻",
    "Urban Fantasy": "都市奇幻",
    "Sword and Sorcery": "剑与魔法",
    "Mythic Fantasy": "神话奇幻",
    "Fairy Tale": "童话",
    "Gothic Horror": "哥特恐怖",
    "Psychological Horror": "心理恐怖",
    "Supernatural Horror": "超自然恐怖",
    "Body Horror": "身体恐怖",
    "Cosmic Horror": "宇宙恐怖",
    "Slasher": "砍杀恐怖",
    "Cozy Mystery": "温馨推理",
    "Hard-boiled Detective": "硬汉侦探",
    "Police Procedural": "警察程序",
    "Amateur Sleuth": "业余侦探",
    "Legal Thriller": "法律惊悚",
    "Forensic Mystery": "法医推理",
    "Contemporary Romance": "当代爱情",
    "Historical Romance": "历史爱情",
    "Paranormal Romance": "超自然爱情",
    "Romantic Suspense": "爱情悬疑",
    "Regency Romance": "摄政时期爱情",
    "Western Romance": "西部爱情",
    "Espionage Thriller": "谍战惊悚",
    "Psychological Thriller": "心理惊悚",
    "Action Thriller": "动作惊悚",
    "Techno-Thriller": "科技惊悚",
    "Medical Thriller": "医疗惊悚",
    "Traditional Western": "传统西部",
    "Weird Western": "诡异西部",
    "Space Western": "太空西部",
    "Modern Western": "现代西部",
    "Outlaw Western": "亡命徒西部",
    "Cattle Drive Western": "赶牛西部",
    "Ancient History": "古代历史",
    "Medieval": "中世纪",
    "Renaissance": "文艺复兴",
    "Colonial America": "美洲殖民时期",
    "Civil War Era": "美国内战时期",
    "World War Era": "世界大战时期",
}

ZH_LABELS.update(SUBGENRE_ZH_LABELS)


GENDER_BIAS_ZH_LABELS = {
    "Balanced (50F/50M)": "均衡（女性50% / 男性50%）",
    "Slightly Female (60F/40M)": "略偏女性（女性60% / 男性40%）",
    "Mostly Female (75F/25M)": "多数女性（女性75% / 男性25%）",
    "Primarily Female (90F/10M)": "主要为女性（女性90% / 男性10%）",
    "Slightly Male (40F/60M)": "略偏男性（女性40% / 男性60%）",
    "Mostly Male (25F/75M)": "多数男性（女性25% / 男性75%）",
    "Primarily Male (10F/90M)": "主要为男性（女性10% / 男性90%）",
    "Exclusively Female (100F/0M)": "全部女性（女性100%）",
    "Exclusively Male (0F/100M)": "全部男性（男性100%）",
}


FIELD_ZH_LABELS = {
    "age": "年龄",
    "gender": "性别",
    "title": "头衔",
    "occupation": "职业",
    "faction": "所属势力",
    "faction_role": "势力职位",
    # 单复数两种拼写都要有：模型生成的人物卡用单数 goal/flaw/strength，
    # 旧项目的 characters.json 用复数数组。缺一种就会把英文键名原样印出来。
    "goals": "目标",
    "goal": "目标",
    "motivations": "动机",
    "motivation": "动机",
    "flaws": "缺点",
    "flaw": "缺点",
    "strengths": "优点",
    "strength": "优点",
    "profession": "职业",
    "background": "背景",
    "description": "外貌",
    "arc": "人物弧光",
    "family": "家庭",
    "parents": "父母",
    "siblings": "兄弟姐妹",
    "spouse": "配偶",
    "children": "子女",
}


def zh_label(value: str) -> str:
    """返回业务标识的中文显示名称；未知值保持原样。"""
    return ZH_LABELS.get(value, value)


def internal_label(display_value: str, mapping: dict[str, str] | None = None) -> str:
    """把中文显示名称还原为内部英文标识。"""
    labels = mapping or ZH_LABELS
    reverse = {translated: internal for internal, translated in labels.items()}
    return reverse.get(display_value, display_value)


def zh_field(field_name: str) -> str:
    """返回数据字段的中文名称。"""
    return FIELD_ZH_LABELS.get(field_name, field_name.replace("_", " "))
