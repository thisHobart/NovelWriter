"""Short-video writer: explicit snapshots in, whole JSON story out."""
from agents.base.agent import BaseAgent, AgentResult
from core.short_video.models import parse_object
from core.short_video.prompts import outline_prompt, script_prompt
from core.short_video.validation import validate_story


class ShortVideoScriptAgent(BaseAgent):
    def __init__(self, model=None, *, send):
        super().__init__("short-video-writer", model=model)
        self.send = send

    def get_available_tools(self):
        return ["outline", "script", "revise"]

    def get_required_fields(self):
        return ["action", "requirements"]

    def process_task(self, task_data):
        if not self.validate_input(task_data):
            raise ValueError("缺少脚本任务参数")
        action = task_data["action"]
        requirements = task_data["requirements"]
        if action == "outline":
            prompt = outline_prompt(requirements)
            kind = "outline"
        elif action in {"script", "revise"}:
            prompt = script_prompt(requirements, task_data["outline"],
                                   task_data.get("candidate"), task_data.get("asks"))
            kind = "script"
        else:
            raise ValueError("未知短视频写作动作")
        prompt += task_data.get("repair", "")
        content = validate_story(parse_object(self.send(prompt)), requirements, kind)
        return AgentResult(True, {"content": content}, ["生成完成"], {})
