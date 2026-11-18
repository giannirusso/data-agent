"""The CLI starts, explains missing credentials, and rejects an unknown user."""

from typer.testing import CliRunner

from agent.cli import app

runner = CliRunner()


def test_help_exits_cleanly():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "retail" in result.stdout.lower()
    assert "--user" in result.stdout


def test_unknown_user_is_a_clean_error():
    result = runner.invoke(app, ["--user", "nobody"])
    assert result.exit_code == 1
    assert "Unknown user" in result.stdout
    assert "Traceback" not in result.stdout


def test_repl_answers_schema_and_exits_without_credentials(tmp_path, monkeypatch):
    # A developer .env in the working directory must not leak into this test.
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_API_KEY=live-key\nGCP_PROJECT=live-project\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--user", "alice"], input="what data is available?\n/exit\n")
    assert result.exit_code == 0
    assert "GOOGLE_API_KEY" in result.stdout
    assert "live-key" not in result.stdout
    assert "Not available" in result.stdout
    assert "Traceback" not in result.stdout
