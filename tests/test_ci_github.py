"""Offline GitHub CI helper tests; the fake CLI never runs Dagger or a network."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from scripts import ci_github

TRACE = "https://dagger.cloud/household-ci/traces/0123456789abcdef0123456789abcdef"


@pytest.fixture
def runner(tmp_path, monkeypatch):
    repository = tmp_path / "repo with spaces"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    temporary = tmp_path / "runner-temp"
    temporary.mkdir()
    record = tmp_path / "execution.json"
    cli = scripts / "ci"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "record = Path(os.environ['FAKE_RECORD'])\n"
        "calls = record.with_suffix('.calls')\n"
        "with calls.open('a') as destination: destination.write('run\\n')\n"
        "record.write_text(json.dumps({'args':sys.argv[1:], 'cwd':str(Path.cwd()),\n"
        " 'marker':os.environ.get('INHERITED_MARKER'), 'engine':os.environ.get('DAGGER_ENGINE'),\n"
        " 'token_present':bool(os.environ.get('DAGGER_CLOUD_TOKEN'))}))\n"
        "sys.stdout.write(os.environ.get('FAKE_STDOUT', ''))\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write(os.environ.get('FAKE_STDERR', ''))\n"
        "sys.stderr.flush()\n"
        "raise SystemExit(int(os.environ.get('FAKE_EXIT', '0')))\n"
    )
    cli.chmod(0o755)
    values = {
        "RUNNER_TEMP": str(temporary),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "FAKE_RECORD": str(record),
        "INHERITED_MARKER": "preserve-this",
        "DAGGER_ENGINE": "cloud",
        "DAGGER_CLOUD_TOKEN": "oidc",
        "FAKE_STDOUT": "",
        "FAKE_STDERR": "",
        "FAKE_EXIT": "0",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return repository, temporary, record, values


@pytest.mark.parametrize("exit_code", [0, 7, 42])
def test_exact_command_environment_directory_and_failure_are_preserved(
    runner, monkeypatch, capsys, exit_code
):
    repository, temporary, record, values = runner
    monkeypatch.setenv("FAKE_EXIT", str(exit_code))
    monkeypatch.setenv("FAKE_STDOUT", "checks started\n")
    monkeypatch.setenv("FAKE_STDERR", "trace: " + TRACE + "\n")
    assert ci_github.run_checks(repository) == exit_code
    executed = json.loads(record.read_text())
    assert executed == {
        "args": ["--progress=plain", "-vvv", "check"],
        "cwd": str(repository),
        "marker": "preserve-this",
        "engine": "cloud",
        "token_present": True,
    }
    assert record.with_suffix(".calls").read_text() == "run\n"  # Never retry/fallback.
    captured = capsys.readouterr().out
    assert "checks started" in captured and TRACE in captured
    logs = list(temporary.glob("frameart-dagger-ci-*/dagger.log"))
    assert len(logs) == 1
    assert logs[0].read_text() == captured
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert logs[0].parent.stat().st_mode & 0o777 == 0o700
    summary = Path(values["GITHUB_STEP_SUMMARY"]).read_text()
    assert ("Status: Passed" if exit_code == 0 else "Status: Failed") in summary
    assert f"check exit `{exit_code}`" in summary
    assert "Elapsed:" in summary and "Cloud (OIDC)" in summary
    assert TRACE in summary
    outputs = Path(values["GITHUB_OUTPUT"]).read_text()
    assert f"check_exit_code={exit_code}\n" in outputs
    assert f"trace_url={TRACE}\n" in outputs


@pytest.mark.parametrize(
    "url",
    [
        "http://dagger.cloud/org/traces/0123456789abcdef0123456789abcdef",
        "https://dagger.cloud.evil.example/org/traces/0123456789abcdef0123456789abcdef",
        "https://dagger.cloud@evil.example/org/traces/0123456789abcdef0123456789abcdef",
        "https://evil.example/?next=" + TRACE,
        TRACE + "/more",
        TRACE + "?token=secret",
        TRACE + "#fragment",
        TRACE + "f",
        "https://dagger.cloud/your-org/setup",
        "https://dagger.cloud/your-org/traces",
    ],
)
def test_forged_hosts_incomplete_trace_and_setup_marketing_links_are_rejected(
    runner, monkeypatch, url
):
    repository, _temporary, _record, values = runner
    monkeypatch.setenv("FAKE_STDOUT", "View " + url + "\n")
    assert ci_github.run_checks(repository) == 0
    assert "No valid Dagger Cloud trace URL" in Path(values["GITHUB_STEP_SUMMARY"]).read_text()
    assert "trace_url=\n" in Path(values["GITHUB_OUTPUT"]).read_text()


def test_trace_split_at_pipe_boundary_requires_real_end_delimiter():
    finder = ci_github._TraceFinder()
    finder.feed(TRACE)
    assert finder.url is None
    finder.feed(".evil/path\n")
    assert finder.url is None
    finder.feed("\x1b[32m" + TRACE[:40])
    finder.feed(TRACE[40:] + "\x1b[0m\n")
    assert finder.url == TRACE


def test_trace_without_trailing_newline_is_detected_at_real_eof(runner, monkeypatch):
    repository, _temporary, _record, values = runner
    monkeypatch.setenv("FAKE_STDOUT", TRACE)
    assert ci_github.run_checks(repository) == 0
    assert TRACE in Path(values["GITHUB_STEP_SUMMARY"]).read_text()


def test_first_outer_trace_cannot_be_replaced_by_nested_logs_or_test_fixtures():
    other = "https://dagger.cloud/nested/traces/ffffffffffffffffffffffffffffffff"
    finder = ci_github._TraceFinder()
    finder.feed(TRACE + "\n" + other + "\n")
    finder.feed(other + "\n")
    assert finder.url == TRACE


def test_literal_token_redacted_from_both_streams_and_retained_log(runner, monkeypatch, capsys):
    repository, temporary, record, values = runner
    token = "synthetic-secret-cloud-token"
    monkeypatch.setenv("DAGGER_CLOUD_TOKEN", token)
    monkeypatch.setenv("FAKE_STDOUT", "stdout token=" + token + "\n")
    monkeypatch.setenv("FAKE_STDERR", "stderr token=" + token + "\n" + TRACE + "\n")
    assert ci_github.run_checks(repository) == 0
    outputs = [
        capsys.readouterr().out,
        *[path.read_text() for path in temporary.rglob("dagger.log")],
        Path(values["GITHUB_STEP_SUMMARY"]).read_text(),
        Path(values["GITHUB_OUTPUT"]).read_text(),
    ]
    assert all(token not in value for value in outputs)
    assert "[REDACTED]" in outputs[0]
    assert json.loads(record.read_text())["token_present"] is True
    redactor = ci_github._Redactor(token)
    merged = "".join(
        redactor.feed(piece) for piece in ["before synthetic-", "secret-cloud-", "token after"]
    )
    merged += redactor.feed("", final=True)
    assert merged == "before [REDACTED] after"


@pytest.mark.parametrize("child_exit", [0, 17])
def test_log_tee_failure_is_failure_and_never_masks_child_nonzero(runner, monkeypatch, child_exit):
    repository, _temporary, _record, values = runner
    monkeypatch.setenv("FAKE_EXIT", str(child_exit))
    monkeypatch.setenv("FAKE_STDOUT", "synthetic log output\n")

    class BrokenLog(io.StringIO):
        def write(self, value):
            raise OSError("synthetic disk-full failure")

    monkeypatch.setattr(ci_github, "_open_log", lambda: (Path("unused"), BrokenLog()))
    assert ci_github.run_checks(repository) == (child_exit or 1)
    summary = Path(values["GITHUB_STEP_SUMMARY"]).read_text()
    assert "Status: Failed" in summary
    assert "output failed" in summary


def test_missing_cli_still_appends_failure_summary_without_fallback(runner):
    repository, _temporary, record, values = runner
    (repository / "scripts/ci").unlink()
    assert ci_github.run_checks(repository) == 127
    assert "check exit `127`" in Path(values["GITHUB_STEP_SUMMARY"]).read_text()
    assert not record.exists()


def test_summary_append_preserves_prior_content_and_uses_safe_engine_label(runner, monkeypatch):
    repository, _temporary, _record, values = runner
    summary = Path(values["GITHUB_STEP_SUMMARY"])
    summary.write_text("Earlier step\n")
    monkeypatch.setenv("DAGGER_ENGINE", "image://registry.dagger.io/engine:v1.0.0-beta.27")
    monkeypatch.setenv("DAGGER_CLOUD_TOKEN", "")
    assert ci_github.run_checks(repository) == 0
    result = summary.read_text()
    assert result.startswith("Earlier step\n")
    assert "Local (pinned image)" in result
    assert "registry.dagger.io" not in result


def test_helper_never_reads_credential_files_or_uses_network(runner, monkeypatch):
    repository, _temporary, _record, _values = runner
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Helper must not read files or contact a network")

    original_open = Path.open

    def writes_only(path, mode="r", *args, **kwargs):
        if not mode.startswith(("w", "a")):
            forbidden()
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "open", writes_only)
        context.setattr(socket, "socket", forbidden)
        assert ci_github.run_checks(repository) == 0
