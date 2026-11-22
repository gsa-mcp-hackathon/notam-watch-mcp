"""MLflow GenAI evaluation of the NFS aviation agent.

Each eval question runs through the real agent (real model, real mcp-nfs tools,
live gold data). MLflow records one trace per question plus these scores:

- tool_call_count / within_tool_budget: how many MCP calls it took (the
  approval-loop problem we are fixing), against a per-question budget.
- used_expected_tool: the agent picked the tool designed for the question.
- no_tool_errors: no tool response contained an "error" field, except errors
  the question expects (e.g. "No current position" for a flight that is not
  airborne), which are a correct answer, not a failure.
- latency_seconds: end-to-end time per question.
- answer_quality: an LLM judge (MLflow ExpectationsGuidelines) grading the
  answer against per-question guidelines (grounded in tool output, etc.).
- mission_quality: a second LLM judge grading whether the answer prioritizes
  critical risk, suppresses irrelevant noise, handles uncertainty, and helps
  the named aviation user decide what deserves attention first.

Results land as an evaluation run in the app's MLflow experiment.

Two modes (EVAL_MODE env var):
- "rest" (default): scores are computed from the agent's reply (tool calls,
  tool outputs, latency) and the judge is called directly. Everything is
  logged through MLflow REST calls (run params/metrics/tags and trace
  assessments), so it works even when the app cannot reach cloud storage.
- "genai": mlflow.genai.evaluate with trace-based scorers; needs storage access.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import mlflow
from mlflow.entities import Feedback, SpanType
from mlflow.genai.scorers import ExpectationsGuidelines, scorer

from agent import MAX_TURNS, MCP_URL, MODEL, run_agent

AGENT_TIMEOUT_SECONDS = float(os.getenv("EVAL_AGENT_TIMEOUT_SECONDS", "120"))
JUDGE_TIMEOUT_SECONDS = float(os.getenv("EVAL_JUDGE_TIMEOUT_SECONDS", "30"))
ENABLE_JUDGES = os.getenv("EVAL_ENABLE_JUDGES", "true").lower() in {
    "1",
    "true",
    "yes",
}
ProgressCallback = Callable[[dict[str, Any]], None]

COMMON_GUIDELINES = [
    "The answer is based on tool output, not general knowledge, and does not "
    "invent flights, NOTAM numbers or numbers that are not in the tool output.",
    "The answer leads with a direct answer to the question. Where they apply "
    "to this question, it notes caveats from the tool output (for example an "
    "unknown altitude band or stale data); it need not invent caveats that "
    "do not apply.",
]

MISSION_GUIDELINES = [
    "The answer prioritizes the most operationally important confirmed risks "
    "before lower-priority or uncertain items.",
    "The answer does not elevate clearly inactive, off-route, or vertically "
    "irrelevant evidence as a critical risk.",
    "The answer distinguishes confirmed risk from unresolved altitude, "
    "schedule, freshness, or causation uncertainty.",
    "The answer is useful for the role named in the question and clearly says "
    "what deserves attention first without replacing official procedures.",
]


def _case(
    question: str,
    tools: list[str],
    budget: int,
    *extra: str,
    allowed_errors: tuple[str, ...] = (),
    mission: tuple[str, ...] = (),
    tool_match: str = "any",
) -> dict:
    # Questions that need no tool are judged only on their own guidelines.
    # allowed_errors: tool error texts (case-insensitive substrings) that are
    # an expected outcome for this question, not a tool failure.
    common = COMMON_GUIDELINES if tools else []
    return {
        "inputs": {"question": question},
        "expectations": {
            "expected_tools": tools,
            "expected_tools_mode": tool_match,
            "max_tool_calls": budget,
            "guidelines": [*common, *extra],
            "mission_guidelines": [*MISSION_GUIDELINES, *mission]
            if mission
            else [],
            "allowed_tool_errors": list(allowed_errors),
        },
    }


def unexpected_errors(errors: list[str], expectations: dict) -> list[str]:
    """Tool errors that are not an expected outcome for the question."""
    allowed = [a.lower() for a in expectations.get("allowed_tool_errors") or []]
    return [e for e in errors if not any(a in e.lower() for a in allowed)]


EVAL_DATA: list[dict[str, Any]] = [
    _case(
        "I'm flying from New York JFK (KJFK) to Washington Reagan National "
        "(KDCA) at 30,000 feet (FL300), departing now. What are the three most "
        "important Notices to Air Missions (NOTAMs) for this flight, and why?",
        ["get_route_notams"],
        1,
        "Returns a concise top-three briefing rather than summarizing every notice.",
        mission=(
            "Ranks route crossings, active-at-passage hazards, and vertically "
            "relevant notices ahead of general airport or corridor notices.",
        ),
    ),
    _case(
        "For a flight from New York JFK (KJFK) to Washington Reagan National "
        "(KDCA) at 30,000 feet (FL300), which NOTAMs can be given lower priority "
        "because the flight will not enter the affected altitude ranges?",
        ["get_route_notams"],
        1,
        "Explains which notices are vertically irrelevant instead of merely "
        "listing the notices that remain relevant.",
        mission=(
            "Uses altitude evidence to demonstrate noise reduction and does not "
            "treat unresolved AGL relationships as safely irrelevant.",
        ),
    ),
    _case(
        "I'm flying from New York JFK (KJFK) to Washington Reagan National "
        "(KDCA) at 30,000 feet (FL300), departing now. Which active runway, "
        "landing-approach, or airspace conditions could affect my arrival?",
        ["get_route_notams"],
        1,
        "Focuses on destination conditions whose schedules overlap the estimated "
        "arrival window.",
        mission=(
            "Prioritizes arrival conditions by operational impact and timing, "
            "rather than returning an undifferentiated destination NOTAM list.",
        ),
    ),
    _case(
        "Which flights are currently inside or expected to enter an active "
        "temporary flight restriction (TFR) within the next 30 minutes? Rank "
        "the most urgent first.",
        ["get_flights_approaching_notams"],
        1,
        "Lists inside-now flights before future entries, then orders future "
        "entries by time to entry.",
        mission=(
            "Identifies the flight and TFR for each urgent encounter and separates "
            "confirmed vertical conflicts from altitude-unknown encounters.",
        ),
    ),
    _case(
        "Which flights are approaching active restricted or warning airspace in "
        "the next 30 minutes? Clearly separate confirmed altitude conflicts from "
        "cases where the altitude relationship is still unknown.",
        ["get_flights_approaching_notams"],
        1,
        "Reports confirmed and altitude-unknown encounter counts separately.",
        mission=(
            "Prioritizes inside-now and soonest-entry encounters while respecting "
            "the published schedule and altitude band.",
        ),
    ),
    _case(
        "Which flights are moving away from their planned routes near newly "
        "active NOTAM areas? Rank the strongest cases for analyst review without "
        "claiming that the NOTAM caused the route change.",
        ["find_route_deviations_near_notams"],
        1,
        "Ranks candidates using deviation magnitude, proximity, and whether the "
        "original route crossed the active NOTAM area.",
        mission=(
            "Treats each deviation as a candidate for analyst review, not proof "
            "that the NOTAM caused the route change.",
        ),
    ),
    _case(
        "Find the most urgent currently tracked flight approaching an active "
        "temporary flight restriction (TFR). If one is available, brief me on "
        "that flight identifier (callsign): what deserves attention first, and "
        "what can be given lower priority?",
        ["get_flights_approaching_notams", "get_flight_briefing"],
        2,
        "Selects a callsign from current tool evidence before requesting its "
        "complete flight briefing. If no candidate is available, says so without "
        "inventing a callsign.",
        mission=(
            "Connects the selected flight's current position, route, altitude, "
            "hazards ahead, and destination notices into one prioritized briefing.",
        ),
    ),
    _case(
        "Which active runway or landing-approach outages at Chicago O'Hare "
        "International Airport (KORD) require the most immediate attention, "
        "and why?",
        ["search_notams"],
        1,
        "Ranks active runway and approach impacts instead of returning an "
        "unprioritized text search result.",
        mission=(
            "Explains operational impact and avoids treating every matching notice "
            "as equally urgent.",
        ),
    ),
    _case(
        "Explain this NOTAM in plain language for someone unfamiliar with aviation "
        "abbreviations: AIRSPACE NUMEROUS UAS WI AN AREA DEFINED AS "
        "6.47NM RADIUS OF 410857N0960755W (3.8NM S MLE) SFC-400FT AGL",
        [],
        0,
        "Explains in plain English that many drones (unmanned aircraft) operate "
        "within a 6.47 nautical mile radius of the given point, about 3.8 NM "
        "south of MLE, from the surface up to 400 ft above ground level.",
        "States the point as about 41 deg 08' 57\" N, 96 deg 07' 55\" W, or "
        "leaves the coordinate as written; it must not give different degrees.",
        mission=(
            "Translates the notice into plain language while preserving the exact "
            "location, radius, and surface-to-400-foot AGL limit.",
        ),
    ),
]


def _run_sync(coro):
    """Run a coroutine from sync code, even if this thread has a running loop
    (notebooks); then it runs on a fresh thread with its own loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    ctx = contextvars.copy_context()  # keep the MLflow trace context
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, asyncio.run, coro).result()


def predict(question: str) -> str:
    """predict_fn for mlflow.genai.evaluate."""
    reply = _run_sync(run_agent([{"role": "user", "content": question}]))
    return reply.answer


def _tool_spans(trace) -> list:
    return trace.search_spans(span_type=SpanType.TOOL)


@scorer
def tool_call_count(trace) -> int:
    return len(_tool_spans(trace))


@scorer
def within_tool_budget(trace, expectations) -> Feedback:
    names = [s.name for s in _tool_spans(trace)]
    budget = int(expectations.get("max_tool_calls", 3))
    return Feedback(
        value=len(names) <= budget,
        rationale=f"{len(names)} tool call(s), budget {budget}: {names}",
    )


@scorer
def used_expected_tool(trace, expectations) -> Feedback:
    names = [s.name for s in _tool_spans(trace)]
    expected = expectations.get("expected_tools") or []
    mode = expectations.get("expected_tools_mode", "any")
    if not expected:
        return Feedback(value=not names, rationale=f"No tool expected; got {names}")
    hit = all(n in names for n in expected) if mode == "all" else any(
        n in expected for n in names
    )
    return Feedback(
        value=hit,
        rationale=f"Expected {mode} of {expected}; got {names}",
    )


@scorer
def no_tool_errors(trace, expectations) -> Feedback:
    errors = []
    for span in _tool_spans(trace):
        text = span.outputs if isinstance(span.outputs, str) else json.dumps(
            span.outputs, default=str
        )
        if text and '"error"' in text:
            errors.append(f"{span.name}: {text[:200]}")
    real = unexpected_errors(errors, expectations or {})
    expected = len(errors) - len(real)
    note = f" ({expected} expected error(s) ignored)" if expected else ""
    return Feedback(value=not real, rationale=("; ".join(real) or "No errors") + note)


@scorer
def latency_seconds(trace) -> float:
    return round((trace.info.execution_duration or 0) / 1000.0, 1)


# LLM judge that grades each answer against that row's expectations["guidelines"].
answer_quality = ExpectationsGuidelines(name="answer_quality")
mission_quality_judge = ExpectationsGuidelines(name="mission_quality")


@scorer
def mission_quality(inputs, outputs, expectations) -> Feedback:
    guidelines = expectations.get("mission_guidelines") or []
    if not guidelines:
        return Feedback(value="skip", rationale="No mission guidelines for this case")
    return mission_quality_judge(
        inputs=inputs,
        outputs=outputs,
        expectations={"guidelines": guidelines},
    )

SCORERS = [
    tool_call_count,
    within_tool_budget,
    used_expected_tool,
    no_tool_errors,
    latency_seconds,
    answer_quality,
    mission_quality,
]


def run_evaluation_genai(limit: int | None = None) -> dict[str, Any]:
    """mlflow.genai.evaluate path. Needs the app to reach cloud storage, because
    scorers download each trace's span data (traces.json); without that it
    fails with 'Connection refused' to *.storage.cloud.databricks.com."""
    data = EVAL_DATA[:limit] if limit else EVAL_DATA
    with mlflow.start_run(run_name=f"agent-eval-{time.strftime('%Y%m%d-%H%M%S')}"):
        mlflow.log_params(
            {"model": MODEL, "mcp_url": MCP_URL, "max_turns": MAX_TURNS,
             "questions": len(data)}
        )
        result = mlflow.genai.evaluate(data=data, predict_fn=predict, scorers=SCORERS)
    return {"run_id": result.run_id, "metrics": result.metrics}


# ------------------------------------------------- storage-free (REST) mode


def _judge(
    judge, question: str, answer: str, guidelines: list[str]
) -> tuple[Any, str]:
    """Call the ExpectationsGuidelines judge directly (no trace needed)."""
    if not guidelines:
        return None, "no guidelines"
    if not ENABLE_JUDGES:
        return None, "LLM judges disabled by EVAL_ENABLE_JUDGES"

    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    context = contextvars.copy_context()

    def call_judge() -> None:
        try:
            feedback = context.run(
                judge,
                inputs={"question": question},
                outputs=answer,
                expectations={"guidelines": guidelines},
            )
            result_queue.put((True, feedback))
        except Exception as exc:  # noqa: BLE001
            result_queue.put((False, exc))

    threading.Thread(target=call_judge, daemon=True).start()
    try:
        succeeded, result = result_queue.get(timeout=JUDGE_TIMEOUT_SECONDS)
        if not succeeded:
            raise result
        fb = result
        return fb.value, fb.rationale or ""
    except queue.Empty:
        return None, f"judge timed out after {JUDGE_TIMEOUT_SECONDS:g}s"
    except Exception as exc:  # noqa: BLE001 - judge outage must not stop eval
        return None, f"judge error: {exc}"[:300]


def _score_case(
    case: dict[str, Any],
    reply,
    progress: ProgressCallback | None = None,
    question_number: int | None = None,
    total_questions: int | None = None,
) -> dict[str, Any]:
    exp = case["expectations"]
    names = [t["name"] for t in reply.tool_calls]
    expected = exp.get("expected_tools") or []
    mode = exp.get("expected_tools_mode", "any")
    budget = int(exp.get("max_tool_calls", 3))
    used = (not names) if not expected else (
        all(n in names for n in expected)
        if mode == "all"
        else any(n in expected for n in names)
    )
    if progress:
        progress(
            {
                "phase": "answer_judge",
                "current": question_number,
                "total": total_questions,
            }
        )
    verdict, rationale = _judge(
        answer_quality,
        case["inputs"]["question"],
        reply.answer,
        exp.get("guidelines") or [],
    )
    if progress:
        progress(
            {
                "phase": "mission_judge",
                "current": question_number,
                "total": total_questions,
            }
        )
    mission_verdict, mission_rationale = _judge(
        mission_quality_judge,
        case["inputs"]["question"],
        reply.answer,
        exp.get("mission_guidelines") or [],
    )
    return {
        "question": case["inputs"]["question"],
        "tools": names,
        "tool_call_count": len(names),
        "within_tool_budget": len(names) <= budget,
        "used_expected_tool": used,
        "no_tool_errors": not unexpected_errors(reply.tool_errors, exp),
        "expected_tool_errors": len(reply.tool_errors)
        - len(unexpected_errors(reply.tool_errors, exp)),
        "latency_seconds": reply.latency_s,
        "answer_quality": verdict,
        "judge_rationale": rationale[:600],
        "mission_quality": mission_verdict,
        "mission_rationale": mission_rationale[:600],
        "trace_id": reply.trace_id,
        "answer": reply.answer,
        "answer_preview": reply.answer[:300],
    }


def _log_assessments(row: dict[str, Any]) -> None:
    """Attach scores to the request trace (REST call; best effort)."""
    if not row.get("trace_id"):
        return
    for name in (
        "within_tool_budget",
        "used_expected_tool",
        "no_tool_errors",
        "answer_quality",
        "mission_quality",
    ):
        if row.get(name) is None:
            continue
        try:
            mlflow.log_feedback(
                trace_id=row["trace_id"],
                name=name,
                value=row[name],
                rationale=(
                    row["judge_rationale"]
                    if name == "answer_quality"
                    else row["mission_rationale"]
                    if name == "mission_quality"
                    else None
                ),
            )
        except Exception:  # noqa: BLE001
            return


def run_evaluation_rest(
    limit: int | None = None, progress: ProgressCallback | None = None
) -> dict[str, Any]:
    data = EVAL_DATA[:limit] if limit else EVAL_DATA
    rows: list[dict[str, Any]] = []
    with mlflow.start_run(
        run_name=f"agent-eval-{time.strftime('%Y%m%d-%H%M%S')}"
    ) as run:
        mlflow.set_tag("eval_mode", "rest")
        mlflow.log_params(
            {"model": MODEL, "mcp_url": MCP_URL, "max_turns": MAX_TURNS,
             "questions": len(data)}
        )
        for i, case in enumerate(data):
            question = case["inputs"]["question"]
            if progress:
                progress(
                    {
                        "phase": "agent",
                        "current": i + 1,
                        "total": len(data),
                        "completed": i,
                        "question": question,
                    }
                )
            try:
                reply = _run_sync(
                    asyncio.wait_for(
                        run_agent([{"role": "user", "content": question}]),
                        timeout=AGENT_TIMEOUT_SECONDS,
                    )
                )
                row = _score_case(
                    case,
                    reply,
                    progress=progress,
                    question_number=i + 1,
                    total_questions=len(data),
                )
            except TimeoutError:
                row = {
                    "question": question,
                    "error": f"agent timed out after {AGENT_TIMEOUT_SECONDS:g}s",
                }
            except Exception as exc:  # noqa: BLE001 - one bad case, keep going
                row = {"question": question, "error": str(exc)[:300]}
            rows.append(row)
            _log_assessments(row)
            for key in ("tool_call_count", "latency_seconds"):
                if key in row:
                    mlflow.log_metric(f"q_{key}", float(row[key]), step=i)
            # Per-question detail as a run tag (REST, no artifact upload).
            mlflow.set_tag(f"q{i:02d}", json.dumps(row, default=str)[:4900])
            if progress:
                progress(
                    {
                        "phase": "completed_question",
                        "current": i + 1,
                        "total": len(data),
                        "completed": i + 1,
                        "question": question,
                    }
                )
        ok = [r for r in rows if "error" not in r]
        metrics: dict[str, float] = {"questions_completed": float(len(ok))}
        for key in ("tool_call_count", "latency_seconds", "within_tool_budget",
                    "used_expected_tool", "no_tool_errors"):
            vals = [float(r[key]) for r in ok]
            if vals:
                metrics[f"{key}/mean"] = round(sum(vals) / len(vals), 3)
        for judge_name in ("answer_quality", "mission_quality"):
            judged = [
                r[judge_name]
                for r in ok
                if r.get(judge_name) in ("yes", "no", True, False)
            ]
            if judged:
                metrics[f"{judge_name}/mean"] = round(
                    sum(1 for value in judged if value in ("yes", True))
                    / len(judged),
                    3,
                )
        mlflow.log_metrics(metrics)
    return {"run_id": run.info.run_id, "metrics": metrics, "questions": rows}


def run_evaluation(
    limit: int | None = None, progress: ProgressCallback | None = None
) -> dict[str, Any]:
    if os.getenv("EVAL_MODE", "rest").lower() == "genai":
        return run_evaluation_genai(limit)
    return run_evaluation_rest(limit, progress=progress)
