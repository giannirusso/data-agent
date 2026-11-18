"""Delete confirmation: interrupt, exact ids, cancel, ownership, restore, audit."""

from agent.cli import handle_message
from agent.nodes.delete_flow import (
    digest_ids,
    intersect_candidate_ids,
    resolve_delete_answer,
)
from tests.conftest import FakeBQ, make_runtime


def _runtime(settings, **kwargs):
    return make_runtime(settings, bq=FakeBQ(), complete=None, **kwargs)


def test_yes_deletes_only_the_listed_ids_and_writes_audit(settings):
    runtime = _runtime(settings)
    repo = runtime.deps.reports
    first = repo.save("alice", "sess-test", "Q1 sales – Client X", "Notes about Client X", "")
    second = repo.save("alice", "sess-test", "Other", "No match here", "")
    bob = repo.save("bob", "sess-test", "Secret Client X", "Client X for Bob", "")

    prompt = handle_message(runtime, "Delete all reports mentioning Client X")
    assert "Found 1 report" in prompt
    assert "Q1 sales" in prompt
    assert "Secret" not in prompt
    assert "Other" not in prompt

    late = repo.save("alice", "sess-test", "Late Client X", "Client X arrived after the prompt", "")
    reply = handle_message(runtime, "yes")
    assert "Deleted 1 report" in reply
    assert "/restore" in reply

    assert repo.get("alice", first)["deleted_at"] is not None
    assert repo.get("alice", second)["deleted_at"] is None
    assert repo.get("alice", late)["deleted_at"] is None
    assert repo.get("bob", bob)["deleted_at"] is None
    audit = repo.audit_rows("alice")
    assert len(audit) == 1
    assert audit[0]["action"] == "delete"
    assert str(first) in audit[0]["report_ids"]
    assert str(late) not in audit[0]["report_ids"]


def test_numeric_subset_and_no_cancel(settings):
    runtime = _runtime(settings, session_id="sess-nums")
    repo = runtime.deps.reports
    ids = [
        repo.save("alice", "sess-nums", f"Client X {n}", f"body Client X {n}", "")
        for n in (1, 2, 3)
    ]
    prompt = handle_message(runtime, "Delete all reports mentioning Client X")
    assert "Found 3 reports" in prompt
    reply = handle_message(runtime, "1,3")
    assert "Deleted 2 reports" in reply
    assert repo.get("alice", ids[0])["deleted_at"] is not None
    assert repo.get("alice", ids[1])["deleted_at"] is None
    assert repo.get("alice", ids[2])["deleted_at"] is not None

    settings_no = settings.model_copy(
        update={
            "sqlite_path": settings.sqlite_path + ".no",
            "checkpoint_path": settings.checkpoint_path + ".no",
            "log_path": settings.log_path + ".no",
        }
    )
    runtime_no = _runtime(settings_no, session_id="sess-no")
    repo_no = runtime_no.deps.reports
    kept = repo_no.save("alice", "sess-no", "Client X keep", "Client X", "")
    handle_message(runtime_no, "Delete all reports mentioning Client X")
    cancelled = handle_message(runtime_no, "no")
    assert "cancelled" in cancelled.lower()
    assert repo_no.get("alice", kept)["deleted_at"] is None
    assert repo_no.audit_rows("alice") == []


def test_conversation_scope_and_restore(settings):
    runtime = _runtime(settings, session_id="conv-1")
    repo = runtime.deps.reports
    here = repo.save("alice", "conv-1", "This chat", "made here", "")
    elsewhere = repo.save("alice", "other-conv", "Other chat", "made elsewhere", "")
    prompt = handle_message(runtime, "Delete all the reports we made in this conversation")
    assert "Found 1 report" in prompt
    assert "This chat" in prompt
    assert "Other chat" not in prompt
    deleted = handle_message(runtime, "yes")
    assert "Deleted 1 report" in deleted
    assert repo.get("alice", here)["deleted_at"] is not None
    assert repo.get("alice", elsewhere)["deleted_at"] is None

    restored = handle_message(runtime, "/restore")
    assert "Restored 1" in restored
    assert repo.get("alice", here)["deleted_at"] is None
    actions = [row["action"] for row in repo.audit_rows("alice")]
    assert actions == ["delete", "restore"]


def test_expired_confirmation_deletes_nothing(settings):
    settings.delete_ttl_seconds = -1
    runtime = _runtime(settings, session_id="sess-exp")
    repo = runtime.deps.reports
    report_id = repo.save("alice", "sess-exp", "Client X", "Client X", "")
    handle_message(runtime, "Delete all reports mentioning Client X")
    reply = handle_message(runtime, "yes")
    assert "expired" in reply.lower()
    assert repo.get("alice", report_id)["deleted_at"] is None


def test_resolve_rejects_tampered_digest_and_bare_yes_on_large_batch():
    ids = list(range(1, 22))
    pending = {
        "ids": ids,
        "user_id": "alice",
        "digest": digest_ids("alice", ids),
        "expires_at": 1_000,
        "large_threshold": 20,
    }
    assert resolve_delete_answer(pending, "yes", now=500).action == "cancel"
    assert resolve_delete_answer(pending, "delete 21", now=500).action == "delete"
    assert resolve_delete_answer(pending, "delete 20", now=500).action == "cancel"

    tampered = dict(pending, digest="not-the-digest", large_threshold=100)
    assert resolve_delete_answer(tampered, "yes", now=500).action == "cancel"
    assert intersect_candidate_ids([1, 2, 3], [3, 9, 1]) == [3, 1]


def test_zero_matches_do_not_ask_for_confirmation(settings):
    runtime = _runtime(settings, session_id="sess-empty")
    reply = handle_message(runtime, "Delete all reports mentioning Client X")
    assert "No matching reports" in reply
    follow = handle_message(runtime, "yes")
    assert "Deleted" not in follow
