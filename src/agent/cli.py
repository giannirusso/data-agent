"""Terminal chat for the retail analytics agent."""

from __future__ import annotations

import re
import time
import uuid

import typer
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from agent.numbers import format_cell
from agent.obs.logging import summarize_session
from agent.runtime import Runtime, build_runtime
from config.settings import load_settings

app = typer.Typer(add_completion=False, no_args_is_help=False)
console = Console()


class SessionExit(Exception):
    """Raised by /exit so the REPL can close without a traceback."""


@app.callback(invoke_without_command=True)
def main(
    user: str = typer.Option("alice", "--user", "-u", help="Demo user (alice or bob)."),
    model: str | None = typer.Option(None, "--model", help="Override the Pro model id."),
    verbose: bool = typer.Option(False, "--verbose", help="Show status and the rewritten SQL."),
    session: str | None = typer.Option(None, "--session", help="Reuse a conversation id."),
) -> None:
    """Chat with the retail analytics agent."""
    try:
        settings = load_settings()
        runtime = build_runtime(user=user, session_id=session, settings=settings, verbose=verbose, model=model)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]The agent could not start: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if verbose:
        runtime.deps.on_status = lambda message: console.print(f"[dim]{message}[/dim]")
    _print_banner(runtime)
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            console.print("Session closed.")
            break
        except KeyboardInterrupt:
            console.print("\nSession closed.")
            break
        if not line:
            continue
        try:
            reply = handle_message(runtime, line)
        except SessionExit:
            console.print("Session closed.")
            break
        except Exception as exc:
            trace = uuid.uuid4().hex[:12]
            runtime.deps.logger.span(
                trace_id=trace,
                span="cli",
                duration_ms=0,
                status="error",
                user_id=runtime.user_id,
                session_id=runtime.session_id,
                details={"error": str(exc)},
            )
            reply = f"Something went wrong (trace {trace}). I kept the session open."
        console.print(Markdown(reply))
        console.print()


def handle_message(runtime: Runtime, text: str) -> str:
    """Handle one user line. Never raises, except SessionExit for /exit."""
    try:
        return _handle_message(runtime, text)
    except SessionExit:
        raise
    except Exception as exc:
        trace = uuid.uuid4().hex[:12]
        runtime.deps.logger.span(
            trace_id=trace,
            span="cli",
            duration_ms=0,
            status="error",
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            details={"error": str(exc)},
        )
        safe = runtime.deps.masker.mask(str(exc)).text
        if safe and safe != str(exc):
            # The user-facing line stays generic; the masked detail is only in the log.
            pass
        return f"Something went wrong (trace {trace}). I kept the session open."


def _handle_message(runtime: Runtime, text: str) -> str:
    command, _, rest = text.partition(" ")
    if text.startswith("/"):
        return _slash(runtime, command.lower(), rest.strip())
    if _pending_interrupt(runtime):
        if _looks_like_confirmation(text):
            return _resume(runtime, text)
        _resume(runtime, "no")
    runtime.last_question = text
    return _new_turn(runtime, text)


def _slash(runtime: Runtime, command: str, rest: str) -> str:
    if command in {"/exit", "/quit"}:
        raise SessionExit()
    if command == "/help":
        return _help()
    if command == "/whoami":
        return (
            f"{runtime.display_name} ({runtime.user_id}), role {runtime.role}.\n\n"
            f"Scope: {', '.join(runtime.brands) or 'none'}.\n\n"
            f"Session: {runtime.session_id}.\n\n"
            f"Models: {runtime.settings.gemini_model_pro} / {runtime.settings.gemini_model_flash}."
        )
    if command == "/reports":
        rows = runtime.deps.reports.list_reports(runtime.user_id)
        if not rows:
            return "You have no saved reports."
        from agent.nodes.reports import format_report_list

        return format_report_list(rows)
    if command == "/save":
        return _save_last(runtime, rest)
    if command == "/restore":
        return _restore(runtime, rest)
    if command == "/trace":
        return _show_trace(runtime, rest)
    if command == "/stats":
        rows = runtime.deps.logger.session_rows(runtime.session_id)
        summary = summarize_session(rows)
        lines = [f"{key}: {value}" for key, value in summary.items()]
        return "Session metrics\n\n" + "\n".join(f"- {line}" for line in lines)
    if command == "/retry":
        if not runtime.last_question:
            return "There is no previous question to retry."
        return _new_turn(runtime, runtime.last_question)
    return "Unknown command. Type /help."


def _save_last(runtime: Runtime, title: str) -> str:
    body = runtime.last_narrative
    if not body:
        return "There's no analysis to save yet. Ask a question first."
    masked = runtime.deps.masker.mask(body).text
    report_id = runtime.deps.reports.save(
        runtime.user_id,
        runtime.session_id,
        title or _first_line(masked),
        masked,
    )
    return f"Saved as report #{report_id} \"{title or _first_line(masked)}\"."


def _restore(runtime: Runtime, rest: str) -> str:
    if rest.isdigit():
        restored = runtime.deps.reports.restore_ids(runtime.user_id, [int(rest)])
    else:
        restored = runtime.deps.reports.restore_last(runtime.user_id, within_days=runtime.settings.restore_days)
    if not restored:
        return "There is nothing recent to restore."
    listed = ", ".join(f"#{item}" for item in restored)
    return f"Restored {len(restored)} report(s): {listed}."


def _show_trace(runtime: Runtime, trace_id: str) -> str:
    if not trace_id:
        return "Usage: /trace <id>"
    rows = runtime.deps.logger.read_trace(trace_id)
    if not rows:
        return f"No events for trace {trace_id}."
    lines = [f"Trace {trace_id}"]
    for row in rows:
        details = row.get("details") or {}
        bits = [f"{row.get('span')} ({row.get('status')}, {row.get('duration_ms')} ms)"]
        if details.get("last_error"):
            bits.append(str(details["last_error"])[:180])
        if details.get("golden_ids"):
            bits.append("trios " + ", ".join(details["golden_ids"]))
        if details.get("row_count") is not None:
            bits.append(f"rows {details['row_count']}")
        if details.get("last_bytes"):
            bits.append(f"bytes {details['last_bytes']}")
        if details.get("rewritten_sql") or details.get("sql"):
            bits.append("sql " + str(details.get("rewritten_sql") or details.get("sql"))[:240])
        if details.get("attempts"):
            bits.append(f"attempt {details['attempts']}")
        lines.append("- " + " — ".join(bits))
    return "\n".join(lines)


def _new_turn(runtime: Runtime, text: str) -> str:
    trace_id = uuid.uuid4().hex[:12]
    runtime.trace_id = trace_id
    started = time.perf_counter()
    payload = {
        "user_id": runtime.user_id,
        "role": runtime.role,
        "entitled_brands": list(runtime.brands),
        "session_id": runtime.session_id,
        "user_text": text,
        "trace_id": trace_id,
        "messages": [HumanMessage(content=text)],
        "intent": "",
        "golden_trios": [],
        "plan": [],
        "current_step": 0,
        "sql": None,
        "rewritten_sql": None,
        "attempts": 0,
        "last_error": None,
        "sql_fingerprint": None,
        "stop_retry": False,
        "fatal": None,
        "pending_delete": None,
        "response": "",
        "refusal": False,
        "save_report": False,
        "empty_retried": False,
        "suspicious_retried": False,
        "retry_execute": False,
        "retry_dry": False,
        "cache_hit": False,
        "transient_tries": 0,
        "status": "",
        "last_bytes": 0,
        "pii_hits": 0,
        "scope_note": "",
    }
    interrupted = _stream(runtime, payload)
    if interrupted:
        return _with_footer(runtime, interrupted.get("prompt") or "Please confirm.", trace_id, started, {})
    values = runtime.graph.get_state(runtime.config()).values
    runtime.last_narrative = values.get("last_narrative") or values.get("response") or runtime.last_narrative
    return render_answer(runtime, values, started)


def _resume(runtime: Runtime, text: str) -> str:
    started = time.perf_counter()
    trace_id = runtime.trace_id or uuid.uuid4().hex[:12]
    interrupted = _stream(runtime, Command(resume=text))
    if interrupted:
        return _with_footer(runtime, interrupted.get("prompt") or "Please confirm.", trace_id, started, {})
    values = runtime.graph.get_state(runtime.config()).values
    runtime.last_narrative = values.get("last_narrative") or runtime.last_narrative
    return render_answer(runtime, values, started)


def _stream(runtime: Runtime, payload) -> dict | None:
    for chunk in runtime.graph.stream(payload, runtime.config(), stream_mode="updates"):
        if "__interrupt__" in chunk:
            interrupts = chunk["__interrupt__"]
            if interrupts:
                return interrupts[0].value
        elif runtime.deps.on_status:
            for name in chunk:
                runtime.deps.on_status(name)
    return None


def _pending_interrupt(runtime: Runtime) -> bool:
    try:
        snapshot = runtime.graph.get_state(runtime.config())
    except Exception:
        return False
    for task in getattr(snapshot, "tasks", None) or []:
        if getattr(task, "interrupts", None):
            return True
    return False


def _looks_like_confirmation(text: str) -> bool:
    raw = text.strip().lower()
    if raw in {"yes", "y", "no", "n", "all", "ok", "confirm", "cancel", "stop", "nope"}:
        return True
    if re.fullmatch(r"delete\s+\d+", raw):
        return True
    return re.fullmatch(r"[0-9,\s]+", raw) is not None


def render_answer(runtime: Runtime, values: dict, started: float) -> str:
    body = values.get("response") or "I don't have an answer for that."
    parts = [body]
    results = values.get("results") or []
    table = _markdown_tables(results)
    if table:
        parts.append(table)
    if results and "Definitions:" not in body:
        brands = ", ".join(values.get("entitled_brands") or runtime.brands)
        parts.append(
            "Definitions: revenue = sale_price excluding Cancelled and Returned; "
            f"margin = sale_price minus cost. Scope: {brands}."
        )
    trace = values.get("trace_id") or runtime.trace_id
    return _with_footer(runtime, "\n\n".join(parts), trace, started, values)


def _with_footer(runtime: Runtime, body: str, trace_id: str, started: float, values: dict) -> str:
    elapsed = time.perf_counter() - started
    queries = len(values.get("results") or [])
    scanned = int(values.get("bytes_total") or 0)
    timings = _node_timings(runtime, trace_id)
    footer = f"trace: {trace_id} · {queries} queries · {scanned} bytes · {elapsed:.1f}s"
    if timings:
        footer += f"\n\nnodes: {timings}"
    if runtime.verbose and (values.get("rewritten_sql") or values.get("sql")):
        footer += f"\n\n```sql\n{values.get('rewritten_sql') or values.get('sql')}\n```"
    return f"{body}\n\n{footer}"


def _markdown_tables(results: list[dict]) -> str:
    blocks = []
    for frame in results[-3:]:
        rows = frame.get("rows") or []
        columns = frame.get("columns") or (list(rows[0].keys()) if rows else [])
        if not columns:
            if frame.get("empty"):
                blocks.append(f"_{frame.get('question') or 'Query'}: no rows._")
            continue
        header = "| " + " | ".join(str(column) for column in columns) + " |"
        rule = "| " + " | ".join("---" for _ in columns) + " |"
        body = []
        for row in rows[:15]:
            body.append("| " + " | ".join(format_cell(column, row.get(column)) for column in columns) + " |")
        title = frame.get("question") or "Result"
        blocks.append(f"**{title}**\n\n" + "\n".join([header, rule, *body]))
    return "\n\n".join(blocks)


def _node_timings(runtime: Runtime, trace_id: str) -> str:
    if not trace_id:
        return ""
    parts = []
    for row in runtime.deps.logger.read_trace(trace_id):
        span = row.get("span")
        if span in {None, "turn", "cli"}:
            continue
        millis = float(row.get("duration_ms") or 0)
        label = f"{millis / 1000:.1f}s" if millis >= 1000 else f"{millis:.0f}ms"
        parts.append(f"{span} {label}")
    return ", ".join(parts)


def _first_line(text: str) -> str:
    line = next((item.strip() for item in text.splitlines() if item.strip()), "Saved analysis")
    return line[:80]


def _help() -> str:
    return "\n".join(
        [
            "Ask a business question in plain language.",
            "",
            "- /reports — list your saved reports",
            "- /save [title] — save the last answer",
            "- /restore [id] — undo the last delete, or one report",
            "- /whoami — user and brand scope",
            "- /trace <id> — spans for a turn",
            "- /stats — session metrics",
            "- /retry — run the last question again",
            "- /help",
            "- /exit",
            "",
            "Deleting reports always asks you to confirm the exact list.",
        ]
    )


def _print_banner(runtime: Runtime) -> None:
    scope = ", ".join(runtime.brands) or "none"
    body = [
        f"user: {runtime.user_id} · scope: {scope}",
        f"model: {runtime.settings.gemini_model_pro} (fallback {runtime.settings.gemini_model_flash})",
        "Type a question, or /help.",
    ]
    if not runtime.settings.google_api_key:
        body.append("GOOGLE_API_KEY is not set — analysis will explain that instead of calling Gemini.")
    if not runtime.settings.gcp_project:
        body.append("GCP_PROJECT is not set — BigQuery is disabled. Schema questions and reports still work.")
    console.print(Panel("\n".join(body), title="Retail Analyst Agent", border_style="cyan"))


if __name__ == "__main__":
    app()
