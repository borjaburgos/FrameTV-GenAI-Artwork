"""Offline contracts for GitHub-hosted CI, telemetry and credential-free PR checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github/workflows/ci.yml"
RELEASE_PATH = ROOT / ".github/workflows/release.yml"
CLOUD_GUARD = """
github.event_name != 'pull_request' ||
(github.event.pull_request.head.repo.full_name == github.repository &&
 github.event.pull_request.user.login == github.repository_owner &&
 github.actor == github.repository_owner)
"""
LOCAL_GUARD = """
github.event_name == 'pull_request' &&
(github.event.pull_request.head.repo.full_name != github.repository ||
 github.event.pull_request.user.login != github.repository_owner ||
 github.actor != github.repository_owner)
"""


def _workflow(path: Path) -> dict[str, Any]:
    # BaseLoader keeps both the key "on" and boolean-looking YAML values as strings.
    result = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(result, dict)
    return result


def _normalize(expression: str) -> str:
    return " ".join(expression.split())


def _run_step(job: dict[str, Any]) -> dict[str, Any]:
    steps = [step for step in job["steps"] if "run" in step]
    assert len(steps) == 1
    return steps[0]


class TestCIWorkflow:
    def test_ci_runs_on_main_push_every_pull_request_and_manual_dispatch(self) -> None:
        workflow = _workflow(CI_PATH)
        assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
        assert workflow["on"]["push"] == {"branches": ["main"]}
        assert workflow["on"]["pull_request"] in ("", {})
        assert workflow["on"]["workflow_dispatch"] in ("", {})
        assert workflow["permissions"] == {"contents": "read"}
        assert "env" not in workflow
        assert set(workflow["jobs"]) == {"dagger", "untrusted"}

    @pytest.mark.parametrize(("event", "same_repo", "owner_author", "owner_actor", "cloud"), [
        ("push", True, True, True, True),
        ("workflow_dispatch", True, False, False, True),
        ("pull_request", True, True, True, True),
        ("pull_request", True, True, False, False),
        ("pull_request", True, False, True, False),
        ("pull_request", True, False, False, False),
        ("pull_request", False, True, True, False),
        ("pull_request", False, False, False, False),
    ])
    def test_trust_guards_are_complementary_and_forks_always_run_locally(
        self, event: str, same_repo: bool, owner_author: bool, owner_actor: bool, cloud: bool,
    ) -> None:
        jobs = _workflow(CI_PATH)["jobs"]
        assert _normalize(jobs["dagger"]["if"]) == _normalize(CLOUD_GUARD)
        assert _normalize(jobs["untrusted"]["if"]) == _normalize(LOCAL_GUARD)
        # These truth-table cases model the exact expressions asserted above,
        # without evaluating arbitrary workflow content as Python or shell code.
        use_cloud = event != "pull_request" or (same_repo and owner_author and owner_actor)
        use_local = event == "pull_request" and (
            not same_repo or not owner_author or not owner_actor
        )
        assert use_cloud is cloud
        assert use_local is not cloud

    def test_both_trust_paths_run_the_same_unfiltered_nine_check_entrypoint(self) -> None:
        for job in _workflow(CI_PATH)["jobs"].values():
            assert job["runs-on"] == "ubuntu-latest"
            assert job["timeout-minutes"] == "30"
            assert "continue-on-error" not in job and "needs" not in job
            assert "strategy" not in job and "env" not in job
            assert len(job["steps"]) == 2
            checkouts = [step for step in job["steps"]
                         if step.get("uses", "").startswith("actions/checkout@")]
            assert len(checkouts) == 1
            assert checkouts[0]["with"]["persist-credentials"] == "false"
            runner = _run_step(job)
            assert runner["run"] == "python3 scripts/ci_github.py"
            for step in job["steps"]:
                assert "if" not in step and "continue-on-error" not in step

    def test_oidc_is_trusted_trace_only_and_both_engines_remain_local(self) -> None:
        jobs = _workflow(CI_PATH)["jobs"]
        assert jobs["dagger"]["permissions"] == {"contents": "read", "id-token": "write"}
        assert jobs["untrusted"]["permissions"] == {"contents": "read"}
        assert _run_step(jobs["dagger"])["env"] == {
            "DAGGER_ENGINE": "image://registry.dagger.io/engine:v1.0.0-beta.27",
            "DAGGER_CLOUD_TOKEN": "oidc",
            "DAGGER_CHECK_PARALLEL": "2",
        }
        assert _run_step(jobs["untrusted"])["env"] == {
            "DAGGER_ENGINE": "image://registry.dagger.io/engine:v1.0.0-beta.27",
            "DAGGER_CLOUD_TOKEN": "", "DAGGER_CHECK_PARALLEL": "2",
        }
        source = CI_PATH.read_text()
        assert "secrets." not in source
        assert "DAGGER_ENGINE: cloud" not in source
        assert "DAGGER_API_TOKEN" not in source
        assert "pull_request_target" not in source


class TestReleaseWorkflow:
    def test_release_still_verifies_before_publishing_on_tags_or_manual_dispatch(self) -> None:
        workflow = _workflow(RELEASE_PATH)
        assert workflow["on"] == {"push": {"tags": ["v*"]}, "workflow_dispatch": ""}
        assert workflow["permissions"] == {"contents": "read", "packages": "write"}
        assert set(workflow["jobs"]) == {"verify", "publish"}
        assert workflow["jobs"]["publish"]["needs"] == "verify"
        verify_runs = [step["run"] for step in workflow["jobs"]["verify"]["steps"]
                       if "run" in step]
        assert "pip install \".[dev,api,integrations]\"" in verify_runs
        assert "ruff check frameart/ tests/ scripts/" in verify_runs
        assert "pytest --tb=short" in verify_runs

    def test_release_retains_multiarchitecture_registry_sbom_and_provenance(self) -> None:
        steps = _workflow(RELEASE_PATH)["jobs"]["publish"]["steps"]
        uses = {step["uses"].split("@", 1)[0]: step for step in steps if "uses" in step}
        assert "docker/setup-qemu-action" in uses and "docker/setup-buildx-action" in uses
        assert uses["docker/login-action"]["with"] == {
            "registry": "ghcr.io", "username": "${{ github.actor }}",
            "password": "${{ secrets.GITHUB_TOKEN }}",
        }
        build = uses["docker/build-push-action"]["with"]
        assert build["context"] == "." and build["push"] == "true"
        assert set(build["platforms"].split(",")) == {"linux/amd64", "linux/arm64"}
        assert build["provenance"] == "mode=max" and build["sbom"] == "true"
        assert build["cache-from"] == "type=gha" and build["cache-to"] == "type=gha,mode=max"
        metadata = uses["docker/metadata-action"]["with"]
        assert metadata["images"] == "ghcr.io/${{ github.repository }}"
        assert "type=semver,pattern={{version}}" in metadata["tags"]
        assert "type=semver,pattern={{major}}.{{minor}}" in metadata["tags"]
        assert "type=raw,value=latest," in metadata["tags"]
        assert "type=sha" in metadata["tags"]
        assert "DAGGER_CLOUD_TOKEN" not in RELEASE_PATH.read_text()
