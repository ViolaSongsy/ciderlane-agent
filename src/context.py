from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class ReturnContext:
    """Application-owned identity and verification state for one conversation."""

    verified_order_id: str | None = None
    conversation_id: str = field(default_factory=lambda: str(uuid4()))