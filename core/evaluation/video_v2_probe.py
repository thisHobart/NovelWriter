"""Small, explicit live text probe. No automatic human confirmation or media calls."""

import argparse
from pathlib import Path

from core.short_video.v2.conversation import ConversationService
from core.short_video.v2.models import ASSET_KINDS, default_config, encode
from core.short_video.v2.project_service import ProjectService
from core.short_video.v2.repository import ProjectRepository
from core.short_video.v2.workflow import WorkflowService

CASES = {
    "rural": (
        "井边铜钥匙",
        ["脑洞"],
        ["乡村"],
        [],
        "我回乡收拾父亲的老宅。院子里的井封了十年，井沿却放着一把干净的铜钥匙。我认出那是父亲挂在腰间的旧钥匙。"
        "邻居说，昨夜听见井里响了三声。我不信，蹲下摸了摸水泥井盖，里面真的传来三下轻敲。"
        "手机这时收到一段定时发送的录音，是父亲的声音：‘如果井里敲三下，别回答，也别拿钥匙。’我低头，钥匙已经握在手里。",
    ),
    "urban": (
        "被调换的报表",
        ["剧情"],
        ["都市", "职场"],
        [],
        "林雨加班到十一点，才发现第二天汇报用的报表被人换了。她拿出离线备份，两份数据只有客户签名不同。"
        "同事周宁说打印机坏了，劝她明早再处理。林雨没有争辩，把两份报表的签名拍在一起，发给了客户。"
        "客户立即回电：‘左边那份确实是我的签名，但我今天根本没去过你们公司。’她望向还亮着灯的会议室，周宁正在里面接电话。",
    ),
    "fantasy": (
        "碎剑认主",
        ["玄幻", "冒险"],
        ["异界"],
        [],
        "少年沈川替师父守着废弃剑阁。山门考核只剩最后一日，他的木剑又被试剑石震断。师父没有替他求情，只把一柄断剑交给他，让他天亮前还到后山。"
        "沈川走到废炉旁，断剑忽然发烫，炉壁上浮出师父年轻时的名字。身后传来守阁长老的声音：‘这把剑十年前就该销毁了。’"
        "他握紧断剑，发现剑柄里藏着一张尚未拆封的山门逐令。",
    ),
    "system": (
        "最后一次读档",
        ["脑洞"],
        ["都市"],
        ["系统", "重生"],
        "许晨再次醒在事故发生的清晨。系统显示只剩一次读档机会。他已经试过换路、提前报警，每次都在桥边失败。"
        "这回他没有出门，而是重听行车记录仪。撞击前的录音里，妹妹说的不是‘快走’，而是‘别关车窗’。"
        "他找到当天的天气截图，桥上无风。系统忽然弹出红字：本次选择将覆盖全部存档。妹妹的电话恰好响起。",
    ),
}


def main() -> None:
    from dotenv import load_dotenv

    from core.generation.api_providers import environment_file

    load_dotenv(environment_file())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--model", default="deepseek")
    parser.add_argument("--root", default="quality_runs/video_v2_20261102")
    parser.add_argument(
        "--stage",
        choices=["novel_copy", "video_copy", "asset_catalog", "storyboard", "asset_spec", "chat"],
        required=True,
    )
    parser.add_argument("--asset-index", type=int, default=0)
    args = parser.parse_args()
    repository = ProjectRepository(Path(args.root) / args.case)
    if not repository.current("source"):
        title, themes, backgrounds, settings, article = CASES[args.case]
        config = default_config()
        config.update(
            title=title, tracks={"themes": themes, "backgrounds": backgrounds, "settings": settings}
        )
        ProjectService(repository).import_text(article, config=config, expected_revision=0)
    if args.stage == "chat":
        service = ConversationService(repository, args.model)
        conversation = service.store.create("纯聊天实测")
        service.execute(
            service.prepare(
                conversation["id"],
                "请给出一个让故事开头更紧凑、结尾保留悬念的写作建议。",
            )
        )
        print(encode(service.store.get(conversation["id"])))
        return
    params = {}
    if args.stage == "asset_spec":
        entity = repository.current("asset_catalog")["content"]["entities"][args.asset_index]
        params = {
            "entity_id": entity["asset_id"],
            "variant_id": entity["variants"][0]["variant_id"],
            "kind": ASSET_KINDS[entity["kind"]],
        }
    service = WorkflowService(repository, args.model)
    result = service.execute(service.prepare(args.stage, **params))
    print(encode(repository.record(result["version_id"])))


if __name__ == "__main__":
    main()
