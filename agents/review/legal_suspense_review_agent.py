"""法律悬疑评审代理的兼容入口。

实现已经泛化到 `agents/review/domain_review_agent.py`，评分维度、硬失败代码和
契约字段改由 `core/generation/domain_profiles.py` 的领域档案提供。本模块保留
原有的导入名，使既有调用方和测试无需改动。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from core.generation.ai_helper import send_prompt
from core.generation.domain_profiles import LEGAL_SUSPENSE

from agents.review.domain_review_agent import (  # noqa: F401  back-compat re-exports
    UNIVERSAL_CONTRACT_LISTS,
    UNIVERSAL_CONTRACT_TEXTS,
    DomainReview,
    DomainReviewAgent,
    DomainReviewError,
    extract_json_object,
)


SCORE_DIMENSIONS = LEGAL_SUSPENSE.score_dimensions
HARD_FAILURE_CODES = set(LEGAL_SUSPENSE.hard_failure_codes)


class LegalSuspenseReviewAgent(DomainReviewAgent):
    """DomainReviewAgent 预先绑定法律悬疑档案。"""

    def __init__(
        self,
        model: str,
        logger: Optional[logging.Logger] = None,
        send_prompt_fn: Callable[..., str] = send_prompt,
    ):
        super().__init__(
            model=model,
            profile=LEGAL_SUSPENSE,
            logger=logger or logging.getLogger("legal-suspense-review"),
            send_prompt_fn=send_prompt_fn,
        )
