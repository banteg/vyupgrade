from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from vyupgrade import compiler, engine
from vyupgrade.compiler import CompileResult
from vyupgrade.engine import MigrationRequest, SourceCompileAttempt
from vyupgrade.models import Config, Diagnostic, Fix, GeneratedFile, RewriteResult


VALIDATION_ARTIFACTS = {"abi": [], "method_identifiers": {}, "layout": {}}
SOURCE_ARTIFACTS = {**VALIDATION_ARTIFACTS, "ast": {}}


def _config(tmp_path: Path, **kwargs) -> Config:
    values = {"paths": (tmp_path,), "target_version": "0.4.3"}
    values.update(kwargs)
    return Config(**values)


def _request(
    path: Path,
    source_version: str = "0.3.10",
    attempts: tuple[SourceCompileAttempt, ...] | None = None,
) -> MigrationRequest:
    return MigrationRequest(
        path,
        "#pragma version 0.3.10\nx: uint256\n",
        source_version,
        attempts or (SourceCompileAttempt(source_version, source_version),),
    )


def _unchanged_rewrite(source, config, path) -> RewriteResult:
    return RewriteResult(source, [], [])


def test_bounded_request_preserves_cli_compiler_selection(tmp_path: Path) -> None:
    path = tmp_path / "Contract.vy"
    source = "#pragma version >0.3.10\nx: uint256\n"

    inferred = engine.bounded_migration_request(path, source, _config(tmp_path))
    explicit = engine.bounded_migration_request(
        path, source, _config(tmp_path, source_vyper="/tmp/vyper")
    )

    assert inferred.source_attempts == (SourceCompileAttempt("0.4.3", ">0.3.10", "0.4.3"),)
    assert explicit.source_attempts == (SourceCompileAttempt(">0.3.10", ">0.3.10"),)


def test_prepare_uses_first_passing_retry_and_its_rule_version(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "Contract.vy"
    attempts = tuple(
        SourceCompileAttempt(version, version) for version in ("0.3.0", "0.3.1", "0.3.2")
    )
    seen: dict[str, object] = {"compile": []}

    def compile_source(path, config, source_version):
        seen["compile"].append(source_version)
        if source_version == "0.3.0":
            return CompileResult("failed", stderr="first failed")
        return CompileResult("passed", artifacts=SOURCE_ARTIFACTS)

    def apply(source, config, path):
        seen["rule_version"] = config.source_version
        return RewriteResult(source, [], [])

    monkeypatch.setattr(engine, "compile_source_file", compile_source)
    monkeypatch.setattr(engine, "apply_rules", apply)

    batch = engine.prepare_migrations((_request(path, "0.3.0", attempts),), _config(tmp_path))

    assert seen == {"compile": ["0.3.0", "0.3.1"], "rule_version": "0.3.1"}
    assert batch.files[0].source_version == "0.3.1"
    assert batch.files[0].source_compile.status == "passed"


def test_prepare_retains_first_failure_when_all_retries_fail(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "Contract.vy"
    attempts = tuple(
        SourceCompileAttempt(version, version) for version in ("0.3.0", "0.3.1", "0.3.2")
    )
    seen: dict[str, object] = {"compile": []}

    def compile_source(path, config, source_version):
        seen["compile"].append(source_version)
        return CompileResult("failed", stderr=f"failed {source_version}")

    def apply(source, config, path):
        seen["rule_version"] = config.source_version
        return RewriteResult(source, [], [])

    monkeypatch.setattr(engine, "compile_source_file", compile_source)
    monkeypatch.setattr(engine, "apply_rules", apply)

    batch = engine.prepare_migrations((_request(path, "0.3.0", attempts),), _config(tmp_path))

    assert seen == {
        "compile": ["0.3.0", "0.3.1", "0.3.2"],
        "rule_version": "0.3.0",
    }
    assert batch.files[0].source_compile.stderr == "failed 0.3.0"


def test_source_ast_is_owned_by_each_file(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "First.vy"
    second = tmp_path / "Second.vy"
    asts: dict[str, object] = {}

    def compile_source(path, config, source_version):
        artifacts = dict(VALIDATION_ARTIFACTS)
        if path == first:
            artifacts["ast"] = {"owner": "first"}
        return CompileResult("passed", artifacts=artifacts)

    def apply(source, config, path):
        asts[path.name] = config.source_ast
        return RewriteResult(source, [], [])

    monkeypatch.setattr(engine, "compile_source_file", compile_source)
    monkeypatch.setattr(engine, "apply_rules", apply)
    config = _config(tmp_path, source_ast={"stale": True})

    engine.prepare_migrations((_request(first), _request(second)), config)

    assert asts == {"First.vy": {"owner": "first"}, "Second.vy": None}
    assert config.source_ast == {"stale": True}


@pytest.mark.parametrize(
    ("source_version", "target_version", "diagnostic_code"),
    [
        (">=0.5.0a1,<0.6.0", "0.4.3", "VYD016"),
        ("0.4.4", "0.5.0a3", "VYD018"),
    ],
)
def test_blocked_source_cannot_validate_or_split_when_diagnostic_is_ignored(
    source_version: str,
    target_version: str,
    diagnostic_code: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "Main.vy"
    original = f"""#pragma version {source_version}

interface Token:
    def balanceOf(owner: address) -> uint256: view
"""
    request = MigrationRequest(path, original, source_version, ())
    config = _config(
        tmp_path,
        target_version=target_version,
        split_interfaces=True,
        ignore=frozenset({diagnostic_code}),
    )

    batch = engine.prepare_migrations((request,), config)
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args, **_kwargs: pytest.fail("blocked source reached target compiler"),
    )
    decision = engine.validate_migrations(batch, config)

    assert batch.files[0].rewrite.source == original
    assert batch.files[0].report.diagnostics == []
    assert batch.files[0].report.changed is False
    assert batch.files[0].report.source_compile == "skipped"
    assert batch.files[0].report.target_compile == "skipped"
    assert decision.status == "not-required"
    assert batch.generated == []


def test_candidate_override_and_revalidation_reset_only_engine_diagnostics(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "Contract.vy"
    rule_diagnostic = Diagnostic("VYD007", 9, "rule-owned diagnostic")
    compiled: list[str] = []

    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )
    monkeypatch.setattr(
        engine,
        "apply_rules",
        lambda source, config, path: RewriteResult(source + "# rewritten\n", [], [rule_diagnostic]),
    )

    def compile_target(path, source, config, overlay):
        compiled.append(source)
        artifacts = (
            {
                **VALIDATION_ARTIFACTS,
                "abi": [{"type": "function", "name": "changed", "inputs": []}],
            }
            if len(compiled) == 1
            else VALIDATION_ARTIFACTS
        )
        return CompileResult("passed", artifacts=artifacts)

    monkeypatch.setattr(engine, "compile_target_source", compile_target)
    batch = engine.prepare_migrations((_request(path),), _config(tmp_path))

    first = engine.validate_migrations(
        batch, _config(tmp_path), lambda _path, _fallback: "# formatted one\n"
    )
    second = engine.validate_migrations(
        batch, _config(tmp_path), lambda _path, _fallback: "# formatted two\n"
    )

    assert compiled == ["# formatted one\n", "# formatted two\n"]
    assert first.status == "blocked"
    assert second.status == "passed"
    assert batch.files[0].report.abi_diff == []
    assert [
        diagnostic
        for diagnostic in batch.files[0].report.diagnostics
        if diagnostic.rule == "VYD007"
    ] == [rule_diagnostic]


def test_generated_interface_and_cross_file_sources_share_one_overlay(
    monkeypatch, tmp_path: Path
) -> None:
    first = tmp_path / "First.vy"
    second = tmp_path / "Second.vy"
    interface = tmp_path / "Shared.vyi"
    overlay_sources: dict[Path, str] = {}
    compiled: list[tuple[Path, object]] = []
    overlay_token = object()

    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )

    def apply(source, config, path):
        generated = []
        if path == first:
            generated.append(
                GeneratedFile(
                    interface,
                    "@external\ndef ping(): ...\n",
                    Fix("VY120", 1, "split interface", "", ""),
                )
            )
        return RewriteResult(source, [], [], generated)

    @contextmanager
    def overlay(sources, target_version, search_paths, *, include_dependencies=False):
        assert include_dependencies is False
        overlay_sources.update(sources)
        yield overlay_token

    def compile_target(path, source, config, overlay):
        compiled.append((path, overlay))
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    monkeypatch.setattr(engine, "apply_rules", apply)
    monkeypatch.setattr(engine, "target_overlay", overlay)
    monkeypatch.setattr(engine, "compile_target_source", compile_target)
    batch = engine.prepare_migrations((_request(first), _request(second)), _config(tmp_path))

    decision = engine.validate_migrations(batch, _config(tmp_path))

    assert set(overlay_sources) == {first, second, interface}
    assert {path for path, _overlay in compiled} == {first, second, interface}
    assert all(used_overlay is overlay_token for _path, used_overlay in compiled)
    assert batch.generated[0].report.target_compile == "passed"
    assert decision.status == "passed"


def test_validation_rejects_duplicate_resolved_destinations(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "First.vy"
    second = tmp_path / "Second.vy"
    interface = tmp_path / "Shared.vyi"

    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )

    def apply(source, config, path):
        declaration = "one" if path == first else "two"
        generated = GeneratedFile(
            interface,
            f"@external\ndef {declaration}(): ...\n",
            Fix("VY120", 1, "split interface", "", ""),
        )
        return RewriteResult(source, [], [], [generated])

    monkeypatch.setattr(engine, "apply_rules", apply)
    batch = engine.prepare_migrations((_request(first), _request(second)), _config(tmp_path))

    with pytest.raises(engine.CandidatePathConflictError, match=r"Shared\.vyi"):
        engine.validate_migrations(batch, _config(tmp_path))


def test_malformed_target_artifacts_remain_unwaivable(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "Contract.vy"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )
    monkeypatch.setattr(engine, "apply_rules", _unchanged_rewrite)
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args: CompileResult(
            "passed",
            artifacts={
                "abi": [None],
                "method_identifiers": {},
                "layout": {"storage_layout": []},
            },
        ),
    )
    config = _config(
        tmp_path,
        allow_unvalidated_source=True,
        allow_abi_change=True,
        allow_method_id_change=True,
        allow_storage_layout_change=True,
    )
    batch = engine.prepare_migrations((_request(path),), config)

    decision = engine.validate_migrations(batch, config)

    assert decision.status == "blocked"
    assert decision.blockers[0].code == "target_artifacts_unavailable"


def test_source_failure_uses_typed_waiver(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "Contract.vy"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("failed", stderr="source failed"),
    )
    monkeypatch.setattr(engine, "apply_rules", _unchanged_rewrite)
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args: CompileResult("passed", artifacts=VALIDATION_ARTIFACTS),
    )
    config = _config(tmp_path, allow_unvalidated_source=True)
    batch = engine.prepare_migrations((_request(path),), config)

    decision = engine.validate_migrations(batch, config)

    assert decision.status == "waived"
    assert [issue.code for issue in decision.waivers] == ["source_compile_failed"]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (CompileResult("passed", artifacts=VALIDATION_ARTIFACTS), "passed"),
        (CompileResult("failed", stderr="interface failed"), "blocked"),
    ],
)
def test_interface_validation_is_target_only(
    target: CompileResult, expected: str, monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "Interface.vyi"
    request = MigrationRequest(
        path,
        "@external\ndef ping(): ...\n",
        "0.3.10",
        (SourceCompileAttempt("0.3.10", "0.3.10"),),
    )
    monkeypatch.setattr(engine, "compile_source_file", lambda *_args: CompileResult("skipped"))
    monkeypatch.setattr(engine, "apply_rules", _unchanged_rewrite)
    monkeypatch.setattr(engine, "compile_target_source", lambda *_args: target)
    config = _config(tmp_path)
    batch = engine.prepare_migrations((request,), config)

    decision = engine.validate_migrations(batch, config)

    assert batch.files[0].report.source_compile == "skipped"
    assert decision.status == expected


def test_prepare_migrations_skips_interface_split_for_dependencies(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "dependency.vy"
    source = (
        "#pragma version 0.3.10\n"
        "interface Token:\n"
        "    def balanceOf(owner: address) -> uint256: view\n"
    )
    request = MigrationRequest(
        path,
        source,
        "0.3.10",
        (SourceCompileAttempt("0.3.10", "0.3.10"),),
        role="dependency",
    )
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )
    monkeypatch.setattr(engine, "apply_rules", _unchanged_rewrite)

    def unexpected_split(*_args):
        raise AssertionError("dependency interface splitting must not run")

    monkeypatch.setattr(engine, "split_interfaces_to_vyi", unexpected_split)

    batch = engine.prepare_migrations((request,), _config(tmp_path, split_interfaces=True))

    assert batch.files[0].report.role == "dependency"
    assert batch.generated == []


def test_validate_migrations_threads_closure_mode(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = project_root / "main.vy"
    search_path = tmp_path / "site-packages"
    dependency = search_path / "depkg" / "mod.vy"
    dependency.parent.mkdir(parents=True)
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='project'\n")
    project_source = "#pragma version 0.4.3\nfrom depkg import mod\n"
    dependency_source = "#pragma version 0.3.10\nVALUE: constant(uint256) = 1\n"
    project.write_text(project_source)
    dependency.write_text(dependency_source)
    rewritten_dependency = dependency_source.replace("0.3.10", "0.4.3")
    real_target_overlay = engine.target_overlay
    observed_flags: list[bool] = []
    observed_search_paths: dict[bool, tuple[Path, ...]] = {}

    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )

    def apply(source, config, path):
        rewritten = rewritten_dependency if path == dependency else source
        return RewriteResult(rewritten, [], [])

    @contextmanager
    def target_overlay(
        sources,
        target_version,
        search_paths,
        *,
        include_dependencies=False,
    ):
        observed_flags.append(include_dependencies)
        with real_target_overlay(
            sources,
            target_version,
            search_paths,
            include_dependencies=include_dependencies,
        ) as overlay:
            yield overlay

    def compile_target(path, source, config, overlay):
        assert overlay is not None
        observed_search_paths[config.include_dependencies] = compiler._overlay_search_paths(
            overlay, config.compiler_search_paths
        )
        if path == dependency:
            target = overlay.paths[dependency.resolve()]
            assert target == overlay.root / "depkg" / "mod.vy"
            assert target.read_text() == rewritten_dependency
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    monkeypatch.setattr(engine, "apply_rules", apply)
    monkeypatch.setattr(engine, "target_overlay", target_overlay)
    monkeypatch.setattr(engine, "compile_target_source", compile_target)
    project_request = MigrationRequest(
        project,
        project_source,
        "0.4.3",
        (SourceCompileAttempt("0.4.3", "0.4.3"),),
    )
    dependency_request = MigrationRequest(
        dependency,
        dependency_source,
        "0.3.10",
        (SourceCompileAttempt("0.3.10", "0.3.10"),),
        role="dependency",
    )
    closure_config = _config(
        tmp_path,
        compiler_search_paths=(search_path,),
        include_dependencies=True,
    )
    default_config = _config(
        tmp_path,
        compiler_search_paths=(search_path,),
    )

    closure_batch = engine.prepare_migrations((project_request, dependency_request), closure_config)
    default_batch = engine.prepare_migrations((project_request,), default_config)
    engine.validate_migrations(closure_batch, closure_config)
    engine.validate_migrations(default_batch, default_config)

    assert observed_flags == [True, False]
    assert search_path.resolve() not in observed_search_paths[True]
    assert search_path.resolve() in observed_search_paths[False]


def test_dependency_source_final_newline_retry_handles_read_only_directory(
    monkeypatch, tmp_path: Path
) -> None:
    dependency_directory = tmp_path / "site-packages" / "depkg"
    dependency_directory.mkdir(parents=True)
    dependency = dependency_directory / "mod.vy"
    source = "#pragma version 0.3.10\nVALUE: constant(uint256) = 1"
    dependency.write_text(source)
    original_failure = CompileResult(
        "failed",
        stderr="ValueError: start (2, 0) precedes previous end",
    )
    request = MigrationRequest(
        dependency,
        source,
        "0.3.10",
        (SourceCompileAttempt("0.3.10", "0.3.10"),),
        role="dependency",
    )
    original_entries = set(dependency_directory.iterdir())

    monkeypatch.setattr(
        compiler,
        "_run_compile_with_formats",
        lambda *_args, **_kwargs: original_failure,
    )
    monkeypatch.setattr(engine, "apply_rules", _unchanged_rewrite)

    def deny_temporary_file(*_args, **_kwargs):
        raise OSError("read-only dependency directory")

    monkeypatch.setattr(compiler.tempfile, "NamedTemporaryFile", deny_temporary_file)

    batch = engine.prepare_migrations((request,), _config(tmp_path))

    assert batch.files[0].source_compile is original_failure
    assert batch.files[0].report.source_compile == "failed"
    assert set(dependency_directory.iterdir()) == original_entries
