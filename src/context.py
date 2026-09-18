from dataclasses import dataclass


@dataclass
class ReturnContext:
    """Application-owned verification state for one conversation."""

    verified_order_id: str | None = None
