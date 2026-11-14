"""Exercise the pinned CLI wrapper without downloading or running a CI engine."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "ci"
PIN = "1.0.0-beta.27"


@pytest.fixture
def ci_wrapper(tmp_path):
    repository = tmp_path / "repo with spaces"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "ci"
    shutil.copyfile(WRAPPER, wrapper)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.defpath,
        "DAGGER_ENGINE": "test-only://preserved-engine",
        "CI_WRAPPER_TEST_ARGUMENTS": str(tmp_path / "arguments"),
        "CI_WRAPPER_TEST_ENGINE": str(tmp_path / "engine"),
        "CI_WRAPPER_TEST_CWD": str(tmp_path / "cwd"),
        "CI_WRAPPER_TEST_DOWNLOAD": str(tmp_path / "download"),
    }
    return repository, wrapper, fake_bin, env


def fake_cli(path: Path, version: str, *, exit_code: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '#!/bin/sh\n'
        'if [ "${1-}" = version ]; then\n'
        f'  printf "version:     v{version}\\n"\n'
        '  exit 0\n'
        'fi\n'
        'printf "%s\\0" "$@" > "$CI_WRAPPER_TEST_ARGUMENTS"\n'
        'printf "%s" "${DAGGER_ENGINE-}" > "$CI_WRAPPER_TEST_ENGINE"\n'
        'pwd > "$CI_WRAPPER_TEST_CWD"\n'
        f'exit {exit_code}\n'
    )
    path.chmod(0o755)


def test_exact_installed_cli_preserves_arguments_engine_directory_and_exit_status(ci_wrapper):
    repository, wrapper, fake_bin, env = ci_wrapper
    fake_cli(fake_bin / "dagger", PIN, exit_code=7)
    arguments = ["call", "all", "--source=.", "argument with spaces", "literal;argument"]

    result = subprocess.run(["sh", str(wrapper), *arguments], env=env, capture_output=True)

    assert result.returncode == 7
    assert Path(env["CI_WRAPPER_TEST_ARGUMENTS"]).read_bytes().split(b"\0")[:-1] == [
        argument.encode() for argument in arguments
    ]
    assert Path(env["CI_WRAPPER_TEST_ENGINE"]).read_text() == env["DAGGER_ENGINE"]
    assert Path(env["CI_WRAPPER_TEST_CWD"]).read_text().strip() == str(repository)
    assert not (repository / ".dagger").exists()


def test_wrong_installed_version_falls_back_to_exact_project_local_cli(ci_wrapper):
    repository, wrapper, fake_bin, env = ci_wrapper
    fake_cli(fake_bin / "dagger", "0.21.11", exit_code=99)
    local = repository / ".dagger" / "bin" / ("dagger-" + PIN)
    fake_cli(local, PIN)

    result = subprocess.run(["sh", str(wrapper), "call", "all"], env=env, capture_output=True)

    assert result.returncode == 0
    assert Path(env["CI_WRAPPER_TEST_ARGUMENTS"]).read_bytes() == b"call\0all\0"
    assert b"0.21.11" not in result.stderr


def test_tampered_download_is_rejected_before_extraction_or_execution(ci_wrapper):
    repository, wrapper, fake_bin, env = ci_wrapper
    fake_cli(fake_bin / "dagger", PIN + "0", exit_code=99)
    downloader = fake_bin / "curl"
    downloader.write_text(
        '#!/bin/sh\n'
        'printf "download attempted" > "$CI_WRAPPER_TEST_DOWNLOAD"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --output ]; then shift; destination=$1; fi\n'
        '  shift\n'
        'done\n'
        'printf "tampered, never executable" > "$destination"\n'
    )
    downloader.chmod(0o755)

    result = subprocess.run(["sh", str(wrapper), "call", "all"], env=env, capture_output=True)

    assert result.returncode != 0
    assert b"checksum did not match" in result.stderr
    assert Path(env["CI_WRAPPER_TEST_DOWNLOAD"]).exists()
    assert not Path(env["CI_WRAPPER_TEST_ARGUMENTS"]).exists()
    assert list((repository / ".dagger" / "bin").iterdir()) == []


def test_project_local_symlink_cache_is_refused(ci_wrapper, tmp_path):
    repository, wrapper, fake_bin, env = ci_wrapper
    fake_cli(fake_bin / "dagger", "0.21.11")
    (repository / ".dagger").symlink_to(tmp_path / "outside-cache", target_is_directory=True)

    result = subprocess.run(["sh", str(wrapper), "call", "all"], env=env, capture_output=True)

    assert result.returncode != 0
    assert b"symbolic link" in result.stderr
    assert not Path(env["CI_WRAPPER_TEST_ARGUMENTS"]).exists()
