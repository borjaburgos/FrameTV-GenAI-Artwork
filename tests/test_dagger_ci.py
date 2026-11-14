"""Check the authored CI module without importing an SDK or starting an engine."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE = (
    Path(__file__).resolve().parents[1]
    / ".dagger/modules/frameart-ci/src/frameart_ci/__init__.py"
)
EXPECTED_CHECKS = {
    "ruff", "python_310", "python_311", "python_312", "python_313", "python_314",
    "browser", "docker",
}


def _tree() -> ast.Module:
    return ast.parse(MODULE.read_text(), filename=str(MODULE))


def _keywords(call: ast.Call) -> dict[str | None, ast.expr]:
    return {keyword.arg: keyword.value for keyword in call.keywords}


def _paths(node: ast.expr, constants: dict[str, ast.expr]) -> list[str]:
    """Resolve only literal path allowlists, including their starred constants."""
    if isinstance(node, ast.Name):
        return _paths(constants[node.id], constants)
    if isinstance(node, ast.Starred):
        return _paths(node.value, constants)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [path for item in node.elts for path in _paths(item, constants)]
    assert isinstance(node, ast.Constant) and isinstance(node.value, str)
    return [node.value]


def _assert_source_binding(tree: ast.Module) -> None:
    """A cached callback must receive source in its serialized constructor input."""
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    fields = [node for node in cls.body if isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name) and node.target.id == "source"]
    assert len(fields) == 1, "Missing explicit source constructor field"
    field = fields[0]
    assert isinstance(field.value, ast.Call) and isinstance(field.value.func, ast.Name)
    assert field.value.func.id == "field" and not field.value.args and not field.value.keywords
    assert ast.dump(field.annotation) == ast.dump(ast.parse(
        "Annotated[Directory, DefaultPath('/'), Ignore(_CONTEXT_IGNORE)]", mode="eval",
    ).body), "Source must be rooted and filtered at the input boundary"
    assert not any(isinstance(node, ast.Attribute)
                   and node.attr in {"host", "workspace", "current_workspace"}
                   for node in ast.walk(tree)), "Ambient source is not a callback cache-key input"
    for method in cls.body:
        if not isinstance(method, ast.AsyncFunctionDef):
            continue
        assert any(
            isinstance(node, ast.Call) and any(
                isinstance(argument, ast.Attribute) and argument.attr == "source"
                and isinstance(argument.value, ast.Name) and argument.value.id == "self"
                for argument in node.args
            )
            for node in ast.walk(method)
        ), f"{method.name} does not consume its bound source"


class TestDaggerCiContract:
    def test_exactly_eight_async_checks_have_function_and_check_decorators(self) -> None:
        tree = _tree()
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        assert len(classes) == 1 and classes[0].name == "FrameartCi"
        methods = [
            node for node in classes[0].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert len(methods) == 8
        assert {method.name for method in methods} == EXPECTED_CHECKS
        for method in methods:
            assert isinstance(method, ast.AsyncFunctionDef), method.name
            assert [
                decorator.id if isinstance(decorator, ast.Name) else None
                for decorator in method.decorator_list
            ] == ["function", "check"], method.name
        checked = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(decorator, ast.Name) and decorator.id == "check"
                    for decorator in node.decorator_list)
        ]
        assert len(checked) == 8

    def test_every_exec_explicitly_disables_nested_engine_access(self) -> None:
        calls = [
            node for node in ast.walk(_tree())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_exec"
        ]
        assert calls
        for call in calls:
            flag = _keywords(call).get("disable_dagger_in_dagger")
            assert isinstance(flag, ast.Constant) and flag.value is True, call.lineno

    def test_checks_bind_source_as_an_explicit_filtered_constructor_input(self) -> None:
        _assert_source_binding(_tree())

    @pytest.mark.parametrize(("old", "new"), [
        ("    source: Annotated[Directory, DefaultPath(\"/\"), Ignore(_CONTEXT_IGNORE)] = field()",
         ""),
        (", DefaultPath(\"/\")", ""),
        (", Ignore(_CONTEXT_IGNORE)", ""),
        ("field()", "field(init=False)"),
        ("field()", "field(default=None)"),
        ("self.source", "core().current_workspace()"),
    ])
    def test_missing_or_ambient_source_binding_is_rejected(self, old: str, new: str) -> None:
        source = MODULE.read_text()
        assert old in source
        with pytest.raises(AssertionError):
            _assert_source_binding(ast.parse(source.replace(old, new, 1)))

    def test_core_is_a_factory_without_ambient_workspace_or_host_reads(self) -> None:
        tree = _tree()
        imports = [
            node for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "dagger_clients.core"
        ]
        assert len(imports) == 1
        assert {(alias.name, alias.asname) for alias in imports[0].names} == {
            ("Container", None), ("Directory", None), ("core", None),
        }
        parents = {
            child: parent
            for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        accessors = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name) or node.id != "core":
                continue
            call = parents[node]
            assert isinstance(call, ast.Call) and call.func is node, node.lineno
            assert not call.args and not call.keywords, node.lineno
            accessor = parents[call]
            assert isinstance(accessor, ast.Attribute) and accessor.value is call, node.lineno
            assert accessor.attr in {"container", "cache_volume"}, node.lineno
            accessors.append(accessor.attr)
        assert set(accessors) == {"container", "cache_volume"}
        assert not any(isinstance(node, ast.Attribute)
                       and node.attr in {"host", "workspace", "current_workspace", "directory"}
                       for node in ast.walk(tree))

    def test_source_requires_allowlists_exclusions_and_no_other_directory_reads(self) -> None:
        tree = _tree()
        source = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_source")
        assert [argument.arg for argument in source.args.args] == ["source", "include"]
        assert not source.args.defaults and not source.args.vararg and not source.args.kwarg
        filters = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "filter"
        ]
        assert len(filters) == 1 and filters[0] in list(ast.walk(source))
        call = filters[0]
        assert isinstance(call.func.value, ast.Name) and call.func.value.id == "source"
        assert not call.args
        keywords = _keywords(call)
        assert isinstance(keywords.get("include"), ast.Name)
        assert keywords["include"].id == "include"
        assert isinstance(keywords.get("exclude"), ast.Name)
        assert keywords["exclude"].id == "_EXCLUDE_INPUTS"
        assert isinstance(keywords.get("gitignore"), ast.Constant)
        assert keywords["gitignore"].value is True
        constants = {
            target.id: node.value
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        expected_boundary = ast.parse("""[
            '**',
            *('!' + pattern for pattern in dict.fromkeys([
                *_APP_INPUTS, *_TEST_INPUTS, *_BROWSER_INPUTS, *_NPM_INPUTS,
                'Dockerfile', '.dockerignore',
            ])),
            *_EXCLUDE_INPUTS,
        ]""", mode="eval").body
        assert ast.dump(constants["_CONTEXT_IGNORE"]) == ast.dump(expected_boundary), (
            "Context must deny everything first, include only CI inputs, and exclude secrets last"
        )
        exclusions = set(_paths(constants["_EXCLUDE_INPUTS"], constants))
        assert {
            "**/*.token", "**/.env", "**/.env.*", "**/config.yaml", "**/secrets/**",
            "**/artifacts/**", "**/artwork/**", "**/data/**", "**/.venv/**",
            "**/node_modules/**",
        } <= exclusions
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_source"):
                continue
            assert len(node.args) == 2 and not node.keywords, node.lineno
            paths = _paths(node.args[1], constants)
            assert paths, node.lineno
            for path in paths:
                assert path and not path.startswith("/") and ".." not in path.split("/"), path
                assert path not in {".", "./", "*", "**", "**/*", "./**", "./**/*"}, path
