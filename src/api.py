from asyncio import Lock
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from agents import Runner, RunState
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.agent import ciderlane_agent
from src.context import ReturnContext


@dataclass
class Session:
    """Process-local HTTP session with application-owned return state."""

    context: ReturnContext = field(default_factory=ReturnContext)
    conversation: list[dict] = field(default_factory=list)
    paused_state: RunState[ReturnContext] | None = None
    lock: Lock = field(default_factory=Lock, repr=False)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, strict=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    yield


app = FastAPI(lifespan=lifespan)
sessions: dict[str, Session] = {}


@app.post("/sessions", status_code=201)
async def create_session() -> dict[str, str]:
    session_id = str(uuid4())
    sessions[session_id] = Session()
    return {"session_id": session_id}


@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, request: ChatRequest) -> dict[str, str]:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    async with session.lock:
        conversation = session.conversation + [{"role": "user", "content": request.message}]
        result = await Runner.run(ciderlane_agent, conversation, context=session.context)
        if result.interruptions:
            raise HTTPException(status_code=409, detail="HTTP approval is not available yet.")
        session.conversation = result.to_input_list()
        return {"status": "completed", "response": result.final_output}
