from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4


@dataclass(frozen=True)
class ReturnEvaluation:
    order_id: str
    item_id: str
    decision: Literal["standard_return", "exception_flow", "blocked"]
    policy_codes: tuple[str, ...]


@dataclass
class ReturnContext:
    """Application-owned return state for one conversation."""

    verified_order_id: str | None = None
    conversation_id: str = field(default_factory=lambda: str(uuid4()))
    current_evaluation: ReturnEvaluation | None = None
