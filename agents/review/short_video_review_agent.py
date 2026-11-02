"""Independent whole-script review; evidence must be present in the script."""
from agents.base.agent import BaseAgent, AgentResult
from core.short_video.models import parse_object
from core.short_video.prompts import review_prompt
from core.short_video.validation import validate_review


class ShortVideoReviewAgent(BaseAgent):
    def __init__(self, model=None, *, send):
        super().__init__("short-video-reviewer", model=model)
        self.send = send

    def get_available_tools(self):
        return ["review"]

    def get_required_fields(self):
        return ["requirements", "outline", "script"]

    def process_task(self, task_data):
        if not self.validate_input(task_data):
            raise ValueError("缺少评审输入")
        raw = self.send(review_prompt(task_data["requirements"], task_data["outline"], task_data["script"]))
        review = validate_review(parse_object(raw), task_data["script"])
        return AgentResult(True, {"review": review}, ["评审完成"], {})
