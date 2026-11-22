"""FastAPI server for the NFS aviation agent: chat UI, chat API, eval API."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import MCP_URL, MODEL, _token, explain_notam, init_tracing, run_agent
from dashboard import dashboard_for_tool_calls, default_dashboard, startup_briefing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nfs-agent")

init_tracing()
app = FastAPI(title="NFS Aviation Agent")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=40)


class DashboardToolCall(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    arguments: str | dict[str, Any] | None = None


class DashboardViewRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    tool_calls: list[DashboardToolCall] = Field(max_length=3)


class NotamExplanationRequest(BaseModel):
    notam_id: str = Field(min_length=1, max_length=80)
    location: str | None = Field(default=None, max_length=80)
    q_code: str | None = Field(default=None, max_length=40)
    text: str = Field(min_length=1, max_length=8000)
    status: str | None = Field(default=None, max_length=160)
    altitude_band: str | None = Field(default=None, max_length=160)
    effective_end: str | None = Field(default=None, max_length=80)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": MODEL,
        "mcp_url": MCP_URL,
        "experiment_id": os.getenv("MLFLOW_EXPERIMENT_ID"),
    }


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    try:
        reply = await run_agent([m.model_dump() for m in req.messages])
    except Exception as exc:  # noqa: BLE001 - surface the cause to the UI
        logger.exception("agent run failed")
        raise HTTPException(status_code=502, detail=f"Agent error: {exc}") from exc
    return reply.to_dict()


_notam_explanation_cache: dict[tuple[str, str], str] = {}


@app.post("/api/notam/explain")
async def notam_explanation(req: NotamExplanationRequest) -> dict[str, str]:
    key = (req.notam_id, req.text)
    if key not in _notam_explanation_cache:
        try:
            explanation = await explain_notam(req.model_dump(exclude_none=True))
        except Exception as exc:  # noqa: BLE001 - preserve the raw NOTAM fallback
            logger.exception("NOTAM explanation failed")
            raise HTTPException(
                status_code=502, detail=f"NOTAM explanation error: {exc}"
            ) from exc
        if len(_notam_explanation_cache) >= 256:
            _notam_explanation_cache.pop(next(iter(_notam_explanation_cache)))
        _notam_explanation_cache[key] = explanation
    return {"plain_language": _notam_explanation_cache[key], "model": MODEL}


# ------------------------------------------------------------- dashboard

_dashboard_cache: dict[str, Any] = {"loaded_at": 0.0, "view": None}
_dashboard_lock = asyncio.Lock()


@app.get("/api/dashboard/briefing")
async def dashboard_briefing() -> dict[str, Any]:
    try:
        return await startup_briefing(MCP_URL, _token())
    except Exception as exc:  # noqa: BLE001 - preserve the chat on failure
        logger.exception("startup briefing failed")
        raise HTTPException(
            status_code=502, detail=f"Startup briefing error: {exc}"
        ) from exc


@app.get("/api/dashboard")
async def dashboard(refresh: bool = False) -> dict[str, Any]:
    async with _dashboard_lock:
        age = time.time() - float(_dashboard_cache["loaded_at"])
        if not refresh and _dashboard_cache["view"] is not None and age < 60:
            return _dashboard_cache["view"]
        try:
            view = await default_dashboard(MCP_URL, _token())
        except Exception as exc:  # noqa: BLE001 - dashboard may load partially
            logger.exception("dashboard refresh failed")
            raise HTTPException(
                status_code=502, detail=f"Dashboard data error: {exc}"
            ) from exc
        _dashboard_cache.update(loaded_at=time.time(), view=view)
        return view


@app.post("/api/dashboard/view")
async def dashboard_view(req: DashboardViewRequest) -> dict[str, Any]:
    try:
        return await dashboard_for_tool_calls(
            MCP_URL,
            _token(),
            [call.model_dump() for call in req.tool_calls],
            req.query,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the chat answer on failure
        logger.exception("dashboard query view failed")
        raise HTTPException(
            status_code=502, detail=f"Dashboard filter error: {exc}"
        ) from exc


# ------------------------------------------------------------- evaluation

_eval_state: dict[str, Any] = {"status": "idle"}
_eval_lock = threading.Lock()


def _run_eval(limit: int | None) -> None:
    from evaluation import run_evaluation

    def report_progress(update: dict[str, Any]) -> None:
        with _eval_lock:
            _eval_state.update(update, updated_at=time.time())

    try:
        result = run_evaluation(limit=limit, progress=report_progress)
        with _eval_lock:
            _eval_state.update(
                status="succeeded", finished_at=time.time(), result=result
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("evaluation failed")
        with _eval_lock:
            _eval_state.update(
                status="failed", finished_at=time.time(), error=str(exc)
            )


@app.post("/api/eval")
async def start_eval(limit: int | None = None) -> dict[str, Any]:
    if limit is not None and not 1 <= limit <= 9:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 9")
    with _eval_lock:
        if _eval_state.get("status") == "running":
            return _eval_state
        _eval_state.clear()
        _eval_state.update(
            status="running",
            started_at=time.time(),
            updated_at=time.time(),
            limit=limit,
            phase="starting",
            current=0,
            total=limit or 9,
            completed=0,
        )
    threading.Thread(target=_run_eval, args=(limit,), daemon=True).start()
    return _eval_state


@app.get("/api/eval")
async def eval_status() -> dict[str, Any]:
    with _eval_lock:
        return dict(_eval_state)


app.mount(
    "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui"
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("DATABRICKS_APP_PORT", "8000")))
