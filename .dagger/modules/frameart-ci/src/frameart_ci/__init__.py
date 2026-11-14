"""Independent, source-filtered FrameArt checks with reusable dependency layers.

No check receives host credentials, user configuration, TV tokens, artwork
libraries, Docker sockets, or publishing privileges. The engine only needs
ordinary package/image downloads; browser traffic targets its own test server.
"""

from __future__ import annotations

from typing import Annotated, Any

import tomllib
from dagger import DefaultPath, Ignore, check, field, function, object_type
from dagger_clients.core import Container, Directory, core

_WORKDIR = "/tmp/frameart-ci"
_PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright:v1.62.1-noble"
_APP_INPUTS = ["pyproject.toml", "README.md", "LICENSE", "config.example.yaml", "frameart/**"]
_MODULE_INPUTS = [".dagger/modules/frameart-ci/src/**"]
_TEST_INPUTS = [
    "tests/**/*.py",
    "tests/fixtures/**",
    "scripts/**/*.py",
    "scripts/**/*.sh",
    "scripts/ci",
    "deploy/resourcespace/compose.yaml",
    "deploy/resourcespace/pilot-entrypoint.sh",
    "deploy/resourcespace/safety.php",
    *_MODULE_INPUTS,
]
_BROWSER_INPUTS = ["tests/e2e/**", "playwright.config.js"]
_NPM_INPUTS = ["package.json", "package-lock.json"]
# Apply explicit exclusions in addition to allowlists and Git ignore rules.
# Bundled frameart/static quiz assets are source code assets, not user artwork.
_EXCLUDE_INPUTS = [
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.token",
    "**/.env",
    "**/.env.*",
    "**/config.yaml",
    "**/secrets/**",
    "**/artifacts/**",
    "**/artwork/**",
    "**/data/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/*.egg-info/**",
    "**/playwright-report/**",
    "**/test-results/**",
]
# DefaultPath is an explicit immutable constructor argument, not an ambient
# workspace lookup hidden inside a cached check. Include only CI inputs at the
# context boundary, then narrow further per check below. Exclusions are last so
# a source-directory allowlist cannot accidentally read user credentials/data.
_CONTEXT_IGNORE = [
    "**",
    *(
        "!" + pattern
        for pattern in dict.fromkeys(
            [
                *_APP_INPUTS,
                *_TEST_INPUTS,
                *_BROWSER_INPUTS,
                *_NPM_INPUTS,
                "Dockerfile",
                ".dockerignore",
            ]
        )
    ),
    *_EXCLUDE_INPUTS,
]


def _source(source: Directory, include: list[str]) -> Directory:
    """Filter the explicit repository-root snapshot without ambient reads."""
    return source.filter(include=include, exclude=_EXCLUDE_INPUTS, gitignore=True)


async def _project(source: Directory) -> dict[str, Any]:
    metadata = _source(source, ["pyproject.toml"])
    return tomllib.loads(await metadata.file("pyproject.toml").contents())


async def _requirements(source: Directory, extras: tuple[str, ...]) -> str:
    """Derive dependency constraints from project metadata, without importing it."""
    metadata = await _project(source)
    requirements = [*metadata["build-system"]["requires"], *metadata["project"]["dependencies"]]
    for extra in extras:
        requirements.extend(metadata["project"]["optional-dependencies"][extra])
    return "\n".join(dict.fromkeys(requirements)) + "\n"


def _isolated(container: Container, *, python: str) -> Container:
    return (
        container.with_workdir(_WORKDIR)
        .with_env_variable("CI", "true")
        .with_env_variable("PYTHONDONTWRITEBYTECODE", "1")
        .with_env_variable("PYTHONUNBUFFERED", "1")
        .with_env_variable("FRAMEART_CONFIG", "/tmp/frameart-ci-config/absent.yaml")
        .with_env_variable("FRAMEART_DATA_DIR", "/tmp/frameart-ci-data")
        .with_env_variable("FRAMEART_E2E_PYTHON", python)
        .with_env_variable("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    )


async def _python_dependencies(
    source: Directory,
    container: Container,
    *,
    version: str,
    python: str,
    extras: tuple[str, ...],
) -> Container:
    # No app/test source is present at this step: ordinary edits cannot
    # invalidate dependency installation. pip's download/wheel cache persists.
    return (
        _isolated(container, python=python)
        .with_mounted_cache("/root/.cache/pip", core().cache_volume(f"frameart-ci-pip-{version}"))
        .with_new_file("/tmp/frameart-ci-requirements.txt", await _requirements(source, extras))
        .with_exec(
            [python, "-m", "pip", "install", "-r", "/tmp/frameart-ci-requirements.txt"],
            disable_dagger_in_dagger=True,
        )
    )


def _install_application(source: Directory, container: Container, *, python: str) -> Container:
    # hatchling is already in the dependency layer. Build/install the actual
    # package without resolving dependencies again or starting build isolation.
    return container.with_directory(_WORKDIR, _source(source, _APP_INPUTS)).with_exec(
        [python, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "."],
        disable_dagger_in_dagger=True,
    )


async def _pytest(source: Directory, version: str) -> None:
    base = (
        core()
        .container()
        .from_(f"python:{version}-slim")
        .with_exec(
            [
                "sh",
                "-ec",
                "apt-get update && apt-get install -y --no-install-recommends "
                "fonts-dejavu-core && rm -rf /var/lib/apt/lists/*",
            ],
            disable_dagger_in_dagger=True,
        )
    )
    dependencies = await _python_dependencies(
        source,
        base,
        version=version,
        python="python",
        extras=("dev", "api", "integrations"),
    )
    application = _install_application(source, dependencies, python="python")
    await (
        application.with_directory(_WORKDIR, _source(source, _TEST_INPUTS))
        .with_exec(["python", "-m", "pytest", "--tb=short", "-v"], disable_dagger_in_dagger=True)
        .sync()
    )


@object_type
class FrameartCi:
    """Eight independent checks; no publishing, deployment, or credential APIs."""

    source: Annotated[Directory, DefaultPath("/"), Ignore(_CONTEXT_IGNORE)] = field()

    @function
    @check
    async def ruff(self) -> None:
        """Lint application, tests, scripts and the authored Dagger module."""
        metadata = await _project(self.source)
        requirement = next(
            value
            for value in metadata["project"]["optional-dependencies"]["dev"]
            if value.split(">", 1)[0].split("=", 1)[0].split("<", 1)[0] == "ruff"
        )
        container = (
            _isolated(core().container().from_("python:3.12-slim"), python="python")
            .with_mounted_cache("/root/.cache/pip", core().cache_volume("frameart-ci-pip-3.12"))
            .with_exec(
                ["python", "-m", "pip", "install", requirement], disable_dagger_in_dagger=True
            )
            .with_directory(
                _WORKDIR,
                _source(
                    self.source,
                    [
                        "pyproject.toml",
                        "frameart/**",
                        *_TEST_INPUTS,
                    ],
                ),
            )
        )
        await container.with_exec(
            [
                "ruff",
                "check",
                "frameart/",
                "tests/",
                "scripts/",
                ".dagger/modules/frameart-ci/src/",
            ],
            disable_dagger_in_dagger=True,
        ).sync()

    @function
    @check
    async def python_310(self) -> None:
        """Run the complete release-superset pytest suite on Python 3.10."""
        await _pytest(self.source, "3.10")

    @function
    @check
    async def python_311(self) -> None:
        """Run the complete release-superset pytest suite on Python 3.11."""
        await _pytest(self.source, "3.11")

    @function
    @check
    async def python_312(self) -> None:
        """Run the complete release-superset pytest suite on Python 3.12."""
        await _pytest(self.source, "3.12")

    @function
    @check
    async def python_313(self) -> None:
        """Run the complete release-superset pytest suite on Python 3.13."""
        await _pytest(self.source, "3.13")

    @function
    @check
    async def python_314(self) -> None:
        """Run the complete release-superset pytest suite on Python 3.14."""
        await _pytest(self.source, "3.14")

    @function
    @check
    async def browser(self) -> None:
        """Run all Playwright tests and their local API server in one container."""
        python = "/opt/frameart-venv/bin/python"
        base = (
            core()
            .container()
            .from_(_PLAYWRIGHT_IMAGE)
            .with_exec(
                [
                    "sh",
                    "-ec",
                    "apt-get update && apt-get install -y --no-install-recommends "
                    "python3.12 python3.12-venv fonts-dejavu-core "
                    "&& rm -rf /var/lib/apt/lists/*",
                ],
                disable_dagger_in_dagger=True,
            )
            .with_exec(
                ["python3.12", "-m", "venv", "/opt/frameart-venv"], disable_dagger_in_dagger=True
            )
        )
        dependencies = await _python_dependencies(
            self.source,
            base,
            version="3.12",
            python=python,
            extras=("api", "integrations"),
        )
        dependencies = (
            dependencies.with_mounted_cache("/root/.npm", core().cache_volume("frameart-ci-npm"))
            .with_env_variable("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
            .with_directory(_WORKDIR, _source(self.source, _NPM_INPUTS))
            .with_exec(["npm", "ci"], disable_dagger_in_dagger=True)
        )
        application = _install_application(self.source, dependencies, python=python)
        await (
            application.with_directory(_WORKDIR, _source(self.source, _BROWSER_INPUTS))
            # The unchanged Playwright config starts its own loopback API and
            # stores .e2e-data below this /tmp workdir. No host state is mounted.
            .with_exec(["npm", "run", "test:e2e"], disable_dagger_in_dagger=True)
            .sync()
        )

    @function
    @check
    async def docker(self) -> None:
        """Build the real Dockerfile and verify its non-root CLI entrypoint."""
        context = _source(self.source, ["Dockerfile", ".dockerignore", *_APP_INPUTS])
        await (
            context.docker_build()
            .with_env_variable("FRAMEART_DATA_DIR", "/tmp/frameart-ci-docker-data")
            .with_exec(["--version"], use_entrypoint=True, disable_dagger_in_dagger=True)
            .sync()
        )
