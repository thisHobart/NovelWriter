"""Rank visible turns by exact query phrases."""

import re

QUESTION_SEPARATORS = re.compile(
    r"[\W_]+|那一轮|那一版|那一次|那个|那次|当时|最后|最终|实际|原来|以前|已经|"
    r"有没有|怎么|怎样|如何|为什么|哪里|哪一镜|哪一次|哪次|哪一句|哪个|什么|"
    r"改成|改了|改过|应用|确认|之后|以后|之前|"
    r"的时候|的|了|把|给|时|和|与|是|吗|呢|就|再|才|先|从|到|在|为"
)


def query_phrases(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        phrase for phrase in QUESTION_SEPARATORS.split(question.lower()) if len(phrase) >= 2
    ))

METHOD = "revision-keyword"
SETTINGS = {"phrase_min_chars": 2, "phrase_score": "character_count", "tie_break": "newest_first", "candidate_state": "revisions_only"}



def rank_turns(
    question: str, turns: dict[str, str]
) -> list[tuple[str, float | None, list[str]]]:
    phrases = query_phrases(question)
    matches = []
    for turn_id, text in turns.items():
        text = "".join(text.lower().split())
        exact = [phrase for phrase in phrases if phrase in text]
        score = sum(map(len, exact))
        if score:
            matches.append((turn_id, float(score), ["词组 " + item for item in exact]))

    # Keep recency as the tie-breaker in both keyword experiments.
    return sorted(matches, key=lambda row: (-row[1], -int(row[0][1:])))

def rank_questions(questions: list[str], turns: dict[str, str], revisions: dict) -> list[list]:
    cards = {turn_id: revision.text for turn_id, revision in revisions.items()}
    return [rank_turns(question, cards) for question in questions]
