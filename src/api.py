from dataclasses import dataclass, field
from uuid import uuid4

from agents import RunState
from fastapi import FastAPI

from src.context import ReturnContext


@dataclass
class Session:
    """Process-local HTTP session with application-owned return state."""

    context: ReturnContext = field(default_factory=ReturnContext)
    conversation: list[dict] = field(default_factory=list)
    paused_state: RunState[ReturnContext] | None = None


app = FastAPI()
sessions: dict[str, Session] = {}


@app.post("/sessions", status_code=201)
async def create_session() -> dict[str, str]:
    session_id = str(uuid4())
    sessions[session_id] = Session()
    return {"session_id": session_id}
