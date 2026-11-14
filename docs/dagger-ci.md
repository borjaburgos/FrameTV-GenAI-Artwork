# Dagger CI

This branch adds a Dagger pipeline alongside the existing GitHub Actions checks.
GitHub Actions remains the active remote gate. Native Dagger Cloud execution and
branch-gate cutover have not been demonstrated.

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
seconds**, about **79.0× faster**. All nine checks passed both times: 717 tests
and 22 subtests on each Python version from 3.10 through 3.14, 28 browser tests,
Ruff, the Docker CLI smoke check reporting version 0.2.0, and the generated-SDK
staleness check. The warm trace explicitly marks all eight authored check
callbacks and 16 container executions as cached; the SDK staleness check took
0.8 seconds.

These are engine-local measurements, not a general speed guarantee. The engine
used isolated user configuration with both `DAGGER_CLOUD_TOKEN` and
`DAGGER_API_TOKEN` unset. No existing global cache was pruned and no Cloud cache
was used. Raw timing and source-invalidation evidence is retained privately
outside the repository. These results do not establish native Cloud execution,
automatic push checks, or CI cutover.

## Working efficiently

Use the selected-check commands above for the inner development loop, then run
all checks before pushing. Dependencies are installed before source is copied,
so editing a test does not repeat package installation. Stable content hashes
let the engine reuse successful operations; changed inputs invalidate their
dependent work. Keep an engine's cache rather than creating an empty engine for
every edit. Once Cloud Checks is enabled, the same check definitions can run on
Cloud Engines without a second workflow implementation.

The checks make no LLM calls and require no `--allow-llm` permission. That does
not make the coding assistant's reasoning free: use the check selector and a
short failure summary instead of feeding full successful logs or generated
SDK/client source into an agent's context. The generated paths are marked as
such in `.gitattributes`; edit the authored module, not those generated files.

`dagger.lock` pins resolved images and SDK sources. The project's existing Python
dependency ranges are preserved, not converted into a complete transitive lock.
Update pins and dependency constraints deliberately, regenerate the SDK when
needed, and rerun the checks; do not equate a cached pass with a dependency audit.

## Native Cloud cutover is pending

Local checks, source-change invalidation, and engine-local cache reuse have been
demonstrated. Native Cloud execution, automatic push proof, and CI cutover are
not complete. The repository's existing
GitHub App access has been verified; no additional repository permissions were
requested.

An organization/CI token is not a user-management session. The source-management
API rejects such tokens with **“API token is not allowed for this field”**.
User-approved OAuth device authorization succeeded through the API without an
agent-operated browser. No credentials belong in pipeline inputs or this repo.

The final authenticated inventory reports Cloud caching enabled, but only
`CLOUD_MODULES` active and no `CLOUD_CHECKS` entitlement. The
[official beta.27 guard](https://github.com/dagger/dagger/blob/400b82d6f3c09ef096d2f59b3e920267b5969bf4/internal/cmd/dagger/cloud_check.go#L84)
requires both; a caching subscription alone does not satisfy it. No documented
API/CLI operation was found to activate Cloud Checks for an existing organization.
Its owner must resolve that entitlement in organization settings or with Dagger
support. No plan, billing, trial, or trigger changes were made to bypass it.

After entitlement is available, enable default-branch, pull-request **and branch
push** triggers through the supported repository-settings API. Verify all nine
checks on an actual pushed commit, then update required branch gates and retire
both Actions workflows. The release workflow also publishes multiarchitecture
images with SBOM/provenance: its replacement must preserve those capabilities
and never publish during ordinary checks. Until this verified cutover, both
existing Actions workflows remain intact to avoid losing the remote CI gate.
