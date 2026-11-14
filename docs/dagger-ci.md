# Dagger checks on GitHub runners

GitHub Actions triggers and executes CI. A pinned Dagger engine runs on the
GitHub-hosted runner; trusted runs upload telemetry to Dagger Cloud and put a
trace link in the job summary. Fork and other untrusted PRs run the same checks
without Cloud credentials.

**There is no shared cross-run cache in this setup.** Dagger can reuse operations
within a job, and a developer's persistent local engine can reuse previous runs.
A fresh GitHub-hosted runner starts a fresh engine cache. Cloud Engines and native
Cloud Checks are off and are not required for this temporary configuration.

The release workflow and image publisher are unchanged. Ordinary CI never
publishes images or connects to production TVs, ResourceSpace, or the NAS.

## Running checks

From the repository root:

```sh
scripts/ci version
scripts/ci check -l --all --format cli
scripts/ci check
```

For a faster development loop, select one check:

```sh
scripts/ci check --frameart-ci --check=python312
scripts/ci check --frameart-ci --check=ruff
scripts/ci check --frameart-ci --check=browser
scripts/ci check --frameart-ci --check=docker
```

The full command runs all nine checks, including the official SDK staleness
check. CI checks generated code as committed; it does not regenerate first and
silently hide stale output.

| Check | Selector | Coverage |
| --- | --- | --- |
| Python 3.10–3.14 | `python310` through `python314` | Complete backend suite with development/API/integration dependencies |
| Lint | `ruff` | Application, tests, Python scripts, authored Dagger module |
| Browser | `browser` | Pinned Playwright image, npm lockfile, isolated loopback API |
| Container | `docker` | Production Dockerfile and `frameart --version` smoke test |
| Generated code | `--python-sdk` module filter | Official generated SDK/client staleness validation |

## Pinned tools

`scripts/ci` uses **1.0.0-beta.27**, the version in the
[official installation guide](https://docs.dagger.io/getting-started/install/),
and the workspace pins Python SDK module **v1.8.0**. This is the documented beta,
not the older stable CLI. The wrapper accepts only an exact installed version,
otherwise bootstrapping Linux amd64 into ignored `.dagger/bin/`.

The official archive must match the repository's SHA-256 before extraction:

```text
de2a3ca569c3a955d5ce98d2c4879b67a4459c394a7e1684261ba7152bdb6865
```

See [official checksums](https://dl.dagger.io/dagger/releases/1.0.0-beta.27/checksums.txt).
No installer is piped into a shell, global tool is replaced, or credential file
is read by the wrapper. Bootstrap requires `curl`, `sha256sum`, `tar`, and
`mktemp`. Other platforms can provide an already-installed exact-version CLI.

`dagger.lock` pins resolved images and SDK sources. Python dependency ranges
remain those in `pyproject.toml`, not a complete transitive lock. Update pins
and dependencies deliberately; a cached pass is not a dependency audit.

## GitHub configuration and trust

The workflow runs on pushes to `main`, pull requests, and manual dispatches. One
of two jobs runs the complete pipeline through `python3 scripts/ci_github.py`:

- Trusted pushes/manual runs and the owner's same-repository PRs use short-lived
  GitHub OIDC identity for telemetry (`DAGGER_CLOUD_TOKEN=oidc`).
- Fork PRs and other untrusted PRs receive an empty token and still run every
  check. Missing credentials never skip tests or make a test failure optional.

Both jobs use `contents: read`, checkout without persisted credentials, the
same pinned local engine image, and at most two concurrent checks. Only the
trusted job has `id-token: write`, to authenticate trace upload; it does not
request Cloud compute. Neither job has package-write or check-write permissions.
There is no `pull_request_target` execution path or personal OAuth credential.

No saved token was exported to GitHub, and no repository secret is required.
`oidc` is a literal authentication selector, not a stored credential. The CLI
obtains short-lived identity through the supported GitHub/Dagger exchange.
Authentication is step-scoped, outside source snapshots and test-container
environments. Do not print tokens, dump the environment, or archive a whole
engine state as a workaround for unavailable remote caching.

Routing untrusted PRs separately is not a security boundary against a malicious
same-repository writer who can edit trusted workflows and request identity.
Repository write access and workflow changes must remain trusted and reviewed.

## Logs, failures and traces

`scripts/ci_github.py` streams merged, redacted output into the GitHub job log and
keeps a private temporary copy under `RUNNER_TEMP`. That temporary copy is not a
persistent artifact after the runner is discarded. The helper always appends
status, elapsed time, engine mode, and any valid outer Dagger trace link to the
job summary, including on failure. Nested output cannot replace the outer trace.

The check's nonzero exit code is preserved. A failed Cloud connection or a
log/summary write error cannot turn a failed check green. A printed trace URL is
not, on its own, proof of successful execution or receipt by the Cloud service;
hosted execution and telemetry are verified separately.

The GitHub-hosted verification run and its trace will be linked here after they
pass. No hosted trace or cross-run cache success is claimed in advance.

## Source and cache boundaries

An immutable constructor `Directory`, bound by `DefaultPath("/")` and
`Ignore(_CONTEXT_IGNORE)`, makes source content part of every check's cache key.
Each check further filters its inputs. Secrets, real `config.yaml`, `.env`, TV
tokens, user artwork/data, CLI binaries, and exported reports are excluded.

Dependencies are installed before application/test source is copied, so normal
source edits do not invalidate package installation. pip uses per-Python caches;
npm uses its own cache and `package-lock.json`. All test executions explicitly
disable nested Dagger API access. Test servers and data live in ephemeral
containers, not host or production storage.

Within one GitHub job these layers can be reused by independent checks. On a
developer's persistent engine they also survive subsequent invocations. They
do **not** survive separate GitHub-hosted jobs in this configuration, and the
telemetry token does not enable a remote-cache service automatically.

### Verified local benchmark

Tracked-source invalidation was tested by introducing an application syntax
error and a failing test. Both correctly turned their check red; restoring the
files restored green. All temporary edits were removed. An earlier ambient-read
implementation was discarded after failing this test.

With the corrected binding, unchanged source passed all nine checks on a fresh
dedicated local engine in **79.498 seconds**, then **1.006 seconds** on immediate
repeat (about **79×**). That historical pair covered 717 backend tests plus 22
subtests per Python version, 28 browser tests, lint, Docker, and generated code.
The warm run explicitly reused eight check callbacks and 16 container operations.

Both Cloud tokens were unset and user configuration was isolated. No global
cache was pruned. These are engine-local results, not a claim about separate
GitHub runner performance. Raw evidence is retained privately outside the repo.

The checks make no LLM calls. Selected checks and concise failure summaries can
reduce log-reading and agent context, but do not make assistant reasoning free.
Edit the authored module, not generated SDK/client files.

## Deferred Cloud features

The attempted GitHub OIDC Cloud run authenticated but provisioning returned
`This org does not have access to CLOUD_ENGINES`. Native triggers also lack the
separate `CLOUD_CHECKS` entitlement. Neither feature is used by the current setup;
no plan, billing, trial, or entitlement changes are made to bypass those gates.

Cloud Engine compute authorization is distinct from a telemetry token. The
existing saved tokens were rejected for compute provisioning. If access becomes
available, Cloud execution and shared cache reuse must be verified with separate
hosted runs before changing the workflow or claiming remote-cache speedups.
The existing multiarchitecture release publisher, SBOM, and provenance remain
outside this CI change.
