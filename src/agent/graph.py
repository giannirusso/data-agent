"""LangGraph wiring. The model proposes; these edges decide what is allowed to run."""

from __future__ import annotations

import time

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph

from agent.deps import Deps
from agent.nodes.analyze import analyze_node, save_last_node
from agent.nodes.check_result import check_result_node
from agent.nodes.delete_flow import confirm_delete_node, find_delete_node
from agent.nodes.execute import dry_run_node, execute_node
from agent.nodes.generate_sql import generate_sql_node
from agent.nodes.guard import guard_node
from agent.nodes.mask import mask_node
from agent.nodes.plan import plan_node
from agent.nodes.reports import (
    chitchat_node,
    describe_schema_node,
    give_up_node,
    preferences_node,
    report_ops_node,
)
from agent.nodes.retrieve import retrieve_node
from agent.nodes.route import route_node
from agent.nodes.validate import validate_node
from agent.state import AgentState

_DETAIL_KEYS = (
    "intent",
    "sql",
    "rewritten_sql",
    "last_error",
    "attempts",
    "status",
    "last_bytes",
    "fatal",
    "pii_hits",
    "persona_version",
    "stop_retry",
    "prompt_chars",
)


def build_graph(deps: Deps, checkpointer):
    graph = StateGraph(AgentState)

    def after_guard(state: dict) -> str:
        if state.get("refusal"):
            return "mask"
        return "route"

    def after_route(state: dict) -> str:
        return {
            "schema": "describe_schema",
            "analysis": "retrieve",
            "report": "retrieve",
            "follow_up": "analyze",
            "report_list": "report_ops",
            "report_restore": "report_ops",
            "report_delete": "find_delete",
            "chitchat": "chitchat",
            "preferences": "preferences",
            "save_last": "save_last",
            "scope_denied": "mask",
            "refused": "mask",
        }.get(state.get("intent") or "", "retrieve")

    def after_generate(state: dict) -> str:
        if state.get("partial"):
            return "analyze"
        if state.get("fatal") or state.get("stop_retry"):
            return "give_up"
        return "validate_sql"

    def after_validate(state: dict) -> str:
        if state.get("fatal") or state.get("stop_retry"):
            return "give_up"
        if state.get("last_error"):
            if int(state.get("attempts") or 0) >= deps.settings.max_sql_attempts:
                return "give_up"
            return "generate_sql"
        return "dry_run"

    def after_dry(state: dict) -> str:
        if state.get("retry_dry"):
            return "dry_run"
        if state.get("fatal") or state.get("stop_retry"):
            return "give_up"
        if state.get("cache_hit"):
            return "check_result"
        if state.get("last_error"):
            if int(state.get("attempts") or 0) >= deps.settings.max_sql_attempts:
                return "give_up"
            return "generate_sql"
        return "execute"

    def after_execute(state: dict) -> str:
        if state.get("retry_execute"):
            return "execute"
        if state.get("fatal") or state.get("stop_retry"):
            return "give_up"
        if state.get("last_error"):
            if int(state.get("attempts") or 0) >= deps.settings.max_sql_attempts:
                return "give_up"
            return "generate_sql"
        return "check_result"

    def after_check(state: dict) -> str:
        if state.get("partial"):
            return "analyze"
        if state.get("fatal") or (state.get("stop_retry") and state.get("last_error")):
            return "give_up"
        if state.get("last_error"):
            if int(state.get("attempts") or 0) >= deps.settings.max_sql_attempts:
                return "give_up"
            return "generate_sql"
        if int(state.get("current_step") or 0) < len(state.get("plan") or []):
            return "generate_sql"
        return "analyze"

    def after_find(state: dict) -> str:
        if state.get("pending_delete"):
            return "confirm_delete"
        return "mask"

    steps = {
        "guard": guard_node,
        "route": route_node,
        "describe_schema": describe_schema_node,
        "retrieve": retrieve_node,
        "plan": plan_node,
        "generate_sql": generate_sql_node,
        "validate_sql": validate_node,
        "dry_run": dry_run_node,
        "execute": execute_node,
        "check_result": check_result_node,
        "analyze": analyze_node,
        "save_last": save_last_node,
        "report_ops": report_ops_node,
        "chitchat": chitchat_node,
        "preferences": preferences_node,
        "give_up": give_up_node,
        "find_delete": find_delete_node,
        "confirm_delete": confirm_delete_node,
        "mask": mask_node,
    }
    for name, fn in steps.items():
        graph.add_node(name, _traced(name, fn, deps))

    graph.add_edge(START, "guard")
    graph.add_conditional_edges("guard", after_guard, ["mask", "route"])
    graph.add_conditional_edges(
        "route",
        after_route,
        [
            "describe_schema",
            "retrieve",
            "analyze",
            "report_ops",
            "find_delete",
            "chitchat",
            "preferences",
            "save_last",
            "mask",
        ],
    )
    graph.add_edge("retrieve", "plan")
    graph.add_edge("plan", "generate_sql")
    graph.add_conditional_edges("generate_sql", after_generate, ["validate_sql", "give_up"])
    graph.add_conditional_edges("validate_sql", after_validate, ["dry_run", "generate_sql", "give_up"])
    graph.add_conditional_edges("dry_run", after_dry, ["execute", "dry_run", "generate_sql", "give_up", "check_result"])
    graph.add_conditional_edges("execute", after_execute, ["check_result", "execute", "generate_sql", "give_up"])
    graph.add_conditional_edges("check_result", after_check, ["generate_sql", "analyze", "give_up"])
    graph.add_conditional_edges("find_delete", after_find, ["confirm_delete", "mask"])

    for name in (
        "describe_schema",
        "analyze",
        "save_last",
        "report_ops",
        "chitchat",
        "preferences",
        "give_up",
        "confirm_delete",
    ):
        graph.add_edge(name, "mask")
    graph.add_edge("mask", END)
    return graph.compile(checkpointer=checkpointer)


def _traced(name: str, fn, deps: Deps):
    def node(state: dict) -> dict:
        started = time.perf_counter()
        status = "ok"
        result: dict | None = None
        try:
            result = fn(state, deps)
            status = _span_status(name, result)
            return result
        except GraphInterrupt:
            status = "interrupted"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            deps.logger.span(
                trace_id=(state or {}).get("trace_id") or "",
                span=name,
                duration_ms=(time.perf_counter() - started) * 1000,
                status=status,
                user_id=(state or {}).get("user_id"),
                session_id=(state or {}).get("session_id"),
                details=_details(name, result),
            )

    return node


def _span_status(name: str, result: dict | None) -> str:
    if not isinstance(result, dict):
        return "ok"
    if name == "guard" and result.get("refusal"):
        return "blocked"
    if name == "confirm_delete":
        return str(result.get("status") or "ok")
    if result.get("last_error") and name in {"validate_sql", "dry_run", "execute", "check_result", "generate_sql"}:
        return "error"
    if result.get("status") == "give_up":
        return "give_up"
    return "ok"


def _details(name: str, result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    details = {}
    for key in _DETAIL_KEYS:
        value = result.get(key)
        if value not in (None, "", [], {}):
            details[key] = value
    if name == "retrieve":
        details["golden_ids"] = [trio.get("id") for trio in result.get("golden_trios") or []]
    if name == "plan":
        details["steps"] = len(result.get("plan") or [])
    if name == "check_result" and result.get("results") is not None:
        details["result_count"] = len(result["results"])
    if name == "execute" and result.get("pending_frame"):
        details["row_count"] = result["pending_frame"].get("row_count")
    return details
