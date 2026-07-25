from __future__ import annotations
from hashlib import sha256

from pathlib import Path

import pytest

from vyupgrade.compiler import (
    ARCHIVE_TARGET_FLOOR,
    CompileResult,
    OverlayLayoutConflictError,
    TargetOverlay,
    _CompilerProcess,
    _declared_project_environment,
    _compiler_command,
    _overlay_search_paths,
    _run_compile,
    _supports_warning_policy,
    _target_validation_source,
    _uv_bin,
    compare_artifact_details,
    compare_artifacts,
    compile_source_ast,
    compile_source_file,
    compile_target_source,
    compile_target_archive,
    materialize_target_overlay,
    resolve_import_closure,
    target_overlay,
    unavailable_validation_artifacts,
)
from vyupgrade.models import Config, ContentIdentity, DependencyContext, FailureOrigin
from vyupgrade.versions import KNOWN_VERSIONS, parse_version


TARGET_COMPILER_OUTPUT = '[]\n{}\n{}\n{"ast_type":"Module","body":[]}\n'


def _compiler_process(
    command: list[str],
    *,
    returncode: int | None = 0,
    stdout: str = TARGET_COMPILER_OUTPUT,
    stderr: str = "",
    failure_origin: FailureOrigin | None = None,
    error: str | None = None,
) -> _CompilerProcess:
    return _CompilerProcess(
        command=command,
        context=DependencyContext(mode="isolated"),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        resolved_compiler="0.4.3",
        compiler_started=True,
        failure_origin=failure_origin or ("compiler" if returncode else None),
        error=error,
    )


@pytest.fixture(autouse=True)
def clear_uv_bin_cache():
    _uv_bin.cache_clear()
    yield
    _uv_bin.cache_clear()


def test_compiler_command_uses_packaged_uv_bin(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    assert _compiler_command(None, "0.3.7", None)[0] == "/tmp/uv"


def test_compiler_command_pins_python_for_uv(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    assert _compiler_command(None, "0.3.7", None) == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.11",
        "--with",
        "vyper==0.3.7",
        "vyper",
    ]


def test_compiler_command_allows_python_override(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    assert _compiler_command(None, "0.4.3", "3.12") == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.12",
        "--with",
        "vyper==0.4.3",
        "vyper",
    ]


def test_compiler_command_uses_older_python_for_legacy_vyper(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    assert _compiler_command(None, "0.2.16", None) == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.8",
        "--with",
        "vyper==0.2.16",
        "vyper",
    ]


def test_compiler_command_uses_typed_ast_runner_for_legacy_prerelease(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    command = _compiler_command(None, "0.1.0b4", None)

    assert command[:9] == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.8",
        "--with",
        "vyper==0.1.0b4",
        "--with",
        "typed-ast",
    ]
    assert command[9] == "python"
    assert command[10].endswith("legacy_vyper.py")


def test_compiler_command_falls_back_when_uv_lookup_is_broken(monkeypatch) -> None:
    def broken_find_uv_bin() -> str:
        raise TypeError('can only concatenate str (not "NoneType") to str')

    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", broken_find_uv_bin)
    monkeypatch.setattr("vyupgrade.compiler.shutil.which", lambda name: f"/fallback/{name}")

    assert _compiler_command(None, "0.4.3", None)[0] == "/fallback/uv"


def test_explicit_compiler_path_skips_uv_wrapper() -> None:
    assert _compiler_command("/tmp/vyper", "0.3.7", "3.11") == ["/tmp/vyper"]


def test_target_compile_fails_on_unsupported_required_layout(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        return _compiler_process(
            command,
            returncode=1,
            stderr="ValueError: Unsupported format type 'layout'",
        )

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile(
        ["vyper"], Path("/tmp/contract.vy"), Config(paths=(Path("/tmp/contract.vy"),))
    )

    assert result.status == "failed"
    assert result.unavailable_formats == ("layout",)
    assert "required output format 'layout'" in (result.stderr or "")
    assert calls[0][calls[0].index("-f") + 1] == "abi,method_identifiers,layout,ast"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "ast_error",
    [
        "KeyError: 'ast'",
        "ValueError: start (57,7) precedes previous end (58,0)",
    ],
)
def test_target_compile_falls_back_when_optional_ast_is_unavailable(
    monkeypatch,
    ast_error: str,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        requested = command[command.index("-f") + 1]
        if requested.endswith(",ast"):
            return _compiler_process(command, returncode=1, stderr=ast_error)
        return _compiler_process(command, stdout="[]\n{}\n{}\n")

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile(
        ["vyper"], Path("/tmp/contract.vy"), Config(paths=(Path("/tmp/contract.vy"),))
    )

    assert result.status == "passed"
    assert result.artifacts == {"abi": [], "method_identifiers": {}, "layout": {}}
    assert result.unavailable_formats == ("ast",)
    assert [call[call.index("-f") + 1] for call in calls] == [
        "abi,method_identifiers,layout,ast",
        "abi,method_identifiers,layout",
    ]


@pytest.mark.parametrize(
    "required_error",
    [
        "ValueError: Unsupported format type 'layout'",
        "ValueError: start (57,7) precedes previous end (58,0)",
    ],
)
def test_target_compile_does_not_drop_required_formats_after_ast_fallback(
    monkeypatch,
    required_error: str,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        requested = command[command.index("-f") + 1]
        error = "KeyError: 'ast'" if requested.endswith(",ast") else required_error
        return _compiler_process(command, returncode=1, stderr=error)

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile(
        ["vyper"], Path("/tmp/contract.vy"), Config(paths=(Path("/tmp/contract.vy"),))
    )

    assert result.status == "failed"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "artifacts, unavailable",
    [
        (
            {"abi": [None], "method_identifiers": {}, "layout": {}},
            ["abi"],
        ),
        (
            {
                "abi": [{"type": "function", "inputs": [None]}],
                "method_identifiers": {},
                "layout": {},
            },
            ["abi"],
        ),
        (
            {"abi": [], "method_identifiers": {"f()": None}, "layout": {}},
            ["method_identifiers"],
        ),
        (
            {"abi": [], "method_identifiers": {}, "layout": {"storage_layout": []}},
            ["layout"],
        ),
        (
            {
                "abi": [],
                "method_identifiers": {},
                "layout": {"storage_layout": {"owner": {"slot": 0}}},
            },
            ["layout"],
        ),
    ],
)
def test_validation_artifacts_reject_malformed_nested_schemas(artifacts, unavailable) -> None:
    from vyupgrade.compiler import unavailable_validation_artifacts

    result = CompileResult("passed", artifacts=artifacts)

    assert unavailable_validation_artifacts(result) == unavailable


def test_source_compile_degrades_on_unsupported_legacy_layout(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        if command[command.index("-f") + 1] == "abi,method_identifiers,layout":
            return _compiler_process(
                command,
                returncode=1,
                stderr="ValueError: Unsupported format type 'layout'",
            )
        return _compiler_process(command, stdout="[]\n{}\n")

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile_with_formats_for_test(
        ["vyper"], Path("/tmp/contract.vy"), ("abi", "method_identifiers", "layout")
    )

    assert result.status == "degraded"
    assert result.artifacts == {"abi": [], "method_identifiers": {}}
    assert result.unavailable_formats == ("layout",)
    assert calls[-1][calls[-1].index("-f") + 1] == "abi,method_identifiers"


def test_compile_retries_without_legacy_keyerror_format(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        requested = command[command.index("-f") + 1]
        if "method_identifiers" in requested:
            return _compiler_process(command, returncode=1, stderr="KeyError: 'method_identifiers'")
        if "ast" in requested:
            return _compiler_process(command, returncode=1, stderr="KeyError: 'ast'")
        return _compiler_process(command, stdout="[]\n")

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile_with_formats_for_test(
        ["vyper"], Path("/tmp/contract.vy"), ("abi", "method_identifiers", "ast")
    )

    assert result.status == "degraded"
    assert result.unavailable_formats == ("method_identifiers", "ast")
    assert calls[-1][calls[-1].index("-f") + 1] == "abi"


def test_compile_retries_without_ast_for_legacy_span_error(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        requested = command[command.index("-f") + 1]
        if requested in {
            "abi,method_identifiers,layout,ast",
            "abi,method_identifiers,layout",
            "abi,method_identifiers",
        }:
            return _compiler_process(
                command,
                returncode=1,
                stderr="ValueError: start (57,7) precedes previous end (58,0)",
            )
        return _compiler_process(command, stdout="[]\n")

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile_with_formats_for_test(
        ["vyper"], Path("/tmp/contract.vy"), ("abi", "method_identifiers", "layout", "ast")
    )

    assert result.status == "degraded"
    assert result.artifacts == {"abi": []}
    assert result.unavailable_formats == ("ast", "layout", "method_identifiers")
    assert calls[-1][calls[-1].index("-f") + 1] == "abi"


def _run_compile_with_formats_for_test(
    command: list[str], path: Path, formats: tuple[str, ...]
) -> CompileResult:
    from vyupgrade.compiler import _run_compile_with_formats

    return _run_compile_with_formats(
        command,
        path,
        Config(paths=(path,)),
        formats,
        (),
        False,
        allow_unsupported_formats=True,
    )


def test_target_compile_fails_when_compiler_omits_requested_output(monkeypatch) -> None:
    def fake_run(command, *_args, **_kwargs):
        return _compiler_process(command, stdout="[]\n{}\n")

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile(
        ["vyper"], Path("/tmp/contract.vy"), Config(paths=(Path("/tmp/contract.vy"),))
    )

    assert result.status == "failed"
    assert (
        result.stderr == "could not parse compiler output: expected 4 compiler outputs, received 2"
    )


def test_declared_project_environment_uses_full_project_and_project_compiler(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "contracts" / "AMM.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("#pragma version 0.4.1\n", encoding="utf-8")
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "amm"
version = "0.1.0"
dependencies = ["vyper==0.4.1", "snekmate==0.1.1", "requests==2.32.4"]
""",
        encoding="utf-8",
    )
    command = [
        _uv_bin(),
        "run",
        "--no-project",
        "--python",
        "3.11",
        "--with",
        "vyper==0.4.1",
        "vyper",
    ]

    with _declared_project_environment(command, contract, project_compiler=True) as (
        prepared,
        context,
    ):
        environment_root = Path(prepared[prepared.index("--project") + 1])

        assert prepared[:3] == [_uv_bin(), "run", "--isolated"]
        assert environment_root != project.resolve()
        assert (environment_root / "pyproject.toml").is_file()
        assert "--all-extras" in prepared
        assert "--all-groups" in prepared
        assert "--no-project" not in prepared
        assert "--python" not in prepared
        assert not any(argument.startswith("vyper==") for argument in prepared)
        assert prepared[-1] == "vyper"
        assert context == DependencyContext(
            mode="project",
            project_root=str(project.resolve()),
            manifest=ContentIdentity(
                str(pyproject.resolve()),
                sha256(pyproject.read_bytes()).hexdigest(),
            ),
        )


def test_declared_project_environment_uses_existing_lock_without_mutating_project(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    contract = project / "contract.vy"
    project.mkdir()
    contract.write_text("#pragma version 0.4.1\n", encoding="utf-8")
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "locked"
version = "0.1.0"
dependencies = ["vyper==0.4.1"]
""",
        encoding="utf-8",
    )
    lockfile = project / "uv.lock"
    lockfile.write_text("version = 1\n", encoding="utf-8")

    with _declared_project_environment(
        [_uv_bin(), "run", "--no-project", "--with", "vyper==0.4.1", "vyper"],
        contract,
        project_compiler=True,
    ) as (prepared, context):
        assert prepared[prepared.index("--project") + 1] == str(project.resolve())
        assert "--frozen" in prepared
        assert context == DependencyContext(
            mode="project",
            project_root=str(project.resolve()),
            manifest=ContentIdentity(
                str(pyproject.resolve()),
                sha256(pyproject.read_bytes()).hexdigest(),
            ),
            lockfile=ContentIdentity(
                str(lockfile.resolve()),
                sha256(lockfile.read_bytes()).hexdigest(),
            ),
        )


def test_declared_project_environment_uses_project_interpreter_for_compiler_overlay(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    contract = project / "contract.vy"
    project.mkdir()
    contract.write_text("#pragma version 0.3.10\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        """[project]
name = "compiler-overlay"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["requests==2.32.4"]
""",
        encoding="utf-8",
    )
    command = [
        _uv_bin(),
        "run",
        "--no-project",
        "--python",
        "3.11",
        "--with",
        "vyper==0.3.10",
        "vyper",
    ]

    with _declared_project_environment(command, contract, project_compiler=True) as (
        prepared,
        _context,
    ):
        assert "--python" not in prepared
        assert prepared[prepared.index("--with") : prepared.index("--with") + 2] == [
            "--with",
            "vyper==0.3.10",
        ]


def test_declared_project_environment_without_manifest_stays_isolated(tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("#pragma version 0.4.1\n", encoding="utf-8")
    command = [
        _uv_bin(),
        "run",
        "--no-project",
        "--python",
        "3.11",
        "--with",
        "vyper==0.4.1",
        "vyper",
    ]

    with _declared_project_environment(command, contract, project_compiler=True) as (
        prepared,
        context,
    ):
        assert prepared == command
        assert context == DependencyContext(mode="isolated")


def test_compile_skips_search_paths_for_legacy_prerelease_cli(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        return _compiler_process(command)

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile(
        ["/tmp/uv", "run", "--no-project", "--python", "3.8", "--with", "vyper==0.1.0b4", "vyper"],
        contract,
        Config(paths=(contract,), compiler_search_paths=(tmp_path,)),
        extra_paths=(tmp_path,),
    )

    assert result.status == "passed"
    assert "-p" not in calls[0]


def test_compile_can_suppress_modern_vyper_warnings(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *_args, **_kwargs):
        calls.append(command)
        return _compiler_process(command)

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile(
        ["vyper"],
        Path("/tmp/contract.vy"),
        Config(paths=(Path("/tmp/contract.vy"),)),
        suppress_warnings=True,
    )

    assert result.status == "passed"
    assert calls[0][calls[0].index("-W") + 1] == "none"


def test_compile_preserves_typed_timeout_failure(monkeypatch) -> None:
    def fake_run(command, *_args, **_kwargs):
        return _compiler_process(
            command,
            returncode=None,
            stdout="",
            failure_origin="timeout",
            error="compiler timed out after 120 seconds",
        )

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = _run_compile(
        ["vyper"], Path("/tmp/contract.vy"), Config(paths=(Path("/tmp/contract.vy"),))
    )

    assert result.status == "failed"
    assert result.stderr == "compiler timed out after 120 seconds"
    assert result.failure_origin == "timeout"
    assert result.compiler_started is True


def test_compile_source_ast_requests_ast_format(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.4.3\n", encoding="utf-8")
    calls: dict[str, object] = {}

    def fake_compiler_command(
        explicit: str | None, version: str | None, python: str | None
    ) -> list[str]:
        calls["compiler"] = (explicit, version, python)
        return ["vyper"]

    def fake_run_compile_with_formats(
        command: list[str],
        path: Path,
        config: Config,
        formats: tuple[str, ...],
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["run"] = (command, path, formats, extra_paths, suppress_warnings)
        return CompileResult("passed", artifacts={"ast": {"ast_type": "Module"}})

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", fake_compiler_command)
    monkeypatch.setattr(
        "vyupgrade.compiler._run_compile_with_formats", fake_run_compile_with_formats
    )

    result = compile_source_ast(contract, Config(paths=(contract,)), None)

    assert result.status == "passed"
    assert calls["compiler"] == (None, "0.4.3", None)
    assert calls["run"] == (["vyper"], contract, ("ast",), (), True)


def test_compile_source_file_requests_ast_with_validation_outputs(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.4.3\n", encoding="utf-8")
    calls: dict[str, object] = {}

    def fake_compiler_command(
        explicit: str | None, version: str | None, python: str | None
    ) -> list[str]:
        calls["compiler"] = (explicit, version, python)
        return ["vyper"]

    def fake_run_compile_with_formats(
        command: list[str],
        path: Path,
        config: Config,
        formats: tuple[str, ...],
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["run"] = (command, path, formats, extra_paths, suppress_warnings)
        return CompileResult("passed", artifacts={"ast": {"ast_type": "Module"}})

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", fake_compiler_command)
    monkeypatch.setattr(
        "vyupgrade.compiler._run_compile_with_formats", fake_run_compile_with_formats
    )

    result = compile_source_file(contract, Config(paths=(contract,)), None)

    assert result.status == "passed"
    assert calls["compiler"] == (None, "0.4.3", None)
    assert calls["run"] == (
        ["vyper"],
        contract,
        ("abi", "method_identifiers", "layout", "ast"),
        (),
        True,
    )


def test_compile_source_file_does_not_retry_or_transform_final_newline(
    monkeypatch, tmp_path
) -> None:
    contract = tmp_path / "contract.vy"
    source = "# @version 0.2.8\n# eof"
    contract.write_text(source, encoding="utf-8")
    calls: list[tuple[Path, bytes, object]] = []

    def fake_run_compile_with_formats(
        command: list[str],
        path: Path,
        config: Config,
        formats: tuple[str, ...],
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **kwargs,
    ) -> CompileResult:
        calls.append((path, path.read_bytes(), kwargs.get("allow_format_retries")))
        return CompileResult("failed", stderr="ValueError: start (2,0) precedes previous end (3,0)")

    monkeypatch.setattr("vyupgrade.compiler._prepare_command", lambda *args: (["vyper"], False))
    monkeypatch.setattr(
        "vyupgrade.compiler._run_compile_with_formats", fake_run_compile_with_formats
    )

    result = compile_source_file(contract, Config(paths=(contract,)), "0.2.8")

    assert result.status == "failed"
    assert calls == [(contract, source.encode(), False)]
    assert contract.read_text(encoding="utf-8") == source


def test_compile_target_source_enables_decimals_for_decimal_code(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.3.10\n", encoding="utf-8")
    calls: dict[str, object] = {}

    def fake_compiler_command(
        explicit: str | None, version: str | None, python: str | None
    ) -> list[str]:
        calls["compiler"] = (explicit, version, python)
        return ["vyper"]

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["run"] = (command, config.enable_decimals, extra_paths, suppress_warnings)
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", fake_compiler_command)
    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(
        contract,
        "#pragma version 0.4.3\n@external\ndef f(x: decimal) -> decimal:\n    return x / 2.0\n",
        Config(paths=(contract,)),
    )

    assert result.status == "passed"
    assert calls["compiler"] == (None, "0.4.3", None)
    assert calls["run"] == (["vyper"], True, (contract.parent,), True)


def test_compile_target_source_enables_decimals_for_math_import(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.4.0\n", encoding="utf-8")
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["enable_decimals"] = config.enable_decimals
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(
        contract,
        "#pragma version 0.4.3\nimport math\n@external\ndef f(x: uint256, y: uint256) -> uint256:\n    return math.mul(x, y)\n",
        Config(paths=(contract,), target_version="0.4.3"),
    )

    assert result.status == "passed"
    assert calls["enable_decimals"] is True


def test_compile_target_source_compiles_exact_candidate_bytes(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.2.11\n", encoding="utf-8")
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["source"] = path.read_text(encoding="utf-8")
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(
        contract,
        "#pragma version 0.2.11\n@external\ndef f():\n    pass\n",
        Config(paths=(contract,), target_version="0.4.3"),
    )

    assert result.status == "passed"
    assert calls["source"] == "#pragma version 0.2.11\n@external\ndef f():\n    pass\n"


def test_compile_target_interface_uses_import_harness(monkeypatch, tmp_path) -> None:
    interface = tmp_path / "IToken.vyi"
    source = "@external\ndef balanceOf(owner: address) -> uint256: ...\n"
    interface.write_text(source, encoding="utf-8")
    calls: dict[str, str] = {}

    monkeypatch.setattr("vyupgrade.compiler._prepare_command", lambda *_args: (["vyper"], True))

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["harness"] = path.read_text(encoding="utf-8")
        module = calls["harness"].split("import ", 1)[1].split(" as ", 1)[0]
        calls["interface"] = (path.parent / f"{module}.vyi").read_text(encoding="utf-8")
        return CompileResult(
            "passed", artifacts={"abi": [], "method_identifiers": {}, "layout": {}}
        )

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(interface, source, Config(paths=(interface,)))

    assert result.status == "passed"
    assert calls["interface"] == source
    assert calls["harness"].startswith("#pragma version 0.4.3\nimport vyupgrade_interface_")
    assert not list(tmp_path.glob("vyupgrade_interface_*"))


def test_target_validation_source_removes_duplicate_vyper_pragmas() -> None:
    source = "# @version 0.3.10\n# \u00a0@version ^0.2.11\n# pragma solidity ^0.8.0\n"

    result = _target_validation_source(source, "0.4.3")

    assert result == "#pragma version 0.4.3\n\n"


def test_target_validation_source_strips_natspec_docstrings() -> None:
    source = '''# @version 0.3.10
"""
@custom:dev first
@custom:dev duplicate
"""

@external
def f():
    """
    @returns legacy tag
    """
    pass
'''

    result = _target_validation_source(source, "0.4.3")

    assert '"""' not in result
    assert "@custom:dev" not in result
    assert "@returns" not in result
    assert "@external\ndef f():" in result


def test_target_validation_source_strips_multiline_function_natspec_docstrings() -> None:
    source = '''# @version 0.3.10
@external
def callback_deposit(
    user: address,
    callback_args: DynArray[uint256, 5]
  ) -> uint256[2]:
    """
    @notice Migrate loan
    @returns legacy plural tag
    """
    return [0, 0]
'''

    result = _target_validation_source(source, "0.4.3")

    assert '"""' not in result
    assert "@returns" not in result
    assert "def callback_deposit(" in result


def test_target_validation_source_keeps_assigned_triple_quoted_strings() -> None:
    source = '''# @version 0.3.10
VALUE: constant(String[32]) = """literal"""
'''

    result = _target_validation_source(source, "0.4.3")

    assert 'VALUE: constant(String[32]) = """literal"""' in result


def test_target_validation_source_keeps_multiline_string_literals() -> None:
    source = '''# @version 0.3.10
@external
def tokenURI(token_id: uint256) -> String[256]:
    return concat(
        """data:application/json,{
    "name": "Token #"""
        ,
        uint2str(token_id),
    )
'''

    result = _target_validation_source(source, "0.4.3")

    assert '"""data:application/json,{' in result
    assert "return concat(\n        ," not in result


def test_compile_target_source_uses_source_dir_for_relative_imports(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contracts" / "contract.vy"
    contract.parent.mkdir()
    contract.write_text("# @version 0.4.0\n", encoding="utf-8")
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["target_parent"] = path.parent
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(contract, "#pragma version 0.4.3\n", Config(paths=(contract,)))

    assert result.status == "passed"
    assert calls["target_parent"] == contract.parent


def _write_import_closure_fixture(
    tmp_path: Path,
) -> tuple[Path, dict[Path, str], tuple[Path, ...], tuple[Path, ...]]:
    project = tmp_path / "project"
    contract = project / "src" / "a.vy"
    dependency = project / "src" / "b.vy"
    transitive = project / "src" / "lib" / "c.vy"
    interface = project / "src" / "interfaces" / "IThing.json"
    contract.parent.mkdir(parents=True)
    transitive.parent.mkdir(parents=True)
    interface.parent.mkdir(parents=True)
    source = "#pragma version 0.4.3\nfrom . import b\n"
    contract.write_text(source, encoding="utf-8")
    dependency.write_text(
        "# @version 0.4.0\nfrom .lib import c\nfrom .interfaces import IThing\n",
        encoding="utf-8",
    )
    transitive.write_text("# @version 0.4.0\nVALUE: constant(uint256) = 1\n", encoding="utf-8")
    interface.write_text("[]\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "closure-fixture"\n',
        encoding="utf-8",
    )
    return (
        contract,
        {contract: source},
        (project,),
        tuple(sorted((dependency.resolve(), transitive.resolve(), interface.resolve()))),
    )


def _write_external_import_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, dict[Path, str], tuple[Path, ...]]:
    project = tmp_path / "project"
    contract = project / "src" / "main.vy"
    local_dependency = project / "src" / "local.vy"
    site_packages = tmp_path / "site-packages"
    external_dependency = site_packages / "depkg" / "util.vy"
    contract.parent.mkdir(parents=True)
    external_dependency.parent.mkdir(parents=True)
    source = "#pragma version 0.4.3\nfrom . import local\nfrom depkg import util\n"
    contract.write_text(source, encoding="utf-8")
    local_dependency.write_text(
        "# @version 0.3.10\nLOCAL: constant(uint256) = 1\n",
        encoding="utf-8",
    )
    external_dependency.write_text(
        "# @version 0.3.10\nEXTERNAL: constant(uint256) = 2\n",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        '[project]\nname = "overlay-project"\n',
        encoding="utf-8",
    )
    return contract, external_dependency, {contract: source}, (site_packages,)


def _write_create2_collision_fixture(
    tmp_path: Path,
    create2_source: str | None,
) -> tuple[Path, Path, dict[Path, str]]:
    project = tmp_path / "project"
    contract = project / "factory.vy"
    utils = project / "snekmate" / "utils"
    create2_address = utils / "create2_address.vy"
    create2 = utils / "create2.vy"
    utils.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='create2'\n")
    contract_source = "#pragma version 0.4.3\nfrom snekmate.utils import create2_address, create2\n"
    create2_address_source = (
        "# @version 0.3.10\n"
        "def _compute_address(value: uint256) -> address:\n"
        "    return empty(address)\n"
    )
    if create2_source is None:
        create2_source = create2_address_source.replace(
            "_compute_address", "_compute_create2_address"
        )
    contract.write_text(contract_source)
    create2_address.write_text(create2_address_source)
    create2.write_text(create2_source)
    return create2_address, create2, {contract: contract_source}


def test_resolve_import_closure_lists_transitive_dependencies(tmp_path) -> None:
    contract, sources, search_paths, expected_dependencies = _write_import_closure_fixture(tmp_path)

    closure = resolve_import_closure(sources, search_paths)

    assert closure.roots == (contract.resolve(),)
    assert closure.dependencies == expected_dependencies
    assert closure.files == (contract.resolve(), *expected_dependencies)
    assert closure.source_roots == (tmp_path / "project",)
    assert closure.common_root == tmp_path / "project"


@pytest.mark.parametrize(
    "documentation",
    [
        '"""\nimport ghost\n"""\n',
        'EXAMPLE: constant(String[32]) = """\nimport ghost\n"""\n',
    ],
)
def test_resolve_import_closure_ignores_imports_in_strings(
    tmp_path: Path, documentation: str
) -> None:
    project = tmp_path / "project"
    contract = project / "main.vy"
    dependency = project / "real.vy"
    ghost = project / "ghost.vy"
    project.mkdir()
    source = f"# @version 0.3.10\n{documentation}from . import real\n"
    contract.write_text(source, encoding="utf-8")
    dependency.write_text("# @version 0.3.10\n", encoding="utf-8")
    ghost.write_text("not valid Vyper\n", encoding="utf-8")

    closure = resolve_import_closure({contract: source}, (project,))

    assert closure.dependencies == (dependency.resolve(),)


def test_resolve_import_closure_matches_overlay_dependency_copies(tmp_path) -> None:
    _contract, sources, search_paths, _dependencies = _write_import_closure_fixture(tmp_path)
    closure = resolve_import_closure(sources, search_paths)

    with target_overlay(sources, "0.4.3", search_paths) as overlay:
        assert overlay is not None
        copied_files = {
            path.relative_to(overlay.root) for path in overlay.root.rglob("*") if path.is_file()
        }

    override_files = {path.resolve().relative_to(closure.common_root) for path in sources}
    config_files = {
        path.relative_to(closure.common_root)
        for path in closure.common_root.rglob("pyproject.toml")
    }
    create2_aliases = {
        path
        for path in copied_files
        if path.name == "create2.vy" and path.with_name("create2_address.vy") in copied_files
    }
    dependency_copies = copied_files - override_files - config_files - create2_aliases

    assert dependency_copies == {
        path.relative_to(closure.common_root) for path in closure.dependencies
    }


def test_resolve_import_closure_search_path_dependency(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "main.vy"
    site_packages = tmp_path / "site-packages"
    dependency = site_packages / "pkg" / "dep.vy"
    escaped = tmp_path / "outside" / "escape.vy"
    contract.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    escaped.parent.mkdir(parents=True)
    source = "#pragma version 0.4.3\nfrom pkg import dep\nfrom ...outside import escape\n"
    contract.write_text(source, encoding="utf-8")
    dependency.write_text("# @version 0.4.0\n", encoding="utf-8")
    escaped.write_text("# @version 0.4.0\n", encoding="utf-8")

    closure = resolve_import_closure(
        {contract: source},
        (site_packages, project),
    )

    assert closure.dependencies == (dependency.resolve(),)
    assert escaped.resolve() not in closure.files
    with pytest.raises(ValueError):
        dependency.resolve().relative_to(closure.common_root)
    with target_overlay({contract: source}, "0.4.3", (site_packages, project)) as overlay:
        assert overlay is not None
        assert not any(path.name == "dep.vy" for path in overlay.root.rglob("*") if path.is_file())


def test_resolve_import_closure_create2_alias_resolves_to_real_file(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "factory.vy"
    dependency = project / "snekmate" / "utils" / "create2_address.vy"
    contract.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    source = "#pragma version 0.4.3\nfrom snekmate.utils import create2\n"
    contract.write_text(source, encoding="utf-8")
    dependency.write_text("# @version 0.4.0\n", encoding="utf-8")

    closure = resolve_import_closure({contract: source}, (project,))

    assert closure.dependencies == (dependency.resolve(),)
    assert all(path.name != "create2.vy" for path in closure.files)


def test_resolve_import_closure_is_idempotent(tmp_path) -> None:
    _contract, sources, search_paths, _dependencies = _write_import_closure_fixture(tmp_path)

    first = resolve_import_closure(sources, search_paths)
    second = resolve_import_closure(sources, search_paths)

    assert first == second


def test_resolve_import_closure_empty_sources_raises() -> None:
    with pytest.raises(
        ValueError,
        match=r"^resolve_import_closure requires at least one source$",
    ):
        resolve_import_closure({})


def test_materialize_target_overlay_writes_into_given_root(tmp_path) -> None:
    _contract, sources, search_paths, _dependencies = _write_import_closure_fixture(tmp_path)
    with target_overlay(sources, "0.4.3", search_paths) as temporary:
        assert temporary is not None
        expected_files = {
            path.relative_to(temporary.root): path.read_bytes()
            for path in temporary.root.rglob("*")
            if path.is_file()
        }

    root = tmp_path / "materialized"
    root.mkdir()
    overlay = materialize_target_overlay(sources, "0.4.3", root, search_paths)

    assert overlay is not None
    assert overlay.root == root
    assert all(path.is_relative_to(root) for path in overlay.paths.values())
    assert {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == expected_files
    assert root.exists()


def test_materialize_target_overlay_output_root_under_project_root(tmp_path) -> None:
    _contract, sources, search_paths, _dependencies = _write_import_closure_fixture(tmp_path)
    project = tmp_path / "project"
    root = project / "overlay-output"

    overlay = materialize_target_overlay(sources, "0.4.3", root, search_paths)

    assert overlay is not None
    assert list(root.rglob("pyproject.toml")) == [root / "pyproject.toml"]
    assert (root / "pyproject.toml").read_bytes() == (project / "pyproject.toml").read_bytes()


def test_materialize_target_overlay_empty_sources_returns_none(tmp_path) -> None:
    root = tmp_path / "materialized"

    assert materialize_target_overlay({}, "0.4.3", root) is None


def test_closure_overlay_places_external_dep_import_root_relative(
    tmp_path,
) -> None:
    contract, dependency, sources, search_paths = _write_external_import_fixture(tmp_path)
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        sources,
        "0.4.3",
        root,
        search_paths,
        include_dependencies=True,
    )

    assert overlay is not None
    target = root / "depkg" / "util.vy"
    assert target.read_text(encoding="utf-8") == _target_validation_source(
        dependency.read_text(encoding="utf-8"), "0.4.3"
    )
    assert overlay.paths[dependency.resolve()] == target
    assert overlay.source_roots == (
        contract.parents[1].resolve(),
        search_paths[0].resolve(),
    )


def test_closure_overlay_places_external_candidate_bytes(tmp_path) -> None:
    _contract, dependency, sources, search_paths = _write_external_import_fixture(tmp_path)
    candidate = (
        "#pragma version 0.4.3\nEXTERNAL: constant(uint256) = 99\n# candidate bytes stay exact\n"
    )
    sources[dependency] = candidate
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        sources,
        "0.4.3",
        root,
        search_paths,
        include_dependencies=True,
    )

    assert overlay is not None
    target = root / "depkg" / "util.vy"
    assert target.read_bytes() == candidate.encode()
    assert overlay.paths[dependency.resolve()] == target


def test_closure_overlay_preserves_root_that_resolved_nested_dependency(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    contract = project / "main.vy"
    nested_search_path = project / "contracts"
    dependency = nested_search_path / "dep.vy"
    dependency.parent.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='project'\n")
    source = "#pragma version 0.4.3\nfrom contracts import dep\n"
    dependency_source = "#pragma version 0.4.3\nVALUE: constant(uint256) = 1\n"
    contract.write_text(source)
    dependency.write_text(dependency_source)
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        {contract: source},
        "0.4.3",
        root,
        (project, nested_search_path),
        include_dependencies=True,
    )

    assert overlay is not None
    target = root / "contracts" / "dep.vy"
    assert overlay.paths[dependency.resolve()] == target
    assert target.read_text() == dependency_source
    assert not (root / "dep.vy").exists()


def test_closure_overlay_preserves_symlinked_import_path(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "main.vy"
    search_path = tmp_path / "site-packages"
    dependency = search_path / "depkg" / "mod.vy"
    dependency.parent.mkdir(parents=True)
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='project'\n")
    (project / "alias").symlink_to(dependency.parent, target_is_directory=True)
    source = "#pragma version 0.4.3\nfrom alias import mod\n"
    dependency_source = "#pragma version 0.4.3\nVALUE: constant(uint256) = 1\n"
    contract.write_text(source)
    dependency.write_text(dependency_source)
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        {contract: source},
        "0.4.3",
        root,
        (search_path,),
        include_dependencies=True,
    )

    assert overlay is not None
    target = root / "alias" / "mod.vy"
    assert overlay.paths[dependency.resolve()] == target
    assert target.read_text() == dependency_source
    assert not (root / "depkg" / "mod.vy").exists()


def test_closure_overlay_paths_equal_resolved_closure(tmp_path) -> None:
    _contract, _dependency, sources, search_paths = _write_external_import_fixture(tmp_path)
    closure = resolve_import_closure(sources, search_paths)
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        sources,
        "0.4.3",
        root,
        search_paths,
        include_dependencies=True,
    )

    assert overlay is not None
    assert set(overlay.paths) == set(closure.files)
    assert (root / "pyproject.toml").is_file()
    assert root / "pyproject.toml" not in overlay.paths.values()


def test_closure_overlay_multi_root_placement(tmp_path) -> None:
    first_root = tmp_path / "repo" / "a"
    second_root = tmp_path / "repo" / "b"
    first = first_root / "x.vy"
    second = second_root / "y.vy"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "pyproject.toml").write_text("[project]\nname='a'\n")
    (second_root / "pyproject.toml").write_text("[project]\nname='b'\n")
    first_source = "#pragma version 0.4.3\nX: constant(uint256) = 1\n"
    second_source = "#pragma version 0.4.3\nY: constant(uint256) = 2\n"
    first.write_text(first_source)
    second.write_text(second_source)
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        {first: first_source, second: second_source},
        "0.4.3",
        root,
        include_dependencies=True,
    )

    assert overlay is not None
    assert (root / "x.vy").read_text() == first_source
    assert (root / "y.vy").read_text() == second_source
    assert not (root / "a" / "x.vy").exists()
    assert not (root / "b" / "y.vy").exists()
    assert overlay.source_roots == (first_root.resolve(), second_root.resolve())


def test_closure_overlay_conflicting_destinations_raise(tmp_path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = first_root / "pkg" / "util.vy"
    second = second_root / "pkg" / "util.vy"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    (first_root / "pyproject.toml").write_text("[project]\nname='first'\n")
    (second_root / "pyproject.toml").write_text("[project]\nname='second'\n")
    first_source = "#pragma version 0.4.3\nVALUE: constant(uint256) = 1\n"
    second_source = "#pragma version 0.4.3\nVALUE: constant(uint256) = 2\n"
    first.write_text(first_source)
    second.write_text(second_source)
    target = tmp_path / "overlay" / "pkg" / "util.vy"

    with pytest.raises(OverlayLayoutConflictError) as exc_info:
        materialize_target_overlay(
            {first: first_source, second: second_source},
            "0.4.3",
            tmp_path / "overlay",
            include_dependencies=True,
        )

    message = str(exc_info.value)
    assert str(first.resolve()) in message
    assert str(second.resolve()) in message
    assert str(target) in message


def test_closure_overlay_identical_content_collisions_dedupe(tmp_path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = first_root / "pkg" / "util.vy"
    second = second_root / "pkg" / "util.vy"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    (first_root / "pyproject.toml").write_text("[project]\nname='first'\n")
    (second_root / "pyproject.toml").write_text("[project]\nname='second'\n")
    source = "#pragma version 0.4.3\nVALUE: constant(uint256) = 1\n"
    first.write_text(source)
    second.write_text(source)
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        {first: source, second: source},
        "0.4.3",
        root,
        include_dependencies=True,
    )

    assert overlay is not None
    target = root / "pkg" / "util.vy"
    assert target.read_text() == source
    assert overlay.paths[first.resolve()] == target
    assert overlay.paths[second.resolve()] == target
    assert list((root / "pkg").glob("util.vy")) == [target]


def test_closure_overlay_drops_external_search_paths_from_compile_paths(
    tmp_path,
) -> None:
    _contract, _dependency, sources, search_paths = _write_external_import_fixture(tmp_path)

    with target_overlay(
        sources,
        "0.4.3",
        search_paths,
        include_dependencies=True,
    ) as closure_overlay:
        assert closure_overlay is not None
        closure_paths = _overlay_search_paths(closure_overlay, search_paths)
    with target_overlay(sources, "0.4.3", search_paths) as default_overlay:
        assert default_overlay is not None
        default_paths = _overlay_search_paths(default_overlay, search_paths)

    assert search_paths[0] not in closure_paths
    assert search_paths[0] in default_paths


def test_closure_overlay_create2_alias_beside_override(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "factory.vy"
    create2_address = project / "snekmate" / "utils" / "create2_address.vy"
    create2_address.parent.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='create2'\n")
    contract_source = "#pragma version 0.4.3\nfrom snekmate.utils import create2\n"
    candidate = (
        "#pragma version 0.4.3\n"
        "def _compute_address(value: uint256) -> address:\n"
        "    return empty(address)\n"
    )
    contract.write_text(contract_source)
    create2_address.write_text(candidate)
    sources = {contract: contract_source, create2_address: candidate}

    closure_root = tmp_path / "closure"
    closure = materialize_target_overlay(
        sources,
        "0.4.3",
        closure_root,
        include_dependencies=True,
    )
    default_root = tmp_path / "default"
    default = materialize_target_overlay(sources, "0.4.3", default_root)

    assert closure is not None
    assert default is not None
    alias = closure_root / "snekmate" / "utils" / "create2.vy"
    assert alias.read_text() == candidate.replace("_compute_address", "_compute_create2_address")
    assert (closure_root / "snekmate" / "utils" / "create2_address.vy").read_text() == candidate
    assert alias not in closure.paths.values()
    assert not (default_root / "snekmate" / "utils" / "create2.vy").exists()


def test_default_overlay_create2_alias_collision_preserves_last_write(
    tmp_path,
) -> None:
    create2_source = "# @version 0.3.10\nREAL_CREATE2: constant(uint256) = 2\n"
    _create2_address, _create2, sources = _write_create2_collision_fixture(tmp_path, create2_source)
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(sources, "0.4.3", root)

    assert overlay is not None
    assert (root / "snekmate" / "utils" / "create2.vy").read_text() == (
        _target_validation_source(create2_source, "0.4.3")
    )


def test_closure_overlay_create2_alias_collision_raises(tmp_path) -> None:
    create2_source = "# @version 0.3.10\nREAL_CREATE2: constant(uint256) = 2\n"
    create2_address, create2, sources = _write_create2_collision_fixture(tmp_path, create2_source)

    with pytest.raises(OverlayLayoutConflictError) as exc_info:
        materialize_target_overlay(
            sources,
            "0.4.3",
            tmp_path / "overlay",
            include_dependencies=True,
        )

    message = str(exc_info.value)
    assert str(create2_address.resolve()) in message
    assert str(create2.resolve()) in message


def test_closure_overlay_create2_alias_identical_content_dedupes(
    tmp_path,
) -> None:
    _create2_address, create2, sources = _write_create2_collision_fixture(tmp_path, None)
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        sources,
        "0.4.3",
        root,
        include_dependencies=True,
    )

    assert overlay is not None
    target = root / "snekmate" / "utils" / "create2.vy"
    assert target.read_text() == _target_validation_source(create2.read_text(), "0.4.3")
    assert overlay.paths[create2.resolve()] == target


def test_closure_overlay_copies_project_pyproject_only(tmp_path) -> None:
    _contract, dependency, sources, search_paths = _write_external_import_fixture(tmp_path)
    project_pyproject = tmp_path / "project" / "pyproject.toml"
    site_pyproject = search_paths[0] / "pyproject.toml"
    site_pyproject.write_text("[project]\nname='site-packages'\n")
    sources[dependency] = dependency.read_text()
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        sources,
        "0.4.3",
        root,
        search_paths,
        include_dependencies=True,
    )

    assert overlay is not None
    assert (root / "pyproject.toml").read_bytes() == project_pyproject.read_bytes()
    assert site_pyproject.read_bytes() not in {
        path.read_bytes() for path in root.rglob("pyproject.toml")
    }


def test_closure_overlay_excludes_nested_search_path_pyprojects(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "main.vy"
    search_path = project / "vendor" / "site-packages"
    dependency = search_path / "depkg" / "util.vy"
    contract.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    project_pyproject = project / "pyproject.toml"
    dependency_pyproject = dependency.parent / "pyproject.toml"
    project_pyproject.write_text("[project]\nname='project'\n")
    dependency_pyproject.write_text("[project]\nname='dependency'\n")
    source = "#pragma version 0.4.3\nfrom depkg import util\n"
    contract.write_text(source)
    dependency.write_text("# @version 0.3.10\n")
    root = tmp_path / "overlay"

    overlay = materialize_target_overlay(
        {contract: source},
        "0.4.3",
        root,
        (search_path,),
        include_dependencies=True,
    )

    assert overlay is not None
    assert (root / "pyproject.toml").read_bytes() == project_pyproject.read_bytes()
    assert overlay.paths[dependency.resolve()] == root / "depkg" / "util.vy"
    assert not (root / "vendor" / "site-packages" / "depkg" / "pyproject.toml").exists()
    assert dependency_pyproject.read_bytes() not in {
        path.read_bytes() for path in root.rglob("pyproject.toml")
    }


def test_materialize_default_mode_ignores_external_deps(tmp_path) -> None:
    contract, dependency, sources, search_paths = _write_external_import_fixture(tmp_path)
    project = contract.parents[1]
    local_dependency = contract.with_name("local.vy")
    implicit_root = tmp_path / "implicit"
    explicit_root = tmp_path / "explicit"

    implicit = materialize_target_overlay(sources, "0.4.3", implicit_root, search_paths)
    explicit = materialize_target_overlay(
        sources,
        "0.4.3",
        explicit_root,
        search_paths,
        include_dependencies=False,
    )

    assert implicit is not None
    assert explicit is not None

    def tree(root: Path) -> dict[Path, bytes]:
        return {
            path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
        }

    expected_tree = {
        Path("pyproject.toml"): (project / "pyproject.toml").read_bytes(),
        Path("src/local.vy"): _target_validation_source(
            local_dependency.read_text(), "0.4.3"
        ).encode(),
        Path("src/main.vy"): sources[contract].encode(),
    }
    assert tree(implicit_root) == expected_tree
    assert tree(explicit_root) == expected_tree
    assert dependency.resolve() not in implicit.paths
    assert {path: target.relative_to(implicit_root) for path, target in implicit.paths.items()} == {
        contract.resolve(): Path("src/main.vy")
    }
    assert {path: target.relative_to(explicit_root) for path, target in explicit.paths.items()} == {
        contract.resolve(): Path("src/main.vy")
    }
    assert implicit.source_roots == explicit.source_roots == (project.resolve(),)


def test_target_overlay_rewrites_imported_vyper_pragmas(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "exchanges" / "sfrxusd.vy"
    imported = project / "src" / "interfaces" / "IExchange.vyi"
    contract.parent.mkdir(parents=True)
    imported.parent.mkdir(parents=True)
    contract.write_text(
        "# @version 0.4.1\nfrom src.interfaces import IExchange\n",
        encoding="utf-8",
    )
    imported.write_text(
        '# @version 0.4.1\n@external\n@view\ndef quote() -> uint256:\n    """docs"""\n    return ...\n',
        encoding="utf-8",
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["path"] = path
        calls["search_paths"] = config.compiler_search_paths
        imported_overlay = path.parents[1] / "interfaces" / "IExchange.vyi"
        calls["imported_source"] = imported_overlay.read_text(encoding="utf-8")
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom src.interfaces import IExchange\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        result = compile_target_source(
            contract,
            "#pragma version 0.4.3\nfrom src.interfaces import IExchange\n",
            Config(paths=(contract,), compiler_search_paths=(project,)),
            overlay,
        )

    assert result.status == "passed"
    assert calls["path"] != contract
    assert calls["search_paths"][0].name.startswith("vyupgrade-target-")
    assert "#pragma version 0.4.3" in calls["imported_source"]
    assert "# @version 0.4.1" not in calls["imported_source"]
    assert "def quote() -> uint256: ..." in calls["imported_source"]
    assert '"""docs"""' not in calls["imported_source"]
    assert "return ..." not in calls["imported_source"]


def test_target_overlay_rewrites_imported_dependency_modules(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "contracts" / "LpSugar.vy"
    imported = project / "contracts" / "modules" / "lp_shared.vy"
    contract.parent.mkdir(parents=True)
    imported.parent.mkdir(parents=True)
    contract.write_text(
        "# @version 0.4.0\nfrom contracts.modules import lp_shared\n",
        encoding="utf-8",
    )
    imported.write_text(
        "# @version 0.4.0\nfrom snekmate.utils import create2_address\n"
        "def f(salt: bytes32, init_hash: bytes32, factory: address) -> address:\n"
        "    return create2_address._compute_address(salt, init_hash, factory)\n",
        encoding="utf-8",
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        imported_overlay = path.parent / "modules" / "lp_shared.vy"
        calls["imported_source"] = imported_overlay.read_text(encoding="utf-8")
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom contracts.modules import lp_shared\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        result = compile_target_source(
            contract,
            "#pragma version 0.4.3\nfrom contracts.modules import lp_shared\n",
            Config(paths=(contract,), compiler_search_paths=(project,)),
            overlay,
        )

    assert result.status == "passed"
    assert "from snekmate.utils import create2" in calls["imported_source"]
    assert "create2._compute_create2_address(salt, init_hash, factory)" in calls["imported_source"]


def test_target_overlay_skips_unrelated_sibling_sources(tmp_path) -> None:
    project = tmp_path / "flat"
    contract = project / "main.vy"
    dependency = project / "dependency.vy"
    unrelated = project / "unrelated.vy"
    project.mkdir()
    contract.write_text("# @version 0.4.0\nimport dependency\n", encoding="utf-8")
    dependency.write_text("# @version 0.4.0\nx: uint256\n", encoding="utf-8")
    unrelated.write_text("# @version 0.4.0\ny: uint256\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nimport dependency\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        assert overlay is not None
        overlay_contract = overlay.paths[contract.resolve()]
        overlay_root = overlay_contract.parent
        assert (overlay_root / "dependency.vy").exists()
        assert not (overlay_root / "unrelated.vy").exists()


def test_target_overlay_preserves_shallow_absolute_import_roots(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "strategy.vy"
    dependency = project / "src" / "modules" / "constants.vy"
    contract.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom src.modules import constants\n", encoding="utf-8")
    dependency.write_text(
        "# @version 0.4.0\nMAX_BPS: constant(uint256) = 10_000\n", encoding="utf-8"
    )

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom src.modules import constants\n"},
        "0.4.3",
        (project, project / "src"),
    ) as overlay:
        assert overlay is not None
        overlay_contract = overlay.paths[contract.resolve()]
        overlay_project = overlay_contract.parents[1]
        assert overlay_contract.relative_to(overlay_project) == Path("src/strategy.vy")
        assert (overlay_project / "src" / "modules" / "constants.vy").exists()


def test_target_overlay_copies_json_interfaces(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "validator.vy"
    interface = project / "src" / "interfaces" / "IValidator.json"
    contract.parent.mkdir(parents=True)
    interface.parent.mkdir(parents=True)
    contract.write_text(
        "# @version 0.4.3\nfrom src.interfaces import IValidator\n", encoding="utf-8"
    )
    interface.write_text("[]\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom src.interfaces import IValidator\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        assert overlay is not None
        overlay_contract = overlay.paths[contract.resolve()]
        overlay_project = overlay_contract.parents[1]
        assert (overlay_project / "src" / "interfaces" / "IValidator.json").exists()


def test_target_overlay_copies_create2_address_under_rewritten_name(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "factory.vy"
    dependency = project / "snekmate" / "utils" / "create2_address.vy"
    contract.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    contract.write_text(
        "# @version 0.4.0\nfrom snekmate.utils import create2_address\n", encoding="utf-8"
    )
    dependency.write_text(
        "# @version 0.4.0\n"
        "def _compute_address(salt: bytes32, init_hash: bytes32, factory: address) -> address:\n"
        "    return empty(address)\n",
        encoding="utf-8",
    )

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom snekmate.utils import create2\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        assert overlay is not None
        overlay_contract = overlay.paths[contract.resolve()]
        overlay_project = overlay_contract.parents[1]
        alias_source = (overlay_project / "snekmate" / "utils" / "create2.vy").read_text(
            encoding="utf-8"
        )

    assert "def _compute_create2_address(" in alias_source
    assert "def _compute_address(" not in alias_source


def test_target_overlay_resolves_dependency_imports_from_search_roots(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "token.vy"
    erc20 = project / "lib" / "pypi" / "snekmate" / "tokens" / "erc20.vy"
    permit = erc20.parent / "interfaces" / "IERC20Permit.vyi"
    contract.parent.mkdir(parents=True)
    erc20.parent.mkdir(parents=True)
    permit.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom snekmate.tokens import erc20\n", encoding="utf-8")
    erc20.write_text(
        "# @version 0.4.0\nimport interfaces.IERC20Permit as IERC20Permit\n",
        encoding="utf-8",
    )
    permit.write_text("# @version 0.4.0\n@external\ndef permit(): ...\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom snekmate.tokens import erc20\n"},
        "0.4.3",
        (project / "lib" / "pypi", project / "src", project),
    ) as overlay:
        assert overlay is not None
        assert (overlay.root / "lib" / "pypi" / "snekmate" / "tokens" / "erc20.vy").exists()
        assert (
            overlay.root
            / "lib"
            / "pypi"
            / "snekmate"
            / "tokens"
            / "interfaces"
            / "IERC20Permit.vyi"
        ).exists()


def test_target_overlay_preserves_nested_package_search_roots(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "gnosis" / "TokenizedValidator.vy"
    ownable = (
        project
        / "lib"
        / "github"
        / "pcaversaccio"
        / "snekmate"
        / "src"
        / "snekmate"
        / "auth"
        / "ownable.vy"
    )
    contract.parent.mkdir(parents=True)
    ownable.parent.mkdir(parents=True)
    contract.write_text(
        "# @version 0.4.3\nfrom pcaversaccio.snekmate.src.snekmate.auth import ownable\n",
        encoding="utf-8",
    )
    ownable.write_text("# @version 0.4.3\nOWNER: public(address)\n", encoding="utf-8")

    with target_overlay(
        {
            contract: (
                "#pragma version 0.4.3\n"
                "from pcaversaccio.snekmate.src.snekmate.auth import ownable\n"
            )
        },
        "0.4.3",
        (project / "lib", project / "lib" / "github", project / "src", project),
    ) as overlay:
        assert overlay is not None
        assert (
            overlay.root
            / "lib"
            / "github"
            / "pcaversaccio"
            / "snekmate"
            / "src"
            / "snekmate"
            / "auth"
            / "ownable.vy"
        ).exists()
        assert overlay.root / "lib" / "github" in overlay.search_paths


def test_target_overlay_copies_imported_site_package_sources(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "curve_stablecoin" / "mpolicies" / "AggMonetaryPolicy4.vy"
    ema = project / "venv" / "lib" / "python3.10" / "site-packages" / "curve_std" / "ema.vy"
    contract.parent.mkdir(parents=True)
    ema.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.3\nfrom curve_std import ema\n", encoding="utf-8")
    ema.write_text("# @version 0.4.3\nWINDOW: constant(uint256) = 10\n", encoding="utf-8")

    site_packages = project / "venv" / "lib" / "python3.10" / "site-packages"
    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom curve_std import ema\n"},
        "0.4.3",
        (project, site_packages),
    ) as overlay:
        assert overlay is not None
        assert (
            overlay.root / "venv" / "lib" / "python3.10" / "site-packages" / "curve_std" / "ema.vy"
        ).exists()
        assert (
            overlay.root / "venv" / "lib" / "python3.10" / "site-packages" in overlay.search_paths
        )


def test_target_overlay_resolves_relative_dependency_imports(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "token.vy"
    erc20 = project / "src" / "erc20.vy"
    interface = project / "src" / "utils" / "interfaces" / "IERC5267.vyi"
    contract.parent.mkdir(parents=True)
    interface.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom . import erc20\n", encoding="utf-8")
    erc20.write_text(
        "# @version 0.4.0\nfrom .utils.interfaces import IERC5267\n",
        encoding="utf-8",
    )
    interface.write_text("# @version 0.4.0\n@external\ndef eip712Domain(): ...\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom . import erc20\n"},
        "0.4.3",
        (project / "src", project),
    ) as overlay:
        assert overlay is not None
        assert (overlay.root / "src" / "erc20.vy").exists()
        assert (overlay.root / "src" / "utils" / "interfaces" / "IERC5267.vyi").exists()


def test_target_overlay_rewrites_standard_json_src_package_imports(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "token.vy"
    erc20 = project / "src" / "erc20.vy"
    interface = project / "utils" / "interfaces" / "IERC5267.vyi"
    contract.parent.mkdir(parents=True)
    interface.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom . import erc20\n", encoding="utf-8")
    erc20.write_text(
        "# @version 0.4.0\nfrom .utils.interfaces import IERC5267\n",
        encoding="utf-8",
    )
    interface.write_text("# @version 0.4.0\n@external\ndef eip712Domain(): ...\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom . import erc20\n"},
        "0.4.3",
        (project / "src", project),
    ) as overlay:
        assert overlay is not None
        erc20_overlay = overlay.root / "src" / "erc20.vy"
        assert "from ..utils.interfaces import IERC5267" in erc20_overlay.read_text(
            encoding="utf-8"
        )
        assert (overlay.root / "utils" / "interfaces" / "IERC5267.vyi").exists()


def test_target_overlay_preserves_exact_override_bytes(tmp_path) -> None:
    package = tmp_path / "package"
    contract = package / "multijson.vy"
    math = package / "math.vy"
    package.mkdir()
    contract.write_text("# @version 0.4.0\nimport math\n", encoding="utf-8")
    math.write_text(
        "# @version 0.4.0\n@internal\ndef mul(x: uint256, y: uint256) -> uint256:\n    return x * y\n",
        encoding="utf-8",
    )

    with target_overlay(
        {contract: "#pragma version 0.4.3\nimport math\n"},
        "0.4.3",
        (package,),
    ) as overlay:
        assert overlay is not None
        contract_overlay = overlay.root / "multijson.vy"
        assert contract_overlay.read_text(encoding="utf-8") == (
            "#pragma version 0.4.3\nimport math\n"
        )
        assert (overlay.root / "math.vy").exists()


def test_compile_target_source_keeps_decimal_flag_off_without_decimal(
    monkeypatch, tmp_path
) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.3.10\n", encoding="utf-8")
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
        **_kwargs,
    ) -> CompileResult:
        calls["enable_decimals"] = config.enable_decimals
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(
        contract,
        "#pragma version 0.4.3\n@external\ndef f(x: uint256) -> uint256:\n    return x // 2\n",
        Config(paths=(contract,)),
    )

    assert result.status == "passed"
    assert calls["enable_decimals"] is False


def test_warning_policy_is_only_used_for_modern_vyper() -> None:
    assert not _supports_warning_policy("0.4.0")
    assert _supports_warning_policy("0.4.1")
    assert _supports_warning_policy("0.4.3")
    assert not _supports_warning_policy("0.3.10")


def test_compare_artifacts_canonicalizes_abi_constructor_and_gas() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "constructor",
                    "stateMutability": "nonpayable",
                    "inputs": [],
                    "outputs": [],
                },
                {
                    "type": "function",
                    "name": "f",
                    "stateMutability": "view",
                    "inputs": [],
                    "outputs": [{"type": "uint256", "name": ""}],
                    "gas": 1234,
                },
            ],
            "method_identifiers": {
                "__init__(string,string,uint256)": "0xdeadbeef",
                "f()": "0x26121ff0",
            },
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "f",
                    "stateMutability": "view",
                    "inputs": [],
                    "outputs": [{"type": "uint256", "name": ""}],
                },
                {
                    "type": "constructor",
                    "stateMutability": "nonpayable",
                    "inputs": [],
                    "outputs": [],
                },
            ],
            "method_identifiers": {"f()": "0x26121ff0"},
        },
    )

    assert compare_artifacts(source, target) == (True, True, None)


def test_compare_artifacts_treats_pure_and_view_as_readonly_abi() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "target",
                    "stateMutability": "pure",
                    "inputs": [],
                    "outputs": [{"type": "address", "name": ""}],
                }
            ]
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "target",
                    "stateMutability": "view",
                    "inputs": [],
                    "outputs": [{"type": "address", "name": ""}],
                }
            ]
        },
    )

    assert compare_artifacts(source, target) == (True, None, None)
    assert compare_artifact_details(source, target)[0] == []


def test_compare_artifacts_normalizes_legacy_abi_type_spellings() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "constructor",
                    "stateMutability": "nonpayable",
                    "inputs": [],
                    "outputs": [{"type": "address", "name": ""}],
                },
                {
                    "type": "function",
                    "name": "getAverageRating",
                    "stateMutability": "view",
                    "inputs": [{"name": "target", "type": "address"}],
                    "outputs": [{"name": "", "type": "fixed168x10"}],
                },
                {
                    "type": "function",
                    "name": "deposit",
                    "stateMutability": "payable",
                    "inputs": [
                        {
                            "name": "swap_route",
                            "type": "(address,address,int128,int128,bool,bool)[]",
                        }
                    ],
                    "outputs": [{"name": "", "type": "uint256"}],
                },
            ]
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "constructor",
                    "stateMutability": "nonpayable",
                    "inputs": [],
                    "outputs": [],
                },
                {
                    "type": "function",
                    "name": "getAverageRating",
                    "stateMutability": "view",
                    "inputs": [{"name": "target", "type": "address"}],
                    "outputs": [{"name": "", "type": "int168", "internalType": "decimal"}],
                },
                {
                    "type": "function",
                    "name": "deposit",
                    "stateMutability": "payable",
                    "inputs": [
                        {
                            "name": "swap_route",
                            "type": "tuple[]",
                            "components": [
                                {"name": "swap_pool", "type": "address"},
                                {"name": "j_token", "type": "address"},
                                {"name": "i", "type": "int128"},
                                {"name": "j", "type": "int128"},
                                {"name": "is_underlying", "type": "bool"},
                                {"name": "is_crypto_pool", "type": "bool"},
                            ],
                        }
                    ],
                    "outputs": [{"name": "", "type": "uint256"}],
                },
            ]
        },
    )

    assert compare_artifacts(source, target) == (True, None, None)
    assert compare_artifact_details(source, target)[0] == []


def test_compare_artifacts_ignores_legacy_abi_metadata_names_and_readonly_mutability() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "constructor",
                    "inputs": [],
                    "outputs": [],
                },
                {
                    "type": "function",
                    "name": "coins",
                    "constant": True,
                    "payable": False,
                    "inputs": [{"name": "arg0", "type": "int128"}],
                    "outputs": [{"name": "out", "type": "address"}],
                },
                {
                    "type": "function",
                    "name": "update",
                    "constant": False,
                    "payable": False,
                    "inputs": [{"name": "value", "type": "uint256"}],
                    "outputs": [{"name": "deadline", "type": "uint256", "unit": "sec"}],
                },
            ]
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "constructor",
                    "stateMutability": "nonpayable",
                    "inputs": [],
                    "outputs": [],
                },
                {
                    "type": "function",
                    "name": "coins",
                    "stateMutability": "view",
                    "inputs": [{"name": "i", "type": "int128"}],
                    "outputs": [{"name": "", "type": "address"}],
                },
                {
                    "type": "function",
                    "name": "update",
                    "stateMutability": "nonpayable",
                    "inputs": [{"name": "new_value", "type": "uint256"}],
                    "outputs": [{"name": "", "type": "uint256"}],
                },
            ]
        },
    )

    assert compare_artifacts(source, target) == (True, None, None)
    assert compare_artifact_details(source, target)[0] == []


def test_compare_artifacts_keeps_payable_abi_mutability_changes_visible() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "receiveFlashLoan",
                    "stateMutability": "nonpayable",
                    "inputs": [{"name": "tokens", "type": "address[]"}],
                    "outputs": [],
                }
            ]
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "receiveFlashLoan",
                    "stateMutability": "payable",
                    "inputs": [{"name": "tokens", "type": "address[]"}],
                    "outputs": [],
                }
            ]
        },
    )

    assert compare_artifacts(source, target) == (False, None, None)
    assert compare_artifact_details(source, target)[0] == [
        "changed ABI entry: function receiveFlashLoan(address[]): stateMutability 'nonpayable' -> 'payable'"
    ]


def test_compare_artifacts_keeps_view_to_nonpayable_abi_changes_visible() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "record",
                    "stateMutability": "view",
                    "inputs": [{"name": "value", "type": "uint256"}],
                    "outputs": [],
                }
            ]
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "record",
                    "stateMutability": "nonpayable",
                    "inputs": [{"name": "value", "type": "uint256"}],
                    "outputs": [],
                }
            ]
        },
    )

    assert compare_artifacts(source, target) == (False, None, None)
    assert compare_artifact_details(source, target)[0] == [
        "changed ABI entry: function record(uint256): stateMutability 'view' -> 'nonpayable'"
    ]


def test_compare_artifact_details_ignores_constructor_selector() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "method_identifiers": {
                "__init__(string,string,uint256)": "0xcece287e",
                "f()": "0x26121ff0",
            },
        },
    )
    target = CompileResult(
        "passed",
        artifacts={"method_identifiers": {"f()": "0x26121ff0"}},
    )

    _abi_diff, method_diff, _storage_diff = compare_artifact_details(source, target)

    assert method_diff == []


def test_compare_artifact_details_normalizes_single_tuple_outputs() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "points_sum",
                    "stateMutability": "view",
                    "inputs": [
                        {"name": "arg0", "type": "int128"},
                        {"name": "arg1", "type": "uint256"},
                    ],
                    "outputs": [
                        {"name": "bias", "type": "uint256"},
                        {"name": "slope", "type": "uint256"},
                    ],
                }
            ],
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "points_sum",
                    "stateMutability": "view",
                    "inputs": [
                        {"name": "arg0", "type": "int128"},
                        {"name": "arg1", "type": "uint256"},
                    ],
                    "outputs": [
                        {
                            "name": "",
                            "type": "tuple",
                            "components": [
                                {"name": "bias", "type": "uint256"},
                                {"name": "slope", "type": "uint256"},
                            ],
                        }
                    ],
                }
            ],
        },
    )

    assert compare_artifacts(source, target) == (True, None, None)
    assert compare_artifact_details(source, target)[0] == []


def test_compare_artifacts_normalizes_storage_layout_shapes() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "token": {
                    "location": "storage",
                    "slot": 0,
                    "type": "interface ERC20",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
                "pool": {"location": "storage", "slot": 2, "type": "interface IPool"},
                "factories": {
                    "location": "storage",
                    "slot": 3,
                    "type": "HashMap[address, interface IFactory]",
                },
                "loan": {
                    "location": "storage",
                    "slot": 4,
                    "type": "HashMap[address, Loan declaration object]",
                },
                "token_owner": {
                    "location": "storage",
                    "slot": 5,
                    "type": "HashMap[uint256, address][uint256, address]",
                },
                "owner_tokens": {
                    "location": "storage",
                    "slot": 6,
                    "type": "HashMap[address, HashMap[uint256, uint256][uint256, uint256]][address, HashMap[uint256, uint256][uint256, uint256]]",
                },
                "tokens": {
                    "location": "storage",
                    "slot": 7,
                    "type": "DynArray[interface ERC20, 255]",
                },
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "token": {"slot": 0, "type": "/tmp/ERC20.vyi", "n_slots": 1},
                    "balance": {"slot": 1, "type": "uint256", "n_slots": 1},
                    "pool": {"slot": 2, "type": "/tmp/IPool.vyi", "n_slots": 1},
                    "factories": {
                        "slot": 3,
                        "type": "HashMap[address, /tmp/IFactory.vyi]",
                        "n_slots": 1,
                    },
                    "loan": {
                        "slot": 4,
                        "type": "HashMap[address, Loan]",
                        "n_slots": 1,
                    },
                    "token_owner": {
                        "slot": 5,
                        "type": "HashMap[uint256, address]",
                        "n_slots": 1,
                    },
                    "owner_tokens": {
                        "slot": 6,
                        "type": "HashMap[address, HashMap[uint256, uint256]]",
                        "n_slots": 1,
                    },
                    "tokens": {
                        "slot": 7,
                        "type": "DynArray[/tmp/site-packages/vyper/builtins/interfaces/ERC20.vyi, 255]",
                        "n_slots": 256,
                    },
                },
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)


@pytest.mark.parametrize(
    ("source_type", "target_type", "equal"),
    [
        (
            "corpus/vyper/contracts/chainsecurity/10_0x60f/contracts/bridgers/IBridger",
            "/tmp/target/contracts/bridgers/IBridger.vyi",
            False,
        ),
        (
            "HashMap[address, corpus/vyper/contracts/project/interfaces/IBridger]",
            "HashMap[address, /tmp/target/interfaces/IBridger.vyi]",
            False,
        ),
        (
            "DynArray[corpus/vyper/contracts/project/interfaces/IBridger, 8]",
            "DynArray[/tmp/target/interfaces/IBridger.vyi, 8]",
            False,
        ),
        ("/tmp/modules/one/Pool", "/tmp/modules/two/Pool", False),
        ("/tmp/modules/one/Pool.vy", "/tmp/modules/two/Pool.vy", False),
        (
            "DynArray[/tmp/modules/one/Pool.vy, 8]",
            "DynArray[/tmp/modules/two/Pool.vy, 8]",
            False,
        ),
        ("IBridger", "/tmp/foo+bar/IBridger.vyi", False),
        ("IBridger", "/tmp/foo@bar/IBridger.vyi", False),
        ("IBridger", "/tmp/foo (copy)/IBridger.vyi", False),
        ("IBridger", r"C:\target\interfaces\IBridger.vyi", False),
        (
            "HashMap[address, IBridger]",
            "HashMap[address, /tmp/foo+bar/IBridger.vyi]",
            False,
        ),
        (
            "DynArray[IBridger, 8]",
            "DynArray[/tmp/foo (copy)/IBridger.vyi, 8]",
            False,
        ),
        (
            "HashMap[IBridger, DynArray[IPool, 8]]",
            "HashMap[/tmp/foo@bar/IBridger.vyi, DynArray[/tmp/foo (copy)/IPool.vyi, 8]]",
            False,
        ),
        (
            "HashMap[IBridger, DynArray[IPool, 8]]",
            r"HashMap[C:\target\IBridger.vyi, DynArray[C:\target\IPool.vyi, 8]]",
            False,
        ),
        ("interface IBridger", "/tmp/IBridger.vyi", True),
        (
            "HashMap[address, interface IBridger]",
            "HashMap[address, /tmp/IBridger.vyi]",
            True,
        ),
        (
            "DynArray[interface IBridger, 8]",
            "DynArray[/tmp/IBridger.vyi, 8]",
            True,
        ),
    ],
)
def test_compare_artifacts_normalizes_relative_and_absolute_storage_type_paths(
    source_type: str,
    target_type: str,
    equal: bool,
) -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"bridger": {"slot": 4, "type": source_type}}}},
    )
    target = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"bridger": {"slot": 4, "type": target_type}}}},
    )

    assert compare_artifacts(source, target) == (None, None, equal)
    assert bool(compare_artifact_details(source, target)[2]) is not equal


@pytest.mark.parametrize(
    "type_name",
    [
        "/tmp/modules/Pool",
        "/tmp/modules/Pool.vy",
        r"C:\modules\Pool.vy",
        "DynArray[/tmp/modules/Pool.vy, 8]",
    ],
)
def test_compare_artifacts_preserves_matching_non_interface_paths_as_unknown(
    type_name: str,
) -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"pool": {"slot": 4, "type": type_name}}}},
    )
    target = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"pool": {"slot": 4, "type": type_name}}}},
    )

    assert unavailable_validation_artifacts(source) == ["abi", "method_identifiers"]
    assert unavailable_validation_artifacts(target) == ["abi", "method_identifiers"]
    assert compare_artifacts(source, target) == (None, None, True)


@pytest.mark.parametrize(
    "target_type",
    [
        "DynArray[/tmp/foo,bar/IPool.vyi, 8]",
        "DynArray[/tmp/foo[bar]/IPool.vyi, 8]",
    ],
)
def test_compare_artifacts_preserves_ambiguous_path_delimiters_fail_closed(
    target_type: str,
) -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"pools": {"slot": 4, "type": "DynArray[IPool, 8]"}}}
        },
    )
    target = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"pools": {"slot": 4, "type": target_type}}}},
    )

    assert unavailable_validation_artifacts(target) == [
        "abi",
        "method_identifiers",
        "layout",
    ]
    assert compare_artifacts(source, target) == (None, None, None)
    assert compare_artifact_details(source, target)[2] == []


def test_compare_artifacts_normalizes_nested_module_layouts() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "access": {
                        "hasRole": {
                            "slot": 0,
                            "type": "HashMap[bytes32, HashMap[address, bool]]",
                        },
                        "getRoleAdmin": {
                            "slot": 1,
                            "type": "HashMap[bytes32, bytes32]",
                        },
                    },
                    "vault": {
                        "accounting": {
                            "owner": {"slot": 2, "type": "address"},
                        }
                    },
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "access": {
                        "hasRole": {
                            "slot": "0x0",
                            "type": "HashMap[bytes32, HashMap[address, bool]]",
                        },
                        "getRoleAdmin": {
                            "slot": "1",
                            "type": "HashMap[bytes32, bytes32]",
                        },
                    },
                    "vault": {
                        "accounting": {
                            "owner": {"slot": "0x2", "type": "address"},
                        }
                    },
                },
                "code_layout": {"ignored": {"offset": 0, "type": "uint256", "length": 32}},
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)
    assert compare_artifact_details(source, target)[2] == []


def test_layout_artifact_accepts_code_only_immutables_as_empty_storage() -> None:
    immutable_only = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "code_layout": {
                    "OWNER": {"offset": 0, "type": "address", "length": 32},
                    "module": {"LIMIT": {"offset": 32, "type": "uint256", "length": 32}},
                }
            }
        },
    )
    empty_storage = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {}}},
    )

    assert unavailable_validation_artifacts(immutable_only) == ["abi", "method_identifiers"]
    assert compare_artifacts(immutable_only, empty_storage) == (None, None, True)
    assert compare_artifact_details(immutable_only, empty_storage)[2] == []


def test_layout_artifact_accepts_all_known_top_level_wrappers() -> None:
    result = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {"owner": {"slot": 0, "type": "address", "n_slots": 1}},
                "transient_storage_layout": {
                    "scratch": {"slot": 0, "type": "uint256", "n_slots": 1}
                },
                "code_layout": {"LIMIT": {"offset": 0, "type": "uint256", "length": 32}},
            }
        },
    )

    assert unavailable_validation_artifacts(result) == ["abi", "method_identifiers"]


@pytest.mark.parametrize(
    "unknown_sibling",
    [
        {"metadata": {}},
        {"owner": {"slot": 1, "type": "address", "n_slots": 1}},
    ],
)
def test_layout_artifact_rejects_unknown_top_level_wrapper_siblings(
    unknown_sibling: dict[str, object],
) -> None:
    result = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {"owner": {"slot": 0, "type": "address", "n_slots": 1}},
                **unknown_sibling,
            }
        },
    )

    assert unavailable_validation_artifacts(result) == [
        "abi",
        "method_identifiers",
        "layout",
    ]


@pytest.mark.parametrize(
    "code_layout",
    [
        {"OWNER": {"offset": 0, "type": "address"}},
        {"OWNER": {"offset": -1, "type": "address", "length": 32}},
        {"OWNER": {"offset": 0, "type": "address", "length": 0}},
        {
            "OWNER": {
                "offset": 0,
                "type": "address",
                "length": 32,
                "unknown": 1,
            }
        },
        {"module": {}},
    ],
)
def test_layout_artifact_rejects_malformed_code_layout(
    code_layout: dict[str, object],
) -> None:
    result = CompileResult("passed", artifacts={"layout": {"code_layout": code_layout}})

    assert unavailable_validation_artifacts(result) == [
        "abi",
        "method_identifiers",
        "layout",
    ]


@pytest.mark.parametrize(
    "layout",
    [
        {
            "storage_layout": {
                "owner": {"slot": 0, "type": "address", "n_slots": 1},
                "balance": {"slot": 1, "type": "uint256"},
            }
        },
        {
            "storage_layout": {"owner": {"slot": 0, "type": "address", "n_slots": 1}},
            "transient_storage_layout": {"scratch": {"slot": 0, "type": "uint256"}},
        },
    ],
)
def test_layout_artifact_rejects_mixed_n_slots_schema(
    layout: dict[str, object],
) -> None:
    result = CompileResult("passed", artifacts={"layout": layout})

    assert unavailable_validation_artifacts(result) == [
        "abi",
        "method_identifiers",
        "layout",
    ]


@pytest.mark.parametrize(
    "layout",
    [
        {
            "storage_layout": {
                "values": {
                    "slot": 2**256 - 1,
                    "type": "uint256[2]",
                    "n_slots": 2,
                }
            }
        },
        {"storage_layout": {"values": {"slot": 2**256 - 2, "type": "uint256[3]", "n_slots": 3}}},
    ],
)
def test_layout_artifact_rejects_storage_spans_past_uint256(
    layout: dict[str, object],
) -> None:
    result = CompileResult("passed", artifacts={"layout": layout})

    assert unavailable_validation_artifacts(result) == [
        "abi",
        "method_identifiers",
        "layout",
    ]


@pytest.mark.parametrize("wrapper", ["storage_layout", "transient_storage_layout"])
def test_layout_artifact_rejects_overlapping_ranges(wrapper: str) -> None:
    result = CompileResult(
        "passed",
        artifacts={
            "layout": {
                wrapper: {
                    "values": {"slot": 4, "type": "uint256[2]", "n_slots": 2},
                    "owner": {"slot": 5, "type": "address", "n_slots": 1},
                }
            }
        },
    )

    assert unavailable_validation_artifacts(result) == [
        "abi",
        "method_identifiers",
        "layout",
    ]


def test_compare_artifact_details_qualifies_nested_module_changes() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "left": {"owner": {"slot": 0, "type": "address"}},
                    "right": {"owner": {"slot": 1, "type": "address"}},
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "left": {"owner": {"slot": 0, "type": "address"}},
                    "right": {"owner": {"slot": 2, "type": "address"}},
                }
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)
    assert compare_artifact_details(source, target)[2] == [
        "changed storage: right.owner slot 1 address -> 2 address"
    ]


@pytest.mark.parametrize("malformed_side", ["source", "target"])
@pytest.mark.parametrize(
    "malformed_layout",
    [
        {"storage_layout": {"vault": {"owner": {"slot": 0}}}},
        {"storage_layout": {"vault": {"owner": {"slot": 0, "type": "address", "n_slots": None}}}},
        {
            "storage_layout": {
                "vault": {
                    "owner": {
                        "slot": 0,
                        "type": "address",
                        "nested": {"balance": {"slot": 1, "type": "uint256"}},
                    }
                }
            }
        },
    ],
)
def test_compare_artifacts_rejects_malformed_nested_module_layouts(
    malformed_side: str, malformed_layout: dict[str, object]
) -> None:
    valid_layout = {
        "storage_layout": {
            "vault": {"owner": {"slot": 0, "type": "address"}},
        }
    }
    source_layout = malformed_layout if malformed_side == "source" else valid_layout
    target_layout = malformed_layout if malformed_side == "target" else valid_layout
    source = CompileResult("passed", artifacts={"layout": source_layout})
    target = CompileResult("passed", artifacts={"layout": target_layout})

    malformed = source if malformed_side == "source" else target
    assert unavailable_validation_artifacts(malformed) == ["abi", "method_identifiers", "layout"]
    assert compare_artifacts(source, target) == (None, None, None)
    assert compare_artifact_details(source, target)[2] == []


def test_compare_artifacts_rejects_changed_unknown_storage_leaf_scalar() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "owner": {
                        "slot": 0,
                        "type": "address",
                        "n_slots": 1,
                        "compiler_metadata": 1,
                    }
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "owner": {
                        "slot": 0,
                        "type": "address",
                        "n_slots": 1,
                        "compiler_metadata": 2,
                    }
                }
            }
        },
    )

    assert unavailable_validation_artifacts(source) == [
        "abi",
        "method_identifiers",
        "layout",
    ]
    assert unavailable_validation_artifacts(target) == [
        "abi",
        "method_identifiers",
        "layout",
    ]
    assert compare_artifacts(source, target) == (None, None, None)


def test_compare_artifacts_rejects_empty_nested_storage_namespace() -> None:
    malformed = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"vault": {"owner": {}}}}},
    )
    valid = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"vault": {"owner": {"slot": 0, "type": "address"}}}}
        },
    )

    assert unavailable_validation_artifacts(malformed) == ["abi", "method_identifiers", "layout"]
    assert compare_artifacts(malformed, valid) == (None, None, None)


@pytest.mark.parametrize(
    "layout",
    [
        {"storage_layout": {"owner": {"slot": 0, "type": "address", "location": "transient"}}},
        {"storage_layout": {"owner": {"slot": 0, "type": "address", "location": "memory"}}},
        {
            "storage_layout": {},
            "transient_storage_layout": {
                "scratch": {"slot": 0, "type": "uint256", "location": "storage"}
            },
        },
    ],
)
def test_layout_artifact_rejects_leaf_location_outside_containing_namespace(
    layout: dict[str, object],
) -> None:
    result = CompileResult("passed", artifacts={"layout": layout})

    assert unavailable_validation_artifacts(result) == ["abi", "method_identifiers", "layout"]


def test_compare_artifacts_compares_n_slots_when_both_layouts_supply_it() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {"values": {"slot": 0, "type": "CustomStruct", "n_slots": 1}}
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {"values": {"slot": 0, "type": "CustomStruct", "n_slots": 2}}
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)
    assert compare_artifact_details(source, target)[2] == [
        "changed storage: values slot 0 CustomStruct -> 0 CustomStruct (n_slots 1 -> 2)"
    ]


def test_compare_artifacts_infers_known_width_when_legacy_layout_omits_it() -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"values": {"slot": 0, "type": "uint256"}}},
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"values": {"slot": 0, "type": "uint256", "n_slots": 1}}}
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)


def test_layout_artifact_rejects_explicit_width_for_known_scalar() -> None:
    result = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"values": {"slot": 0, "type": "uint256", "n_slots": 2}}}
        },
    )

    assert unavailable_validation_artifacts(result) == [
        "abi",
        "method_identifiers",
        "layout",
    ]


@pytest.mark.parametrize(
    ("type_name", "n_slots"),
    [
        ("bool", 1),
        ("address", 1),
        ("uint256", 1),
        ("bytes32", 1),
        ("HashMap[address, uint256]", 1),
        ("uint256[3]", 3),
        ("address[2][3]", 6),
        ("DynArray[uint256, 3]", 4),
        ("DynArray[uint256[2], 3]", 7),
        ("DynArray[uint256, 3][3]", 12),
        ("DynArray[DynArray[uint256, 3][3], 3][5]", 185),
        ("Bytes[64]", 3),
        ("String[32]", 2),
    ],
)
def test_compare_artifacts_infers_known_legacy_storage_widths(
    type_name: str,
    n_slots: int,
) -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"value": {"slot": 0, "type": type_name}}},
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {"value": {"slot": 0, "type": type_name, "n_slots": n_slots}}
            }
        },
    )

    assert unavailable_validation_artifacts(source) == ["abi", "method_identifiers"]
    assert unavailable_validation_artifacts(target) == ["abi", "method_identifiers"]
    assert compare_artifacts(source, target) == (None, None, True)


def test_compare_artifacts_infers_explicit_legacy_interface_width() -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"pool": {"slot": 0, "type": "interface Pool"}}},
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"pool": {"slot": 0, "type": "Pool.vyi", "n_slots": 1}}}
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)


@pytest.mark.parametrize(
    (
        "source_type",
        "target_type",
        "target_n_slots",
        "interface_names",
        "target_annotation",
    ),
    [
        (
            "interface Pool",
            "Pool",
            1,
            ("Pool",),
            {"ast_type": "Name", "id": "Pool"},
        ),
        (
            "HashMap[address, interface Pool]",
            "HashMap[address, Pool]",
            None,
            ("Pool",),
            {
                "ast_type": "Subscript",
                "value": {"ast_type": "Name", "id": "HashMap"},
                "slice": {
                    "ast_type": "Tuple",
                    "elements": [
                        {"ast_type": "Name", "id": "address"},
                        {"ast_type": "Name", "id": "Pool"},
                    ],
                },
            },
        ),
        (
            "DynArray[interface Pool, 8]",
            "DynArray[Pool, 8]",
            9,
            ("Pool",),
            {
                "ast_type": "Subscript",
                "value": {"ast_type": "Name", "id": "DynArray"},
                "slice": {
                    "ast_type": "Tuple",
                    "elements": [
                        {"ast_type": "Name", "id": "Pool"},
                        {"ast_type": "Int", "value": 8},
                    ],
                },
            },
        ),
        (
            "interface Pool[3]",
            "Pool[3]",
            3,
            ("Pool",),
            {
                "ast_type": "Subscript",
                "value": {"ast_type": "Name", "id": "Pool"},
                "slice": {"ast_type": "Int", "value": 3},
            },
        ),
        (
            "HashMap[interface IERC20, interface AuctionZap]",
            "HashMap[interface IERC20, AuctionZap]",
            None,
            ("AuctionZap",),
            {
                "ast_type": "Subscript",
                "value": {"ast_type": "Name", "id": "HashMap"},
                "slice": {
                    "ast_type": "Tuple",
                    "elements": [
                        {"ast_type": "Name", "id": "IERC20"},
                        {"ast_type": "Name", "id": "AuctionZap"},
                    ],
                },
            },
        ),
    ],
)
def test_compare_artifacts_accepts_pairwise_interface_marker_omission(
    source_type: str,
    target_type: str,
    target_n_slots: int | None,
    interface_names: tuple[str, ...],
    target_annotation: dict[str, object],
) -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"value": {"slot": 0, "type": source_type}}},
    )
    target_entry: dict[str, object] = {"slot": 0, "type": target_type}
    if target_n_slots is not None:
        target_entry["n_slots"] = target_n_slots
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"value": target_entry}},
            "ast": {
                "ast_type": "Module",
                "body": [
                    *({"ast_type": "InterfaceDef", "name": name} for name in interface_names),
                    {
                        "ast_type": "VariableDecl",
                        "target": {"ast_type": "Name", "id": "value"},
                        "annotation": target_annotation,
                    },
                ],
            },
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)
    assert compare_artifact_details(source, target)[2] == []


@pytest.mark.parametrize(
    "target_body",
    [
        [],
        [{"ast_type": "StructDef", "name": "Pool"}],
        [
            {
                "ast_type": "FunctionDef",
                "name": "unrelated",
                "body": [{"ast_type": "InterfaceDef", "name": "Pool"}],
            }
        ],
    ],
)
def test_compare_artifacts_requires_top_level_target_interface_evidence(
    target_body: list[dict[str, object]],
) -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "value": {
                    "slot": 0,
                    "type": "HashMap[address, interface Pool]",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"value": {"slot": 0, "type": "HashMap[address, Pool]"}}},
            "ast": {
                "ast_type": "Module",
                "body": [
                    *target_body,
                    {
                        "ast_type": "VariableDecl",
                        "target": {"ast_type": "Name", "id": "value"},
                        "annotation": {
                            "ast_type": "Subscript",
                            "value": {"ast_type": "Name", "id": "HashMap"},
                            "slice": {
                                "ast_type": "Tuple",
                                "elements": [
                                    {"ast_type": "Name", "id": "address"},
                                    {"ast_type": "Name", "id": "Pool"},
                                ],
                            },
                        },
                    },
                ],
            },
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)


@pytest.mark.parametrize(
    "target_ast",
    [
        None,
        [],
        {},
        {"ast_type": "Module", "body": "invalid"},
        {"ast": {"ast_type": "StructDef", "body": []}},
    ],
)
def test_compare_artifacts_rejects_missing_or_malformed_target_ast(
    target_ast: object,
) -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "value": {
                    "slot": 0,
                    "type": "HashMap[address, interface Pool]",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"value": {"slot": 0, "type": "HashMap[address, Pool]"}}},
            "ast": target_ast,
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)


def test_compare_artifacts_does_not_apply_root_evidence_to_nested_layout_key() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "child": {
                        "values": {
                            "slot": 0,
                            "type": "HashMap[address, interface Pool]",
                        }
                    }
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "child": {
                        "values": {
                            "slot": 0,
                            "type": "HashMap[address, Pool]",
                        }
                    }
                }
            },
            "ast": {
                "ast_type": "Module",
                "body": [
                    {"ast_type": "InterfaceDef", "name": "Pool"},
                    {
                        "ast_type": "VariableDecl",
                        "target": {"ast_type": "Name", "id": "values"},
                        "annotation": {
                            "ast_type": "Subscript",
                            "value": {"ast_type": "Name", "id": "HashMap"},
                            "slice": {
                                "ast_type": "Tuple",
                                "elements": [
                                    {"ast_type": "Name", "id": "address"},
                                    {"ast_type": "Name", "id": "Pool"},
                                ],
                            },
                        },
                    },
                ],
            },
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)


@pytest.mark.parametrize(
    ("source_type", "target_type", "target_n_slots"),
    [
        ("interface IERC20", "ERC20", 1),
        ("interface Pool", "Pool", 2),
        (
            "HashMap[interface Foo, Bar]",
            "HashMap[Foo, interface Bar]",
            None,
        ),
        (
            "HashMap[address, Pool]",
            "HashMap[address, interface Pool]",
            None,
        ),
    ],
)
def test_compare_artifacts_rejects_interface_marker_aliases_and_width_changes(
    source_type: str,
    target_type: str,
    target_n_slots: int | None,
) -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"value": {"slot": 0, "type": source_type}}},
    )
    target_entry: dict[str, object] = {"slot": 0, "type": target_type}
    if target_n_slots is not None:
        target_entry["n_slots"] = target_n_slots
    target = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"value": target_entry}}},
    )

    assert compare_artifacts(source, target) == (None, None, False)
    assert compare_artifact_details(source, target)[2]


@pytest.mark.parametrize(
    "source_type",
    [
        "/tmp/site-packages/vyper/builtins/interfaces/IERC20.vyi[3]",
        "IERC20.vyi[3]",
    ],
)
def test_compare_artifacts_infers_fixed_array_of_vyi_interface_width(
    source_type: str,
) -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"tokens": {"slot": 0, "type": source_type}}},
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "tokens": {
                        "slot": 0,
                        "type": "interface IERC20[3]",
                        "n_slots": 3,
                    }
                }
            }
        },
    )

    assert unavailable_validation_artifacts(source) == ["abi", "method_identifiers"]
    assert unavailable_validation_artifacts(target) == ["abi", "method_identifiers"]
    assert compare_artifacts(source, target) == (None, None, True)


def test_compare_artifacts_propagates_delimited_vyi_evidence_into_dynarray() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "pools": {
                    "slot": 0,
                    "type": "DynArray[interface Pool, 8]",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "pools": {
                        "slot": 0,
                        "type": "DynArray[Pool.vyi, 8]",
                        "n_slots": 9,
                    }
                }
            }
        },
    )

    assert unavailable_validation_artifacts(source) == ["abi", "method_identifiers"]
    assert unavailable_validation_artifacts(target) == ["abi", "method_identifiers"]
    assert compare_artifacts(source, target) == (None, None, True)


def test_compare_artifacts_rejects_one_sided_unknown_storage_width() -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"value": {"slot": 0, "type": "CustomStruct"}}},
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {"value": {"slot": 0, "type": "CustomStruct", "n_slots": 2}}
            }
        },
    )

    assert unavailable_validation_artifacts(source) == ["abi", "method_identifiers"]
    assert unavailable_validation_artifacts(target) == ["abi", "method_identifiers"]
    assert compare_artifacts(source, target) == (None, None, False)
    assert compare_artifact_details(source, target)[2] == [
        "changed storage: value slot 0 CustomStruct -> 0 CustomStruct (n_slots unknown -> 2)"
    ]


def test_compare_artifacts_accepts_symmetric_unknown_omitted_widths() -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"value": {"slot": 0, "type": "CustomStruct"}}},
    )
    target = CompileResult(
        "passed",
        artifacts={"layout": {"value": {"slot": 0, "type": "CustomStruct"}}},
    )

    assert compare_artifacts(source, target) == (None, None, True)


@pytest.mark.parametrize(
    "storage_layout",
    [
        {
            "a.b": {"slot": 0, "type": "address"},
            "a": {"b": {"slot": 1, "type": "address"}},
        },
        {
            "nonreentrant.lock": {"slot": 0, "type": "nonreentrant lock"},
            "_vyupgrade_reentrancy_lock_slot": {"slot": 0, "type": "uint256"},
        },
    ],
)
def test_layout_artifact_rejects_flattened_and_normalized_name_collisions(
    storage_layout: dict[str, object],
) -> None:
    result = CompileResult("passed", artifacts={"layout": {"storage_layout": storage_layout}})

    assert unavailable_validation_artifacts(result) == ["abi", "method_identifiers", "layout"]


def test_layout_artifact_distinguishes_legacy_storage_layout_variable_from_wrapper() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "location": "storage",
                    "slot": 0,
                    "type": "uint256",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {"storage_layout": {"slot": 0, "type": "uint256", "n_slots": 1}}
            }
        },
    )

    assert unavailable_validation_artifacts(source) == ["abi", "method_identifiers"]
    assert compare_artifacts(source, target) == (None, None, True)


@pytest.mark.parametrize(
    ("name", "location"),
    [
        ("storage_layout", "storage"),
        ("transient_storage_layout", "transient"),
        ("code_layout", "storage"),
    ],
)
def test_layout_artifact_preserves_explicit_legacy_wrapper_named_variables(
    name: str,
    location: str,
) -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                name: {
                    "location": location,
                    "slot": 0,
                    "type": "uint256",
                }
            }
        },
    )
    target_layout: dict[str, object] = {"storage_layout": {}}
    wrapper = "transient_storage_layout" if location == "transient" else "storage_layout"
    target_layout[wrapper] = {name: {"slot": 0, "type": "uint256", "n_slots": 1}}
    target = CompileResult("passed", artifacts={"layout": target_layout})

    assert unavailable_validation_artifacts(source) == ["abi", "method_identifiers"]
    assert unavailable_validation_artifacts(target) == ["abi", "method_identifiers"]
    assert compare_artifacts(source, target) == (None, None, True)


@pytest.mark.parametrize(
    ("name", "node"),
    [
        ("storage_layout", {"slot": 0, "type": "uint256"}),
        ("transient_storage_layout", {"slot": 0, "type": "uint256"}),
        (
            "code_layout",
            {"offset": 0, "type": "uint256", "length": 32},
        ),
        (
            "storage_layout",
            {"location": "memory", "slot": 0, "type": "uint256"},
        ),
    ],
)
def test_layout_artifact_rejects_truncated_or_ambiguous_wrapper_named_roots(
    name: str,
    node: dict[str, object],
) -> None:
    result = CompileResult("passed", artifacts={"layout": {name: node}})

    assert unavailable_validation_artifacts(result) == [
        "abi",
        "method_identifiers",
        "layout",
    ]


@pytest.mark.parametrize("slot", [2**256, str(2**256), hex(2**256)])
def test_layout_artifact_rejects_slots_outside_uint256(slot: int | str) -> None:
    result = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"owner": {"slot": slot, "type": "address"}}}},
    )

    assert unavailable_validation_artifacts(result) == ["abi", "method_identifiers", "layout"]


def test_compare_artifacts_compares_transient_layouts_explicitly() -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"scratch": {"location": "transient", "slot": 0, "type": "uint256"}}},
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {},
                "transient_storage_layout": {"scratch": {"slot": 1, "type": "address"}},
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)
    assert compare_artifact_details(source, target)[2] == [
        "changed transient storage: scratch slot 0 uint256 -> 1 address"
    ]


@pytest.mark.parametrize("target_slot", ["16", "0x10"])
def test_compare_artifacts_normalizes_numeric_string_storage_slots(target_slot: str) -> None:
    source = CompileResult(
        "passed",
        artifacts={"layout": {"storage_layout": {"owner": {"slot": 16, "type": "address"}}}},
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {"storage_layout": {"owner": {"slot": target_slot, "type": "address"}}}
        },
    )

    assert unavailable_validation_artifacts(target) == ["abi", "method_identifiers"]
    assert compare_artifacts(source, target) == (None, None, True)
    assert compare_artifact_details(source, target)[2] == []


def test_compare_artifacts_flags_real_storage_slot_shift() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.withdraw": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "balance": {"slot": 0, "type": "uint256", "n_slots": 1},
                },
                "transient_storage_layout": {
                    "$.nonreentrant_key": {
                        "slot": 0,
                        "type": "nonreentrant lock",
                        "n_slots": 1,
                    },
                },
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)


def test_compare_artifacts_normalizes_storage_nonreentrant_lock_names() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "$.nonreentrant_key": {
                        "slot": 0,
                        "type": "nonreentrant lock",
                        "n_slots": 1,
                    },
                    "balance": {"slot": 1, "type": "uint256", "n_slots": 1},
                }
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)


def test_compare_artifacts_normalizes_generated_nonreentrant_storage_gap() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "_vyupgrade_reentrancy_lock_slot": {
                        "slot": 0,
                        "type": "uint256",
                        "n_slots": 1,
                    },
                    "balance": {"slot": 1, "type": "uint256", "n_slots": 1},
                }
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)


def test_compare_artifacts_does_not_treat_prefixed_user_variable_as_lock() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "_vyupgrade_reentrancy_lock_slot_user_data": {
                    "location": "storage",
                    "slot": 0,
                    "type": "uint256",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "$.nonreentrant_key": {
                        "slot": 0,
                        "type": "nonreentrant lock",
                        "n_slots": 1,
                    }
                }
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)
    assert compare_artifact_details(source, target)[2] == [
        "removed storage: _vyupgrade_reentrancy_lock_slot_user_data slot 0 uint256",
        "added storage: $nonreentrant:0 slot 0 nonreentrant lock",
    ]


def test_compare_artifacts_requires_exact_generated_gap_name_and_lock_counterpart() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "_vyupgrade_reentrancy_lock_slot_user_data": {
                        "slot": 0,
                        "type": "uint256",
                        "n_slots": 1,
                    }
                }
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)


def test_compare_artifacts_rejects_generated_gap_with_non_singleton_span() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "_vyupgrade_reentrancy_lock_slot": {
                        "slot": 0,
                        "type": "uint256",
                        "n_slots": 2,
                    }
                }
            }
        },
    )

    assert unavailable_validation_artifacts(target) == [
        "abi",
        "method_identifiers",
        "layout",
    ]
    assert compare_artifacts(source, target) == (None, None, None)


def test_compare_artifacts_does_not_hide_gap_when_lock_moved_to_transient() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "_vyupgrade_reentrancy_lock_slot": {
                        "slot": 0,
                        "type": "uint256",
                        "n_slots": 1,
                    }
                },
                "transient_storage_layout": {
                    "$.nonreentrant_key": {
                        "slot": 0,
                        "type": "nonreentrant lock",
                        "n_slots": 1,
                    }
                },
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)
    assert compare_artifact_details(source, target)[2] == [
        "moved storage to transient: $nonreentrant:0 slot 0 nonreentrant lock -> $nonreentrant:0 slot 0 nonreentrant lock",
        "added storage: _vyupgrade_reentrancy_lock_slot slot 0 uint256",
    ]


def test_compare_artifacts_treats_transient_only_nonreentrant_move_as_persistent_equal() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {},
                "transient_storage_layout": {
                    "$.nonreentrant_key": {"slot": 0, "type": "nonreentrant lock", "n_slots": 1},
                },
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)
    assert compare_artifact_details(source, target)[2] == [
        "moved storage to transient: $nonreentrant:0 slot 0 nonreentrant lock -> $nonreentrant:0 slot 0 nonreentrant lock",
    ]


def test_compare_artifact_details_reports_nonreentrant_lock_moved_to_transient_storage() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "balance": {"slot": 0, "type": "uint256", "n_slots": 1},
                },
                "transient_storage_layout": {
                    "$.nonreentrant_key": {"slot": 0, "type": "nonreentrant lock", "n_slots": 1},
                },
            }
        },
    )

    _abi_diff, _method_diff, storage_diff = compare_artifact_details(source, target)

    assert storage_diff == [
        "moved storage to transient: $nonreentrant:0 slot 0 nonreentrant lock -> $nonreentrant:0 slot 0 nonreentrant lock",
        "changed storage: balance slot 1 uint256 -> 0 uint256",
    ]


def test_compare_artifacts_normalizes_legacy_storage_type_names() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "is_killed": {
                        "location": "storage",
                        "slot": 16,
                        "type": "HashMap[ERC20, enum Epoch('SLEEP','COLLECT','EXCHANGE','FORWARD')]",
                    },
                    "permission": {
                        "location": "storage",
                        "slot": 17,
                        "type": "flag Permission('READ','WRITE')",
                    },
                    "permissions": {
                        "location": "storage",
                        "slot": 18,
                        "type": "HashMap[address, flag Permission('READ','WRITE')]",
                    },
                    "get_gauge": {
                        "location": "storage",
                        "slot": 19,
                        "type": "address[115792089237316195423570985008687907853269984665640564039457584007913129639935]",
                    },
                }
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "is_killed": {
                        "slot": 16,
                        "type": "HashMap[ERC20, Epoch]",
                    },
                    "permission": {
                        "slot": 17,
                        "type": "Permission",
                    },
                    "permissions": {
                        "slot": 18,
                        "type": "HashMap[address, Permission]",
                    },
                    "get_gauge": {
                        "slot": 19,
                        "type": "HashMap[uint256, address]",
                    },
                }
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)
    assert compare_artifact_details(source, target)[2] == []


def test_compare_artifact_details_reports_changed_selectors_and_storage() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "method_identifiers": {
                "old()": "0x11111111",
                "shared()": "0x22222222",
            },
            "layout": {
                "owner": {"location": "storage", "slot": 0, "type": "address"},
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            },
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "method_identifiers": {
                "shared()": "0x33333333",
                "new()": "0x44444444",
            },
            "layout": {
                "storage_layout": {
                    "balance": {"slot": 0, "type": "uint256"},
                    "recipient": {"slot": 1, "type": "address"},
                },
            },
        },
    )

    _abi_diff, method_diff, storage_diff = compare_artifact_details(source, target)

    assert method_diff == [
        "removed selector: old() = 0x11111111",
        "added selector: new() = 0x44444444",
        "changed selector: shared() 0x22222222 -> 0x33333333",
    ]
    assert storage_diff == [
        "removed storage: owner slot 0 address",
        "added storage: recipient slot 1 address",
        "changed storage: balance slot 1 uint256 -> 0 uint256",
    ]


def test_compare_artifact_details_reports_full_storage_changes() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                f"slot_{index:02d}": {
                    "location": "storage",
                    "slot": index,
                    "type": "uint256",
                }
                for index in range(15)
            },
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    f"slot_{index:02d}": {"slot": index + 1, "type": "uint256"}
                    for index in range(15)
                },
            },
        },
    )

    _abi_diff, _method_diff, storage_diff = compare_artifact_details(source, target)

    assert len(storage_diff) == 15
    assert storage_diff[-1] == "changed storage: slot_14 slot 14 uint256 -> 15 uint256"
    assert not any(line.startswith("... ") for line in storage_diff)


def test_compile_target_archive_requires_overlay_staging(tmp_path: Path, monkeypatch) -> None:
    entry = tmp_path / "main.vy"
    entry.write_text("#pragma version 0.4.3\n")
    overlay = TargetOverlay(tmp_path / "overlay", {}, (), ())

    def unexpected_run(*_args, **_kwargs):
        pytest.fail("archive compiler must not run without a staged entry")

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", unexpected_run)

    result = compile_target_archive(
        entry,
        entry.read_text(),
        Config(paths=(entry,), target_vyper="vyper"),
        overlay,
        tmp_path / "out.vyz",
    )

    assert result.status == "failed"
    assert result.stderr is not None
    assert "not staged" in result.stderr


def test_compile_target_archive_replaces_existing_output(tmp_path: Path, monkeypatch) -> None:
    entry = tmp_path / "main.vy"
    staged = tmp_path / "overlay" / "main.vy"
    staged.parent.mkdir()
    staged.write_text("#pragma version 0.4.3\n")
    output = tmp_path / "out.vyz"
    output.write_bytes(b"old archive")
    overlay = TargetOverlay(
        staged.parent,
        {entry.resolve(): staged},
        (entry.parent.resolve(),),
        (staged.parent,),
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], *_args, **_kwargs):
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_bytes(b"new archive")
        return _compiler_process(command, stdout="")

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = compile_target_archive(
        entry,
        staged.read_text(),
        Config(paths=(entry,), target_vyper="vyper"),
        overlay,
        output,
    )

    assert result.status == "passed"
    assert result.artifacts == {"archive": str(output)}
    assert output.read_bytes() == b"new archive"
    assert calls[0][-1] == "main.vy"
    assert calls[0][calls[0].index("-f") + 1] == "archive"
    assert list(tmp_path.glob(".out.vyz.vyupgrade-*")) == []


def test_compile_target_archive_cleans_temporary_output_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    entry = tmp_path / "main.vy"
    staged = tmp_path / "overlay" / "main.vy"
    staged.parent.mkdir()
    staged.write_text("#pragma version 0.4.3\n")
    output = tmp_path / "out.vyz"
    overlay = TargetOverlay(
        staged.parent,
        {entry.resolve(): staged},
        (entry.parent.resolve(),),
        (staged.parent,),
    )

    def fake_run(command: list[str], *_args, **_kwargs):
        Path(command[command.index("-o") + 1]).write_bytes(b"partial")
        return _compiler_process(command, returncode=1, stdout="", stderr="archive failed")

    monkeypatch.setattr("vyupgrade.compiler._run_compiler_process", fake_run)

    result = compile_target_archive(
        entry,
        staged.read_text(),
        Config(paths=(entry,), target_vyper="vyper"),
        overlay,
        output,
    )

    assert result.status == "failed"
    assert result.stderr == "archive failed"
    assert not output.exists()
    assert list(tmp_path.glob(".out.vyz.vyupgrade-*")) == []


def test_archive_target_floor_is_known_version() -> None:
    assert parse_version(ARCHIVE_TARGET_FLOOR) in KNOWN_VERSIONS
