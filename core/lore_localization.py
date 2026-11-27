"""设定界面的中文展示；不改变文件字段名或用户尚未修改的数据。"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from core.localization import zh_label

FIELD_LABELS = {
    "name": "名称", "character_name": "人物名称", "full_name": "全名", "faction_name": "势力名称",
    "role": "角色定位", "gender": "性别", "age": "年龄", "profession": "职业", "title": "身份 / 称号",
    "goal": "目标", "goals": "目标", "motivation": "动机", "motivations": "动机",
    "flaw": "弱点", "flaws": "弱点", "strength": "优势", "strengths": "优势", "arc": "成长方向",
    "background": "背景", "description": "描述", "summary": "摘要", "appearance_summary": "外貌",
    "backstory_summary": "背景摘要", "appearance": "外貌", "personality": "性格", "traits": "特征",
    "primary_traits": "主要特征", "faction": "所属势力", "opposes": "阻碍目标", "character": "目标人物",
    "blocked_goal": "被阻碍的目标", "nature": "性质", "type": "类型", "kind": "类别",
    "territory": "活动地点", "territories": "活动地点", "territory_kind": "地点类型",
    "conflicts": "势力冲突", "with": "关联势力", "over": "冲突原因", "resources": "资源",
    "allies": "盟友", "enemies": "敌对势力", "jurisdiction": "管辖范围", "funding_level": "资金水平",
    "reputation": "声誉", "ideology": "理念", "faction_profile": "势力概况", "influence": "影响力",
    "relationships": "人物关系", "character1": "人物一", "character2": "人物二", "relation": "亲属关系",
    "family": "家庭", "parents": "父母", "children": "子女", "siblings": "兄弟姐妹", "spouse": "配偶",
    "status": "状况", "characters": "人物", "factions": "势力", "locations": "地点",
    "era": "年代", "technology_level": "技术水平", "world_constraints": "世界规则",
    "canonical_characters": "人物名称登记", "canonical_factions": "势力名称登记",
    "canonical_locations": "地点名称登记", "aliases": "别名", "id": "标识", "schema_version": "格式版本",
    "origin": "来源", "metadata": "附加信息", "generation_date": "生成时间", "total_characters": "人物总数",
    "genre": "题材", "subgenre": "子题材", "notes": "备注", "abilities": "能力", "skills": "技能",
    "secrets": "秘密", "history": "历史", "leader": "领导者", "members": "成员", "location": "所在地",
}

VALUE_LABELS = {
    "protagonist": "主角", "deuteragonist": "第二主角", "antagonist": "反派", "supporting": "配角",
    "Male": "男", "Female": "女", "male": "男", "female": "女", "lore": "世界设定",
    "Father": "父亲", "Mother": "母亲", "child": "子女", "sibling": "兄弟姐妹", "spouse": "配偶",
    "Living": "在世", "Deceased": "已故", "Estranged": "关系疏远", "Unknown": "未知",
    "Police Department": "警察部门", "Criminal Organization": "犯罪组织",
    "Private Investigation Agency": "私人调查机构", "Partners": "搭档", "Former partners": "昔日搭档",
    "Suspect and detective": "嫌疑人与侦探", "Victim and perpetrator": "受害者与加害者",
    "Court Reporter": "法庭书记员", "Defense Attorney": "辩护律师", "Forensic Expert": "法医鉴证专家",
    "Police Officer": "警员", "Witness": "证人", "District Attorney": "地方检察官",
    "Investigator": "调查员", "Paralegal": "律师助理", "Private Investigator": "私家侦探",
    "Prosecutor": "检察官", "Federal": "联邦级", "International": "国际范围", "Regional": "地区级",
    "Local": "地方级", "National": "全国范围", "Heavily funded": "资金充裕", "Respected": "受人尊重",
    "Build reputation for excellence": "凭借出色表现建立声誉",
    "Control lucrative criminal markets": "控制利润丰厚的非法市场",
    "Escape from prison": "逃离监狱", "Expand criminal territory and influence": "扩大犯罪地盘与影响力",
    "Find an alibi witness": "寻找不在场证明的证人", "Help solve the case": "协助破案",
    "Maintain client confidentiality": "保护委托人的隐私", "Maintain secrecy and avoid detection": "隐藏行踪，避免暴露",
    "Prevent crime through community policing": "通过社区警务预防犯罪",
    "Protect citizens and maintain public safety": "保护市民，维护公共安全",
    "Solve complex cases for clients": "为委托人侦破复杂案件",
    "Solve crimes and bring perpetrators to justice": "侦破案件，将犯罪者绳之以法",
    "Solve their partner's murder": "查明搭档遇害的真相", "Steal a priceless artifact": "盗取价值连城的文物",
    "Fighting corruption": "打击腐败", "Protecting loved ones": "保护亲人",
    "Seeking closure": "寻求释怀", "Seeking justice for the innocent": "为无辜者伸张正义",
    "Arrogant and condescending": "傲慢自负，居高临下", "Drinks too much": "酗酒",
    "Has anger management issues": "难以控制愤怒", "Prone to making assumptions": "容易先入为主",
    "Prone to taking unnecessary risks": "容易冒不必要的风险",
    "Excellent deductive reasoning": "擅长演绎推理", "Excellent research skills": "调查研究能力出色",
    "Skilled in combat": "擅长格斗", "Skilled negotiator": "擅长谈判",
    "Discovering their true calling": "找到真正的人生使命",
    "Finding the courage to do what's right": "鼓起勇气做正确的事",
    "Learning to see the bigger picture": "学会从全局看问题", "Overcoming past trauma": "走出过去的创伤",
    "Comes from a family of law enforcement": "出身执法人员家庭", "Former criminal turned good": "曾经犯罪，后来改过自新",
    "Former undercover operative": "曾是卧底人员", "Has a background in psychology": "具备心理学背景",
    "A reputable agency known for solving difficult mysteries": "一家以侦破疑难案件闻名、声誉良好的调查机构",
    "A sophisticated criminal enterprise with multiple revenue streams": "一个组织严密、拥有多种收入来源的犯罪集团",
    "A well-equipped police department with modern forensic capabilities": "一个装备完善、具备现代鉴证能力的警察部门",
    "Family ties complicate their relationship": "亲属纽带使他们的关系更加复杂",
    "Their friendship is tested by the case": "这起案件考验着他们的友谊",
    "Their past partnership ended badly": "他们过去的合作不欢而散",
    "They are working against each other": "他们彼此对抗", "They share information and resources": "他们共享信息和资源",
    "They support each other's goals": "他们支持彼此的目标",
    "Client database and case files": "委托人资料库与案件档案", "Corrupt officials and connections": "腐败官员及其关系网",
    "Criminal databases and investigation tools": "犯罪资料库与调查工具",
    "Forensic laboratories and crime scene units": "法证实验室与现场勘查队伍",
    "High-tech surveillance equipment": "高科技监控设备", "Intimidation and enforcement capabilities": "威吓与强制执行能力",
    "Legal expertise and court connections": "法律专业知识与法院人脉",
    "Network of contacts and informants": "人脉与线人网络", "Network of informants and contacts": "线人与人脉网络",
    "Patrol officers and emergency response teams": "巡逻警员与应急响应队伍",
    "Safe houses and hidden facilities": "安全屋与隐蔽设施", "Training facilities and equipment": "训练设施与装备",
    "Community leaders": "社区领袖", "Corporate clients": "企业客户", "Criminal lawyers": "刑事律师",
    "Federal agencies": "联邦机构", "Forensic laboratories": "法证实验室", "Insurance companies": "保险公司",
    "Legal professionals": "法律专业人士", "Money launderers": "洗钱人员", "Other police departments": "其他警察部门",
    "Underground networks": "地下关系网", "Corrupt officials": "腐败官员", "Drug cartels": "贩毒集团",
    "Rival criminal groups": "敌对犯罪团伙", "Uncooperative witnesses": "不配合调查的证人",
}


def field_label(key, index=None):
    if isinstance(key, int):
        return f"第 {key + 1} 项"
    if key in FIELD_LABELS:
        return FIELD_LABELS[key]
    if re.search(r"[\u4e00-\u9fff]", str(key)):
        return str(key)
    return "扩展属性" + (f" {index + 1}" if index is not None else "")


def display_value(value):
    if value is None:
        return "未设置"
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value)
    return VALUE_LABELS.get(text, zh_label(text))


_CHAPTER = re.compile(r"(?:^|[_\-\s])(?:chapter|ch)[_\-\s]*(\d+)(?=$|[_\-\s])", re.I)


def _later_stage_label(relative, name):
    """故事结构、场景规划、章节正文的文件名，用各页面上的叫法；认不出的返回 None。"""
    stem = PurePosixPath(name).stem
    chapter = _CHAPTER.search(stem)
    if relative.startswith("story/structure/"):
        if name == "structure_contract.json":
            return "故事结构 · 结构登记表"
        from core.localization import structure_file_label
        return structure_file_label(relative)
    if relative.startswith("story/planning/"):
        if name == "reconciled_locations_arcs.md":
            return "地点与故事弧"
        if stem.startswith("chapter_outlines_"):
            from core.localization import structure_file_label
            return structure_file_label(relative)
        if stem.startswith("scenes_") or "/detailed_scene_plans/" in relative:
            return f"第 {int(chapter.group(1))} 章场景" if chapter else None
    if relative.startswith("story/content/") and chapter:
        return f"第 {int(chapter.group(1))} 章正文"
    return None


def file_label(relative):
    relative = str(relative).replace("\\", "/")
    name = PurePosixPath(relative).name
    known = {"generated_lore.md": "世界观正文", "characters.json": "人物资料", "factions.json": "势力资料",
             "lore_contract.json": "名称与规则登记表", "suggested_titles.md": "候选标题"}
    if name in known:
        return known[name]
    if name.startswith("background_") and name.endswith(".md"):
        detail = name[len("background_"):-3]
        for role in ("protagonist", "deuteragonist", "antagonist", "supporting"):
            if detail.startswith(role + "_"):
                return f"人物背景 · {display_value(role)} · {detail[len(role) + 1:]}"
        return "人物背景 · " + detail
    return _later_stage_label(relative, name) or relative


def describe_value(value, level=0):
    """预览用中文条目，不向作者展示原始字段名与序列化符号。"""
    lines = []
    prefix = "  " * level
    if isinstance(value, dict):
        for index, (key, item) in enumerate(value.items()):
            label = field_label(key, index)
            if isinstance(item, (dict, list)):
                lines.append(prefix + label + "：")
                lines.append(describe_value(item, level + 1))
            else:
                lines.append(prefix + label + "：" + display_value(item))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                lines.append(prefix + f"第 {index + 1} 项：\n" + describe_value(item, level + 1))
            else:
                lines.append(prefix + "• " + display_value(item))
        if not value:
            lines.append(prefix + "（暂无）")
    else:
        lines.append(prefix + display_value(value))
    return "\n".join(lines)


def display_message(message):
    text = str(message)
    text = re.sub(r"story/(?:lore|structure|planning|content)/[^\s：，]+\.(?:md|json)",
                  lambda m: file_label(m.group()), text)
    text = re.sub(r"\b[A-Za-z_]+\b", lambda m: FIELD_LABELS.get(m.group(), VALUE_LABELS.get(m.group(), m.group())), text)
    text = re.sub(r"(?<!\d)\[(\d+)\]", lambda m: f"第 {int(m.group(1)) + 1} 项", text)
    return text.replace("JSON", "数据").replace(" ID ", "标识")
