# Dagger CI with GitHub Actions

GitHub Actions remains the CI trigger and remote gate. Its jobs run the shared
Dagger pipeline: trusted jobs are configured to use Cloud Engines for cache and
traces; fork and other untrusted pull requests use a local engine without Cloud
credentials. Actual GitHub OIDC eligibility, remote execution, and cross-run
Cloud cache reuse are still awaiting a verified GitHub run.

These are separate capabilities:

| Component | Role in this setup |
| --- | --- |
| GitHub Actions | Trigger main-branch pushes, pull requests, and manual runs; report required checks |
| Dagger Cloud Engines | Intended remote execution, persistent engine cache, and execution traces for trusted jobs |
| Native Dagger Cloud Checks | Dagger-managed triggers/checks; off and not required for this GitHub-triggered design |

The release workflow and image publisher are unchanged. Ordinary checks do not
publish images or require production credentials.

## Pinned CLI

`scripts/ci` runs Dagger **1.0.0-beta.27**, the version currently documented by the
[official installation guide](https://docs.dagger.io/getting-started/install/).
This is the documented beta, distinct from the stable
[0.21.11 release](https://github.com/dagger/dagger/releases/tag/v0.21.11). The
new workspace/module scaffold uses the beta interface; an installed stable CLI
is not silently substituted.

The wrapper first checks the installed CLI for an exact version match. Otherwise
it uses a matching project-local binary in `.dagger/bin/`, or downloads the pinned
Linux amd64 archive directly from the official release endpoint. The archive must
match this repository-pinned SHA-256 before extraction:

```text
de2a3ca569c3a955d5ce98d2c4879b67a4459c394a7e1684261ba7152bdb6865
```

The archive and checksum are published at
[Dagger's official release directory](https://dl.dagger.io/dagger/releases/1.0.0-beta.27/checksums.txt).
No remote installer is piped into a shell, no global binary is replaced, and no
credential file is read or logged. Bootstrap downloads require `curl`,
`sha256sum`, `tar`, and `mktemp`. Other platforms can use an already-installed
exact-version CLI; automatic bootstrap is currently Linux amd64 only.

Arguments are passed unchanged to Dagger from the repository root. The existing
environment, including `DAGGER_ENGINE`, is preserved:

```sh
scripts/ci version
scripts/ci check -l --all --format cli
scripts/ci check
```

The list command prints the exact selectors exposed by the workspace. To run a
single authored check, use the module and check filters:

```sh
scripts/ci check --frameart-ci --check=python312
scripts/ci check --frameart-ci --check=ruff
scripts/ci check --frameart-ci --check=browser
scripts/ci check --frameart-ci --check=docker
```

`scripts/ci check` runs all nine discovered checks: the eight FrameArt checks
below and the official Python SDK check for stale generated code. The generated
SDK and client sources remain committed and are marked as generated in GitHub.

## Local and Cloud Engine commands

With no `DAGGER_ENGINE` override, the normal local entry point is:

```sh
scripts/ci check
```

An explicitly selected Cloud Engine requires compute authorization. For a
separately provisioned **ComputeToken**, supply its value through a trusted
environment variable, not a source file or command-line argument:

```sh
DAGGER_ENGINE=cloud DAGGER_CLOUD_TOKEN="$DAGGER_COMPUTE_TOKEN" scripts/ci check
```

`DAGGER_COMPUTE_TOKEN` above is an example variable supplied by the caller; the
wrapper does not read a token store. The two available organization tokens were
verified as telemetry tokens, and Cloud Engine provisioning rejected them with
an invalid-token-type error. A telemetry token is not a ComputeToken. Merely
attaching a telemetry token to a local engine does not establish remote compute
or shared Cloud cache reuse.

The GitHub trusted job instead uses the supported first-party OIDC route:

```yaml
permissions:
  contents: read
  id-token: write
# Set only on the Dagger step of the trusted job:
env:
  DAGGER_ENGINE: cloud
  DAGGER_CLOUD_TOKEN: oidc
```

`oidc` is a literal selector for short-lived GitHub identity, not a stored secret.
No personal OAuth session or API key is saved as a GitHub secret. Successful
organization/repository eligibility and Cloud Engine provisioning must still be
proved by an actual trusted GitHub run; this configuration alone is not proof.

## GitHub trust and failure handling

One selected job runs all nine checks through the same pinned `scripts/ci`.
The trusted Cloud job receives only job-scoped `contents: read` and
`id-token: write` permissions. Fork and other untrusted pull-request jobs use a
local engine, have no Cloud credentials, and do not receive OIDC-token write
permission. They run the same complete check suite, not a reduced substitute.

This separates the ordinary untrusted-PR path; it is not a security boundary
against a malicious same-repository collaborator who can change a trusted
workflow or its caller. Repository write access and workflow changes must remain
trusted and reviewed. Never run untrusted source with Cloud identity merely to
obtain a cache hit.

The GitHub helper preserves Dagger's exit status and writes the run summary and
any available trace link even when checks fail. A missing trace on an
authentication/provisioning failure must not hide the failure or turn the job
green. Trace links, execution results, and a second unchanged Cloud run will be
recorded after remote verification; no Cloud performance claim is established
yet.

## Check coverage

The eight authored checks cover the existing remote gate. Each selector works
with `scripts/ci check --frameart-ci --check=SELECTOR`:

| Check | Selector | Behavior |
| --- | --- | --- |
| Python 3.10 | `python310` | Install development/API/integration dependencies and run the backend tests |
| Python 3.11 | `python311` | Run the same backend tests on 3.11 |
| Python 3.12 | `python312` | Run the same backend tests on 3.12 |
| Python 3.13 | `python313` | Run the same backend tests on 3.13 |
| Python 3.14 | `python314` | Run the same backend tests on 3.14 |
| Lint | `ruff` | Check application, tests, Python scripts, and the authored Dagger module |
| Browser | `browser` | Use the pinned Playwright image and npm lockfile to run the browser suite against an isolated local server |
| Docker | `docker` | Build the production Dockerfile and verify that `frameart --version` loads |

No check should use production credentials, connect to household TVs, write to
ResourceSpace, or publish an image. The Docker check is a build and CLI smoke
check; image publishing is outside this change.

## Inputs, outputs, and cache boundaries

The authored module binds an immutable `Directory` as its `source` constructor
input using `DefaultPath("/")` for the workspace root and `Ignore(_CONTEXT_IGNORE)`
for the input boundary. The source snapshot is therefore part of the check
object's cache key before a callback runs, rather than an ambient workspace read
hidden inside a cached callback. Each check narrows that snapshot with explicit
source allowlists, additional exclusion patterns, and Git ignore filtering.
Local `.env` files, `config.yaml`, tokens, secrets,
production data, `.e2e-data`, Git metadata, downloaded CLI binaries, and exported
reports must not become pipeline inputs. `.dagger/bin/` and
`dagger-ci-artifacts/` are ignored by Git. The production Docker context also
excludes the Dagger workspace and CI artifacts; Git archive attributes exclude
local cache, report, and data paths.

Dependency constraints are derived from `pyproject.toml` and installed before
application and test source is copied in. Source-only edits therefore leave the
dependency installation inputs unchanged. pip uses a per-Python-version download
cache; npm uses its own cache and installs from `package-lock.json`. The browser
server and its data live inside the test container. Host application settings,
TV pairing tokens, and Docker sockets are not mounted into the checks.

These ignore rules support the boundary; the module's explicit input selection
remains the control to inspect before enabling hosted execution. Reports belong
in the ignored `dagger-ci-artifacts/` directory if explicitly exported; the
checks do not publish artifacts or images automatically.

## Verified source invalidation and engine-local cache

Two real source-change probes verified the explicit constructor input binding.
Introducing a syntax error into tracked application source made Ruff fail;
restoring it returned green. Adding a tracked failing test produced exactly one
failure alongside 717 passing tests; restoring it returned 717 passing tests.
Both probe edits were removed, and the original files have no remaining diff.
An earlier timing pair was discarded because its ambient workspace reads did not
invalidate a cached callback; it is not used as performance evidence here.

With the corrected binding, the same command was run twice against unchanged
source on a fresh, dedicated engine and empty engine cache:

```sh
scripts/ci --progress=plain -vvv check
```

The cold run took **79.498 seconds**; the immediate warm repeat took **1.006
seconds**, about **79.0× faster**. At this measurement, all nine checks passed
both times: 717 tests
and 22 subtests on each Python version from 3.10 through 3.14, 28 browser tests,
Ruff, the Docker CLI smoke check reporting version 0.2.0, and the generated-SDK
staleness check. The warm trace explicitly marks all eight authored check
callbacks and 16 container executions as cached; the SDK staleness check took
0.8 seconds.

These are engine-local measurements, not a general speed guarantee. The engine
used isolated user configuration with both `DAGGER_CLOUD_TOKEN` and
`DAGGER_API_TOKEN` unset. No existing global cache was pruned and no Cloud cache
was used. Raw timing and source-invalidation evidence is retained privately
outside the repository. These results do not establish Cloud Engine performance,
GitHub-triggered remote execution, automatic native Cloud Checks, or CI cutover.

## Working efficiently

Use the selected-check commands above for the inner development loop, then run
all checks before pushing. Dependencies are installed before source is copied,
so editing a test does not repeat package installation. Stable content hashes
let the engine reuse successful operations; changed inputs invalidate their
dependent work. Keep an engine's cache rather than creating an empty engine for
every edit. The same check definitions can run on a Cloud Engine from GitHub
Actions without enabling native Cloud Checks or duplicating the pipeline.

The checks make no LLM calls and require no `--allow-llm` permission. That does
not make the coding assistant's reasoning free: use the check selector and a
short failure summary instead of feeding full successful logs or generated
SDK/client source into an agent's context. The generated paths are marked as
such in `.gitattributes`; edit the authored module, not those generated files.

`dagger.lock` pins resolved images and SDK sources. The project's existing Python
dependency ranges are preserved, not converted into a complete transitive lock.
Update pins and dependency constraints deliberately, regenerate the SDK when
needed, and rerun the checks; do not equate a cached pass with a dependency audit.

## Current proof and optional native Cloud Checks

Local checks, tracked-source invalidation, and engine-local warm reuse have been
demonstrated. The GitHub-triggered Cloud Engine route is configured but remains
pending remote OIDC, trace, and cache evidence. GitHub Actions stays the trigger
and gate; there is no native-check cutover in this change.

Native Cloud Checks remains off. The final authenticated inventory reports
Cloud caching enabled but no `CLOUD_CHECKS` entitlement. The
[official beta.27 guard](https://github.com/dagger/dagger/blob/400b82d6f3c09ef096d2f59b3e920267b5969bf4/internal/cmd/dagger/cloud_check.go#L84)
for native checks requires both `CLOUD_MODULES` and `CLOUD_CHECKS`; a caching
subscription alone does not satisfy it. That native-check entitlement is not a
prerequisite for the GitHub-triggered Cloud Engine design. No plan, billing,
trial, or native trigger changes were made to bypass it.

User-approved OAuth device authorization worked for management reads, but it is
not used by the pipeline. Organization/API tokens were rejected by the
source-management API with **“API token is not allowed for this field”**. Neither
management authorization nor telemetry-token availability proves compute access.
No credentials belong in pipeline inputs or this repository.

Native Dagger-managed triggers could be evaluated separately in the future after
entitlement and branch-gate verification. They are optional, not the next blocker
for this setup. The existing release workflow still publishes multiarchitecture
images with SBOM/provenance and is deliberately outside this change.
