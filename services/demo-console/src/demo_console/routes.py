"""HTTP handlers for DESIGN.md section 5, the error shape and the request guard.

Errors are always ``{"error": {"code", "message", "hint"}}`` (5.0). The guard refuses any
``Host`` other than ``127.0.0.1:<port>`` / ``localhost:<port>`` (DNS rebinding), and every
mutating request needs ``Content-Type: application/json`` and ``X-Console: 1`` (4.6).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from demo_console.actions import ActionError
from demo_console.clients import NotFound, TemporalUnreachable
from demo_console.procs import ProcessError
from demo_console.rows import a2a_state_from_result, finance_rows, ui_link
from demo_console.runs import finance_task_id, support_id
from demo_console.scenarios import default_queue, default_workflow_id, scenario_json
from demo_console.settings import (
    AGENTS_NS,
    DEFAULT_APPROVER,
    DEV_TQ,
    PROFILES,
    SERVICES_NS,
    VERSIONED_TQ,
)

if TYPE_CHECKING:
    from demo_console.app import Console

STATUS = {
    "bad_request": 400,
    "forbidden_origin": 403,
    "aws_disabled": 403,
    "not_found": 404,
    "not_input_required": 409,
    "confirm_required": 409,
    "precondition_failed": 409,
    "invalid_param": 422,
    "temporal_unreachable": 503,
}
TICKETS = {
    "A": "Order #1042, my headphones arrived broken. I want my money back.",
    "B": "Order #2077: my laptop arrived with a cracked screen. I want a refund.",
}
_DURATION = re.compile(r"^(\d+(\.\d+)?(ms|s|m|h|d))+$")
_WF_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
TERMINAL = ("completed", "rejected", "failed", "canceled")


class ApiError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra


def error_response(code: str, message: str, hint: str | None = None, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message, "hint": hint, **extra}
    return JSONResponse({"error": body}, status_code=STATUS.get(code, 400))


def console(request: Request) -> Console:
    return request.app.state.console


async def body_of(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError("bad_request", "The body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ApiError("bad_request", "The body must be a JSON object.")
    return data


# --- the guard ---------------------------------------------------------------------------------


class Guard:
    """Host check on every request; JSON + ``X-Console: 1`` on every mutating one."""

    def __init__(self, app: ASGIApp, port: int) -> None:
        self.app = app
        self.allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        if host not in self.allowed:
            resp = error_response(
                "forbidden_origin",
                f"Host {host!r} is not allowed.",
                "Open the console at http://127.0.0.1:<port>/.",
            )
            await resp(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json" or headers.get("x-console") != "1":
                resp = error_response(
                    "forbidden_origin",
                    "Changes need Content-Type: application/json and X-Console: 1.",
                    "The console's own page sends both; another site cannot.",
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def unreachable_hint(profile: str) -> str:
    if profile == "local":
        return "Start the dev server (Manage stack), or run make dev-server-up."
    if profile == "eks-pf":
        return "Run make resume, then make pf TEMPORAL_PROFILE=eks-pf."
    return f"Run make pf TEMPORAL_PROFILE={profile}."


def handled(fn: Any) -> Any:
    """Map the console's exceptions to the error shape."""

    async def wrapper(request: Request) -> Response:
        try:
            return await fn(request)
        except (ApiError, ActionError, ProcessError) as err:
            extra = getattr(err, "extra", {}) or {}
            return error_response(err.code, err.message, err.hint, **extra)
        except NotFound as err:
            return error_response("not_found", f"Not found: {err}")
        except TemporalUnreachable:
            profile = console(request).settings.profile
            return error_response(
                "temporal_unreachable",
                "Temporal is not reachable at 127.0.0.1:7233.",
                unreachable_hint(profile),
            )

    wrapper.__name__ = fn.__name__
    return wrapper


# --- config and stack --------------------------------------------------------------------------


async def healthz(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


@handled
async def get_config(request: Request) -> Response:
    return JSONResponse(console(request).settings.config_body())


@handled
async def post_profile(request: Request) -> Response:
    c = console(request)
    body = await body_of(request)
    profile = body.get("profile")
    if profile not in PROFILES:
        raise ApiError("invalid_param", f"profile must be one of {list(PROFILES)}.")
    others = c.procs.other_profile(profile)
    if others:
        raise ApiError(
            "precondition_failed",
            "Processes started under another profile are still running.",
            "Stop them first (Manage stack).",
            processes=others,
        )
    await c.switch_profile(profile)
    return JSONResponse(c.settings.config_body())


@handled
async def get_stack(request: Request) -> Response:
    return JSONResponse(await console(request).hub.stack_cache.get())


@handled
async def get_scenarios(request: Request) -> Response:
    c = console(request)
    await c.hub.stack_cache.get()
    model = ((c.stack.last or {}).get("model") or {}).get("mode", "unknown")
    out = [scenario_json(s, c.stack.facts, model) for s in c.catalog.values()]
    return JSONResponse({"scenarios": out})


# --- runs --------------------------------------------------------------------------------------


@handled
async def list_runs(request: Request) -> Response:
    c = console(request)
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "25"))))
    except ValueError:
        raise ApiError("invalid_param", "limit must be a number.") from None
    if limit == 25:
        return JSONResponse(await c.hub.runs_cache.get())
    return JSONResponse(await c.runs.list_runs(limit))


def run_cost(c: Console, queue: str) -> str:
    profile = c.settings.profile
    if profile == "eks-pf":
        return "aws" if queue == "versioned" else "tokens"
    model = ((c.stack.last or {}).get("model") or {}).get("mode")
    return "tokens" if model in ("live", "mixed") else "free"


@handled
async def start_run(request: Request) -> Response:
    c = console(request)
    body = await body_of(request)
    sid = body.get("scenario")
    scenario = c.catalog.get(sid or "")
    if scenario is None:
        raise ApiError("not_found", f"No scenario {sid!r}.")
    await c.hub.stack_cache.get()
    queue = body.get("queue") or default_queue(scenario, c.stack.facts.shape)
    if queue is None or not scenario.case:
        raise ApiError("invalid_param", f"Scenario {sid} does not start a Support run.")
    if queue not in ("dev", "versioned"):
        raise ApiError("invalid_param", "queue must be dev or versioned.")
    if c.settings.profile not in scenario.profiles:
        raise ApiError(
            "precondition_failed", f"{scenario.title} is not for profile {c.settings.profile}."
        )
    mode = body.get("a2a_mode") or "tool"
    if mode not in ("tool", "handoff"):
        raise ApiError("invalid_param", "a2a_mode must be tool or handoff.")
    budget = body.get("nexus_send_timeout") or "25h"
    if not isinstance(budget, str) or not _DURATION.match(budget):
        raise ApiError("invalid_param", "nexus_send_timeout must look like 25h, 90m or 30s.")
    wf = body.get("workflow_id") or default_workflow_id(scenario)
    if not isinstance(wf, str) or not _WF_ID.match(wf) or wf.startswith("a2a-task-"):
        raise ApiError("invalid_param", f"Invalid workflow ID {wf!r}.")
    ticket = body.get("ticket") or TICKETS[scenario.case]
    if not isinstance(ticket, str):
        raise ApiError("invalid_param", "ticket must be text.")
    cost = run_cost(c, queue)
    try:
        c.actions.gate(cost, bool(body.get("confirm")), what=f"Starting {scenario.title}")
    except ActionError as err:
        raise ApiError(err.code, err.message, err.hint) from None
    task_queue = VERSIONED_TQ if queue == "versioned" else DEV_TQ
    run_id, attached = await c.gateway.start_workflow(
        AGENTS_NS,
        "SupportSupervisorWorkflow",
        [ticket, mode, budget],
        workflow_id=wf,
        task_queue=task_queue,
    )
    if not attached:
        c.runs.save_record(wf, scenario=scenario.id, queue=queue, a2a_mode=mode)
    c.hub.runs_cache.invalidate()
    s = c.settings
    return JSONResponse(
        {
            "workflow_id": wf,
            "run_id": run_id,
            "attached": attached,
            "links": {
                "support": ui_link(s.ui_base, AGENTS_NS, wf),
                "finance": ui_link(s.ui_base, SERVICES_NS, finance_task_id(wf)),
            },
        },
        status_code=201,
    )


@handled
async def get_run(request: Request) -> Response:
    c = console(request)
    return JSONResponse(await c.runs.view(request.path_params["wf"]))


@handled
async def get_rows(request: Request) -> Response:
    c = console(request)
    raw = request.query_params.get("after")
    try:
        after = int(raw) if raw not in (None, "") else None
    except ValueError:
        raise ApiError("invalid_param", "after must be a number.") from None
    return JSONResponse(await c.runs.rows(request.path_params["wf"], after))


@handled
async def get_report(request: Request) -> Response:
    c = console(request)
    return JSONResponse(await c.runs.report(request.path_params["wf"]))


async def _task_state(c: Console, task_id: str) -> str | None:
    """``get_task`` (5 s); with no Finance poller, the task's history instead."""
    try:
        value = await c.gateway.query(SERVICES_NS, task_id, "get_task", 5.0)
        state, _, _ = a2a_state_from_result(value)
        return state
    except NotFound:
        raise
    except Exception:
        history = await c.gateway.fetch_history(SERVICES_NS, task_id)
        _, facts = finance_rows(history, ui_base="", task_id=task_id, send_pair=None)
        return facts.state


async def _decide(request: Request, verdict: str) -> Response:
    c = console(request)
    body = await body_of(request)
    approver = body.get("approver", DEFAULT_APPROVER)
    if not isinstance(approver, str) or not approver.strip():
        raise ApiError("invalid_param", "approver must not be empty.")
    comment = body.get("comment") or ""
    if not isinstance(comment, str):
        raise ApiError("invalid_param", "comment must be text.")
    wf = support_id(request.path_params["wf"])
    task_id = finance_task_id(request.path_params["wf"])
    try:
        before = await _task_state(c, task_id)
    except NotFound:
        raise ApiError(
            "not_found", f"No A2A task {task_id} yet.", "Finance has not been asked yet."
        ) from None
    if before != "input-required":
        raise ApiError(
            "not_input_required",
            f"The A2A task is {before}; approve and reject only apply to input-required.",
            "Open the run to see who decided.",
        )
    await c.gateway.signal(
        SERVICES_NS, task_id, verdict, {"approver": approver.strip(), "comment": comment.strip()}
    )
    after = before
    loop = asyncio.get_running_loop()
    deadline = loop.time() + c.settle_s
    while loop.time() < deadline:
        await asyncio.sleep(0.25)
        try:
            after = await _task_state(c, task_id)
        except NotFound:
            break
        if after in TERMINAL:
            break
    c.runs.forget(wf)
    return JSONResponse(
        {
            "workflow_id": wf,
            "task_id": task_id,
            "verdict": verdict,
            "state_before": before,
            "state_after": after,
            "settled": after in TERMINAL,
        }
    )


@handled
async def approve(request: Request) -> Response:
    return await _decide(request, "approve")


@handled
async def reject(request: Request) -> Response:
    return await _decide(request, "reject")


@handled
async def terminate(request: Request) -> Response:
    c = console(request)
    body = await body_of(request)
    if not body.get("confirm"):
        raise ApiError(
            "confirm_required", "Terminating ends the conversation for good.", "Send confirm: true."
        )
    reason = body.get("reason") or "console cleanup"
    wf = support_id(request.path_params["wf"])
    await c.gateway.terminate(AGENTS_NS, wf, str(reason))
    finance = False
    try:
        desc = await c.gateway.describe(SERVICES_NS, finance_task_id(wf))
        if desc.workflow_execution_info.status == 1:  # RUNNING
            await c.gateway.terminate(SERVICES_NS, finance_task_id(wf), str(reason))
            finance = True
    except NotFound:
        pass
    c.runs.forget(wf)
    c.hub.runs_cache.invalidate()
    return JSONResponse({"terminated": True, "finance_terminated": finance})


# --- actions, jobs, processes ------------------------------------------------------------------


@handled
async def list_actions(request: Request) -> Response:
    c = console(request)
    await c.hub.stack_cache.get()
    return JSONResponse({"actions": c.actions.describe_all()})


@handled
async def run_action(request: Request) -> Response:
    c = console(request)
    body = await body_of(request)
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise ApiError("bad_request", "params must be an object.")
    await c.hub.stack_cache.get()
    status, job = await c.actions.run(request.path_params["id"], params, bool(body.get("confirm")))
    return JSONResponse(job, status_code=status)


@handled
async def list_jobs(request: Request) -> Response:
    c = console(request)
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "20"))))
    except ValueError:
        raise ApiError("invalid_param", "limit must be a number.") from None
    return JSONResponse({"jobs": [c.jobs.view(j) for j in c.jobs.recent(limit)]})


@handled
async def get_job(request: Request) -> Response:
    c = console(request)
    job = c.jobs.get(request.path_params["id"])
    if job is None:
        raise ApiError("not_found", f"No job {request.path_params['id']!r}.")
    return JSONResponse(c.jobs.view(job, lines=200))


@handled
async def list_processes(request: Request) -> Response:
    c = console(request)
    await c.hub.stack_cache.get()
    return JSONResponse({"processes": c.procs.describe_all()})


@handled
async def start_process(request: Request) -> Response:
    c = console(request)
    body = await body_of(request)
    await c.hub.stack_cache.get()
    wci = body.get("wci", 0)
    try:
        wci = int(wci)
    except (TypeError, ValueError):
        raise ApiError("invalid_param", "wci must be 0 or 1.") from None
    proc = await c.procs.start(
        request.path_params["name"],
        variant=body.get("variant"),
        wci=wci,
        confirm=bool(body.get("confirm")),
    )
    c.hub.stack_cache.invalidate()
    return JSONResponse(proc)


@handled
async def stop_process(request: Request) -> Response:
    c = console(request)
    body = await body_of(request)
    proc = await c.procs.stop(request.path_params["name"], sig=body.get("signal") or "TERM")
    c.hub.stack_cache.invalidate()
    return JSONResponse(proc)


@handled
async def process_log(request: Request) -> Response:
    c = console(request)
    try:
        tail = max(1, min(5000, int(request.query_params.get("tail", "200"))))
    except ValueError:
        raise ApiError("invalid_param", "tail must be a number.") from None
    name = request.path_params["name"]
    lines = c.procs.tail(name, tail)
    if request.query_params.get("raw") == "1":
        return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""))
    return JSONResponse({"name": name, "lines": lines})


# --- the stream --------------------------------------------------------------------------------


async def stream(request: Request) -> Response:
    c = console(request)
    wf = request.query_params.get("wf") or None
    events = c.hub.events(support_id(wf) if wf else None, disconnected=request.is_disconnected)
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


ROUTES = [
    Route("/healthz", healthz),
    Route("/api/config", get_config),
    Route("/api/profile", post_profile, methods=["POST"]),
    Route("/api/stack", get_stack),
    Route("/api/scenarios", get_scenarios),
    Route("/api/runs", list_runs),
    Route("/api/runs", start_run, methods=["POST"]),
    Route("/api/runs/{wf}", get_run),
    Route("/api/runs/{wf}/rows", get_rows),
    Route("/api/runs/{wf}/report", get_report),
    Route("/api/runs/{wf}/approve", approve, methods=["POST"]),
    Route("/api/runs/{wf}/reject", reject, methods=["POST"]),
    Route("/api/runs/{wf}/terminate", terminate, methods=["POST"]),
    Route("/api/actions", list_actions),
    Route("/api/actions/{id}", run_action, methods=["POST"]),
    Route("/api/jobs", list_jobs),
    Route("/api/jobs/{id}", get_job),
    Route("/api/processes", list_processes),
    Route("/api/processes/{name}/start", start_process, methods=["POST"]),
    Route("/api/processes/{name}/stop", stop_process, methods=["POST"]),
    Route("/api/processes/{name}/log", process_log),
    Route("/api/stream", stream),
]
