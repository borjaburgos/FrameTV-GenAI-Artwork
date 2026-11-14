#!/usr/bin/env python3
"""Run the repository CI once, retaining redacted logs and a GitHub summary.

The workflow chooses authentication and engine environment. This helper never
reads credential files, retries a failed Cloud run, or falls back to another
engine. It passes the inherited environment only to the repository CLI wrapper.
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import TextIO

_TRACE = re.compile(
    r"(?<![\w:/?&=.%@-])https://dagger\.cloud/"
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}/traces/[a-fA-F0-9]{32}"
    r"(?=[\s)\]>'\",;])"
)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _Redactor:
    """Redact literal tokens even when a child splits them across pipe reads."""

    def __init__(self, token: str) -> None:
        self.token = token if token and token != "oidc" else ""
        self.pending = ""

    def feed(self, text: str, *, final: bool = False) -> str:
        combined = self.pending + text
        if self.token:
            combined = combined.replace(self.token, "[REDACTED]")
        retained = 0 if final or not self.token else len(self.token) - 1
        boundary = max(0, len(combined) - retained)
        self.pending = combined[boundary:]
        return combined[:boundary]


class _TraceFinder:
    def __init__(self) -> None:
        self.pending = ""
        self.url: str | None = None

    def feed(self, text: str, *, final: bool = False) -> None:
        # The outer CLI emits its trace first. Nested logs or test fixtures must
        # not replace it with an unrelated (but syntactically valid) trace URL.
        if self.url is not None:
            return
        # Require an observed end delimiter, not the end of a pipe chunk; an
        # attacker cannot split a forged URL suffix into the next chunk.
        text = _ANSI.sub("", self.pending + text + (" " if final else ""))
        if match := _TRACE.search(text):
            self.url = match.group(0)
        self.pending = text[-1024:]


def engine_mode() -> str:
    """Describe the workflow selection without echoing URLs or credentials."""
    engine = os.environ.get("DAGGER_ENGINE", "")
    if engine == "cloud" or engine.startswith(("cloud://", "dagger-cloud://")):
        return "Cloud (OIDC)" if os.environ.get("DAGGER_CLOUD_TOKEN") == "oidc" else "Cloud"
    if engine.startswith("image://"):
        return "Local (pinned image)"
    return "Local / workflow default"


def _open_log() -> tuple[Path, TextIO]:
    preferred = os.environ.get("RUNNER_TEMP")
    parent = Path(preferred) if preferred else None
    if parent is not None and (not parent.is_dir() or parent.resolve() == Path("/")):
        parent = None
    directory = Path(tempfile.mkdtemp(prefix="frameart-dagger-ci-", dir=parent))
    path = directory / "dagger.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return path, os.fdopen(descriptor, "w", encoding="utf-8", errors="replace")


def _append_result(
    *,
    check_exit_code: int,
    elapsed: float,
    engine: str,
    trace_url: str | None,
    failed_output: bool,
) -> bool:
    """Only append controlled single-line values to GitHub's special files."""
    status = "Passed" if check_exit_code == 0 and not failed_output else "Failed"
    summary = (
        "### Dagger checks\n\n"
        f"- Status: {status} (check exit `{check_exit_code}`)\n"
        f"- Elapsed: {elapsed:.1f}s\n"
        f"- Engine: {engine}\n"
    )
    summary += (
        f"- Trace: [Open Dagger trace]({trace_url})\n"
        if trace_url
        else "- Trace: No valid Dagger Cloud trace URL was emitted.\n"
    )
    summary += "- Logs: retained under the runner temporary directory.\n"
    if failed_output:
        summary += "- Log/console output failed; this run is not considered successful.\n"
    success = True
    for variable, payload in (
        ("GITHUB_STEP_SUMMARY", summary + "\n"),
        ("GITHUB_OUTPUT", f"check_exit_code={check_exit_code}\ntrace_url={trace_url or ''}\n"),
    ):
        destination = os.environ.get(variable)
        if not destination:
            continue
        try:
            with Path(destination).open("a", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError:
            success = False
    return success


def run_checks(repository: Path) -> int:
    """Run exactly one check command; no shell, retries, or authentication changes."""
    started = time.monotonic()
    redactor = _Redactor(os.environ.get("DAGGER_CLOUD_TOKEN", ""))
    finder = _TraceFinder()
    log: TextIO | None = None
    output: TextIO | None = sys.stdout
    failed_output = False
    check_exit_code = 1
    process = None

    def emit(text: str) -> None:
        nonlocal failed_output, log, output
        finder.feed(text)
        for destination in (output, log):
            if destination is None:
                continue
            try:
                destination.write(text)
                destination.flush()
            except (OSError, UnicodeError, ValueError):
                failed_output = True
                if destination is log:
                    with suppress(OSError, ValueError):
                        log.close()
                    log = None
                else:
                    output = None

    try:
        try:
            _path, log = _open_log()
        except OSError:
            failed_output = True
        process = subprocess.Popen(
            [str(repository / "scripts" / "ci"), "--progress=plain", "-vvv", "check"],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        assert process.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        with process.stdout:
            for chunk in iter(lambda: process.stdout.read(8192), b""):
                emit(redactor.feed(decoder.decode(chunk)))
            emit(redactor.feed(decoder.decode(b"", final=True), final=True))
        check_exit_code = process.wait()
    except FileNotFoundError:
        check_exit_code = 127
    except KeyboardInterrupt:
        check_exit_code = 130
        if process is not None:
            process.terminate()
            process.wait()
    except OSError:
        check_exit_code = 126 if process is None else 1
        if process is not None:
            process.terminate()
            process.wait()
    finally:
        finder.feed("", final=True)
        if log is not None:
            try:
                log.close()
            except OSError:
                failed_output = True
        reported = _append_result(
            check_exit_code=check_exit_code,
            elapsed=time.monotonic() - started,
            engine=engine_mode(),
            trace_url=finder.url,
            failed_output=failed_output,
        )
        failed_output = failed_output or not reported
    # A tee/reporting error may fail a successful run, never hide a check failure.
    if check_exit_code:
        return check_exit_code if check_exit_code > 0 else 128 - check_exit_code
    return 1 if failed_output else 0


def main() -> int:
    return run_checks(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    raise SystemExit(main())
