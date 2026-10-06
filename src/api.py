from asyncio import Lock
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from agents import Runner, RunResult, RunState
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from src.agent import ciderlane_agent
from src.context import ReturnContext
from src.main import approval_description


@dataclass
class Session:
    """Process-local HTTP session with application-owned return state."""

    context: ReturnContext = field(default_factory=ReturnContext)
    conversation: list[dict] = field(default_factory=list)
    paused_state: RunState[ReturnContext] | None = None
    approval_id: str | None = None
    lock: Lock = field(default_factory=Lock, repr=False)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, strict=True)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, strict=True)
    decision: Literal["approve", "reject"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    yield


app = FastAPI(lifespan=lifespan)
sessions: dict[str, Session] = {}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


def validated_approvals(state: RunState, context: ReturnContext):
    """Reject unsupported calls and describe returns from the verified order."""
    approvals = []
    for interruption in state.get_interruptions():
        if interruption.tool_name != "start_return":
            state.reject(interruption)
            continue
        description = approval_description(interruption, context)
        if description is None:
            state.reject(
                interruption,
                rejection_message=("The selected item could not be resolved in the "
                                   "verified order. No return or label was created."),
            )
            continue
        approvals.append((interruption, description))
    return approvals


async def finish_run(session: Session, result: RunResult) -> dict:
    while result.interruptions:
        state = result.to_state()
        approvals = validated_approvals(state, session.context)
        if approvals:
            session.paused_state = state
            session.approval_id = str(uuid4())
            return {
                "status": "approval_required",
                "approval_id": session.approval_id,
                "approvals": [description for _, description in approvals],
            }
        result = await Runner.run(ciderlane_agent, state)
    session.paused_state = None
    session.approval_id = None
    session.conversation = result.to_input_list()
    return {"status": "completed", "response": result.final_output}


@app.post("/sessions", status_code=201)
async def create_session() -> dict[str, str]:
    session_id = str(uuid4())
    sessions[session_id] = Session()
    return {"session_id": session_id}


@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, request: ChatRequest) -> dict:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    async with session.lock:
        if session.paused_state is not None:
            raise HTTPException(status_code=409, detail="Approve or reject the pending return first.")
        conversation = session.conversation + [{"role": "user", "content": request.message}]
        result = await Runner.run(ciderlane_agent, conversation, context=session.context)
        return await finish_run(session, result)


@app.post("/sessions/{session_id}/approval")
async def resolve_approval(session_id: str, request: ApprovalRequest) -> dict:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    async with session.lock:
        state = session.paused_state
        if state is None or request.approval_id != session.approval_id:
            raise HTTPException(status_code=409, detail="This approval is no longer pending.")
        # Consume before resuming so a failed run cannot replay this approval.
        session.paused_state = None
        session.approval_id = None
        for interruption, _ in validated_approvals(state, session.context):
            if request.decision == "approve":
                state.approve(interruption)
            else:
                state.reject(
                    interruption,
                    rejection_message=("The customer declined this return. Do not call "
                                       "start_return again for this request. Explain that "
                                       "no return or label was created."),
                )
        result = await Runner.run(ciderlane_agent, state)
        return await finish_run(session, result)
