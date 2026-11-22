"""The recent-history ranking used as the experiment control."""

METHOD = "recent-3"
SETTINGS = {"order": "newest_first"}


def rank_turns(
    question: str, turns: dict[str, str]
) -> list[tuple[str, float | None, list[str]]]:
    """Return (turn ID, score if applicable, matching features) in priority order."""
    return [(turn_id, None, []) for turn_id in reversed(turns)]

def rank_questions(questions: list[str], turns: dict[str, str], revisions: dict) -> list[list]:
    return [rank_turns(question, turns) for question in questions]
