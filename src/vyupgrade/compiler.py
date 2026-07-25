from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field, replace
from functools import cache
from pathlib import Path
from typing import cast
from uuid import uuid4

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from uv import find_uv_bin

from .models import (
    CompilerAuthority,
    CompilerDeclaration,
    CompilerOutput,
    CompletionStatus,
    Config,
    ContentIdentity,
    DependencyContext,
    ExitStatus,
    FailureOrigin,
    ResolvedCompiler,
    ResolvedPackage,
)
from .source import code_mask
from .storage_layout import compare_storage_layouts, parse_storage_layout
from .versions import (
    VyperVersion,
    compiler_version_for_spec,
    infer_pragma,
    known_versions_satisfying,
    legacy_prerelease_version,
    parse_version,
)


FORMATS = ("abi", "method_identifiers", "layout")
SOURCE_FORMATS = ("abi", "method_identifiers", "layout", "ast")
TARGET_FORMATS = (*FORMATS, "ast")
COMPILE_TIMEOUT_SECONDS = 120
ARCHIVE_TARGET_FLOOR = "0.4.0"
OVERLAY_EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "venv",
}
VALIDATION_SOURCE_SUFFIXES = {".vy", ".vyi", ".json"}
VALIDATION_MODULE_ALIASES = {
    "create2": "create2_address",
}

_MARKER_ENVIRONMENT_SCRIPT = """
import json
import os
import platform
import sys

version = sys.implementation.version
implementation_version = ".".join(str(part) for part in version[:3])
if version.releaselevel != "final":
    suffix = {"alpha": "a", "beta": "b", "candidate": "rc"}[version.releaselevel]
    implementation_version += suffix + str(version.serial)
print(json.dumps({
    "implementation_name": sys.implementation.name,
    "implementation_version": implementation_version,
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "sys_platform": sys.platform,
}))
""".strip()


class OverlayLayoutConflictError(ValueError):
    """Two closure members with different content map to one overlay path."""


class ProjectEnvironmentError(ValueError):
    """The selected project interpreter could not satisfy the compile request."""


@dataclass
class CompileResult:
    status: str
    artifacts: dict[str, object] | None = None
    stderr: str | None = None
    command: list[str] | None = None
    unavailable_formats: tuple[str, ...] = ()
    resolved_compiler: str | None = None
    dependency_context: DependencyContext | None = None
    compiler_started: bool = False
    failure_origin: FailureOrigin | None = None
    compiler_output: CompilerOutput | None = None
    compiler_identity: ResolvedCompiler | None = None
    compiler_authority: CompilerAuthority = "default"
    compiler_declarations: tuple[CompilerDeclaration, ...] = ()
    completion_status: CompletionStatus = "not-started"
    exit_status: ExitStatus = dataclass_field(default_factory=lambda: ExitStatus(None))
    validated_sources: tuple[ContentIdentity, ...] = ()


@dataclass(frozen=True)
class _CompilerProcess:
    command: list[str]
    context: DependencyContext
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    resolved_compiler: str | None = None
    compiler_started: bool = False
    failure_origin: FailureOrigin | None = None
    error: str | None = None
    compiler_identity: ResolvedCompiler | None = None
    compiler_authority: CompilerAuthority = "default"
    compiler_declarations: tuple[CompilerDeclaration, ...] = ()
    completion_status: CompletionStatus = "not-started"
    exit_status: ExitStatus = dataclass_field(default_factory=lambda: ExitStatus(None))
    validated_sources: tuple[ContentIdentity, ...] = ()


@dataclass(frozen=True)
class TargetOverlay:
    root: Path
    paths: Mapping[Path, Path]
    source_roots: tuple[Path, ...]
    search_paths: tuple[Path, ...]


@dataclass(frozen=True)
class ImportClosure:
    roots: tuple[Path, ...]
    dependencies: tuple[Path, ...]
    consumers: Mapping[Path, tuple[Path, ...]]
    source_roots: tuple[Path, ...]
    common_root: Path

    @property
    def files(self) -> tuple[Path, ...]:
        return self.roots + self.dependencies


@dataclass(frozen=True)
class _ResolvedValidationImport:
    path: Path
    root: Path
    relative_path: Path


def unavailable_validation_artifacts(result: CompileResult) -> list[str]:
    artifacts = result.artifacts or {}
    validators: dict[str, Callable[[object], bool]] = {
        "abi": _valid_abi_artifact,
        "method_identifiers": _valid_method_identifiers_artifact,
        "layout": _valid_layout_artifact,
    }
    return [name for name, validator in validators.items() if not validator(artifacts.get(name))]


def _valid_abi_artifact(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return all(_valid_abi_entry(entry) for entry in value)


def _valid_abi_entry(value: object) -> bool:
    if not isinstance(value, dict) or not _nonempty_string(value.get("type")):
        return False
    if "name" in value and not isinstance(value["name"], str):
        return False
    for field in ("inputs", "outputs"):
        if field in value and not _valid_abi_parameters(value[field]):
            return False
    return True


def _valid_abi_parameters(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for parameter in value:
        if not isinstance(parameter, dict) or not _nonempty_string(parameter.get("type")):
            return False
        if "name" in parameter and not isinstance(parameter["name"], str):
            return False
        if "components" in parameter and not _valid_abi_parameters(parameter["components"]):
            return False
    return True


def _valid_method_identifiers_artifact(value: object) -> bool:
    return isinstance(value, dict) and all(
        _nonempty_string(signature) and _nonempty_string(selector)
        for signature, selector in value.items()
    )


def _valid_layout_artifact(value: object) -> bool:
    return parse_storage_layout(value) is not None


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def compile_source_file(path: Path, config: Config, source_version: str | None) -> CompileResult:
    if path.suffix != ".vy":
        return CompileResult("skipped")
    command, suppress_warnings = _prepare_command(
        config.source_vyper,
        source_version or infer_pragma(path.read_text()),
        config.source_python,
    )
    return _run_compile_with_formats(
        command,
        path,
        config,
        SOURCE_FORMATS,
        (),
        suppress_warnings,
        project_compiler=True,
        allow_format_retries=False,
    )


def compile_target_source(
    path: Path, source: str, config: Config, overlay: TargetOverlay | None = None
) -> CompileResult:
    if path.suffix not in {".vy", ".vyi"}:
        return CompileResult("skipped")
    if overlay is not None:
        tmp_path = overlay.paths.get(path.resolve())
        if tmp_path is not None:
            command, suppress_warnings = _prepare_command(
                config.target_vyper, config.target_version, config.target_python
            )
            compile_config = _target_compile_config(source, config)
            compile_config = replace(
                compile_config,
                compiler_search_paths=_overlay_search_paths(
                    overlay, compile_config.compiler_search_paths
                ),
            )
            if path.suffix == ".vyi":
                return _compile_target_interface(
                    command,
                    tmp_path,
                    source,
                    compile_config,
                    (),
                    suppress_warnings,
                    environment_path=path,
                )
            return _run_compile(
                command,
                tmp_path,
                compile_config,
                extra_paths=(),
                suppress_warnings=suppress_warnings,
                environment_path=path,
            )
    command, suppress_warnings = _prepare_command(
        config.target_vyper, config.target_version, config.target_python
    )
    compile_config = _target_compile_config(source, config)
    if path.suffix == ".vyi":
        return _compile_target_interface(
            command,
            path,
            source,
            compile_config,
            (path.parent,),
            suppress_warnings,
            environment_path=path,
        )
    try:
        tmp = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f".{path.stem}.vyupgrade.",
            suffix=".vy",
            dir=path.parent,
            delete=False,
        )
    except OSError:
        tmp = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f".{path.stem}.vyupgrade.",
            suffix=".vy",
            delete=False,
        )
    with tmp:
        tmp.write(source)
        tmp_path = Path(tmp.name)
    try:
        return _run_compile(
            command,
            tmp_path,
            compile_config,
            extra_paths=(path.parent,),
            suppress_warnings=suppress_warnings,
            environment_path=path,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def _compile_target_interface(
    command: list[str],
    path: Path,
    source: str,
    config: Config,
    extra_paths: tuple[Path, ...],
    suppress_warnings: bool,
    *,
    environment_path: Path,
) -> CompileResult:
    try:
        interface_file = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="vyupgrade_interface_",
            suffix=".vyi",
            dir=path.parent,
            delete=False,
        )
    except OSError as exc:
        return CompileResult(
            "failed", stderr=f"could not stage interface for target validation: {exc}"
        )
    interface_path = Path(interface_file.name)
    harness_path: Path | None = None
    try:
        with interface_file:
            interface_file.write(source)
        harness_file = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="vyupgrade_interface_harness_",
            suffix=".vy",
            dir=path.parent,
            delete=False,
        )
        harness_path = Path(harness_file.name)
        with harness_file:
            harness_file.write(
                f"#pragma version {config.target_version}\n"
                f"import {interface_path.stem} as InterfaceUnderTest\n"
            )
        return _run_compile(
            command,
            harness_path,
            config,
            extra_paths=tuple(dict.fromkeys((path.parent, *extra_paths))),
            suppress_warnings=suppress_warnings,
            environment_path=environment_path,
        )
    except OSError as exc:
        return CompileResult(
            "failed", stderr=f"could not create interface validation harness: {exc}"
        )
    finally:
        interface_path.unlink(missing_ok=True)
        if harness_path is not None:
            harness_path.unlink(missing_ok=True)


def compile_target_archive(
    path: Path,
    source: str,
    config: Config,
    overlay: TargetOverlay,
    output: Path,
) -> CompileResult:
    staged = overlay.paths.get(path.resolve())
    if staged is None:
        return CompileResult(
            "failed", stderr=f"archive entry was not staged in target overlay: {path}"
        )

    command, suppress_warnings = _prepare_command(
        config.target_vyper, config.target_version, config.target_python
    )
    compile_config = _target_compile_config(source, config)
    compile_config = replace(
        compile_config,
        compiler_search_paths=_overlay_search_paths(overlay, compile_config.compiler_search_paths),
    )
    tmp_out = output.with_name(f".{output.name}.vyupgrade-{os.getpid()}-{uuid4().hex}")
    full = [
        *command,
        "-f",
        "archive",
        "-o",
        str(tmp_out),
        *_compiler_search_path_args(command, staged, compile_config, ()),
    ]
    if compile_config.enable_decimals:
        full.append("--enable-decimals")
    if suppress_warnings:
        full.extend(["-W", "none"])
    full.append(str(staged.relative_to(overlay.root)))

    try:
        process = _run_compiler_process(
            full,
            path,
            project_compiler=False,
            python_pin=config.target_python,
            cwd=overlay.root,
        )
        if process.compiler_started:
            process = replace(
                process,
                validated_sources=(_content_identity(path, content_path=staged),),
            )
        if process.returncode is None:
            return _failed_compile_result(
                process,
                process.error or "compiler process did not return a result",
            )
        if process.returncode != 0:
            return _failed_compile_result(
                process,
                process.stderr.strip() or process.stdout.strip(),
            )
        if not tmp_out.is_file():
            return _failed_compile_result(
                replace(process, failure_origin="adapter"),
                "target compiler did not produce archive output",
            )
        os.replace(tmp_out, output)
        return CompileResult(
            "passed",
            artifacts={"archive": str(output)},
            stderr=process.stderr.strip() or None,
            command=process.command,
            resolved_compiler=process.resolved_compiler,
            dependency_context=process.context,
            compiler_started=process.compiler_started,
            compiler_identity=process.compiler_identity,
            compiler_authority=process.compiler_authority,
            compiler_declarations=process.compiler_declarations,
            completion_status=process.completion_status,
            exit_status=process.exit_status,
            validated_sources=process.validated_sources,
        )
    finally:
        tmp_out.unlink(missing_ok=True)


def resolve_import_closure(
    sources: Mapping[Path, str],
    search_paths: tuple[Path, ...] = (),
) -> ImportClosure:
    """Resolve the full transitive closure, including external search-path files.

    Default overlay materialization copies only closure members under
    ``common_root``; closure mode materializes external dependencies too.
    """
    resolved_sources = {path.resolve(): source for path, source in sources.items()}
    if not resolved_sources:
        raise ValueError("resolve_import_closure requires at least one source")
    source_roots, import_roots, common_root = _overlay_roots(resolved_sources, search_paths)
    dependencies = [
        path
        for path, _source, _import_root, is_override in _walk_validation_closure(
            source_roots, import_roots, common_root, resolved_sources
        )
        if not is_override
    ]
    dependency_paths = set(dependencies)
    consumers: dict[Path, list[Path]] = {path: [] for path in dependency_paths}
    for root in sorted(resolved_sources):
        for path, _source, _relative_path, is_override in _walk_validation_closure(
            source_roots,
            import_roots,
            common_root,
            resolved_sources,
            entry_paths=(root,),
        ):
            if not is_override and path in dependency_paths:
                consumers[path].append(root)
    return ImportClosure(
        roots=tuple(sorted(resolved_sources)),
        dependencies=tuple(sorted(dict.fromkeys(dependencies))),
        consumers={
            path: tuple(sorted(dict.fromkeys(paths))) for path, paths in sorted(consumers.items())
        },
        source_roots=source_roots,
        common_root=common_root,
    )


@contextmanager
def target_overlay(
    sources: Mapping[Path, str],
    target_version: str,
    search_paths: tuple[Path, ...] = (),
    *,
    include_dependencies: bool = False,
) -> Iterator[TargetOverlay | None]:
    resolved_sources = {path.resolve(): source for path, source in sources.items()}
    if not resolved_sources:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="vyupgrade-target-") as tmp:
        overlay = materialize_target_overlay(
            resolved_sources,
            target_version,
            Path(tmp),
            search_paths,
            include_dependencies=include_dependencies,
        )
        assert overlay is not None
        yield overlay


def materialize_target_overlay(
    sources: Mapping[Path, str],
    target_version: str,
    root: Path,
    search_paths: tuple[Path, ...] = (),
    *,
    include_dependencies: bool = False,
) -> TargetOverlay | None:
    resolved_sources = {path.resolve(): source for path, source in sources.items()}
    if not resolved_sources:
        return None
    roots, import_roots, common = _overlay_roots(resolved_sources, search_paths)
    if include_dependencies:
        paths, overlay_search_paths = _materialize_closure_sources(
            roots,
            import_roots,
            common,
            root,
            target_version,
            resolved_sources,
        )
    else:
        destination = _overlay_destination_resolver(common, root)
        paths: dict[Path, Path] = {}
        overlay_search_paths: set[Path] = set()
        for source_root in roots:
            copied_search_paths, _copied = _copy_validation_sources(
                source_root,
                import_roots,
                common,
                root,
                target_version,
                resolved_sources,
                destination,
                None,
            )
            overlay_search_paths.update(copied_search_paths)
        overlay_search_paths.update(_overlay_configured_search_paths(search_paths, common, root))
        for path, source in resolved_sources.items():
            target = destination(path)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
            paths[path] = target
            overlay_search_paths.add(target.parent)
    if include_dependencies:
        configured_search_paths = tuple(search_path.resolve() for search_path in search_paths)
        for source_root in roots:
            if source_root not in configured_search_paths:
                _copy_project_configs(
                    source_root,
                    root,
                    excluded_roots=configured_search_paths,
                )
    else:
        _copy_project_configs(common, root)
    return TargetOverlay(
        root=root,
        paths=paths,
        source_roots=import_roots if include_dependencies else roots,
        search_paths=tuple(
            sorted(
                (path for path in overlay_search_paths if path != root),
                key=lambda path: str(path),
            )
        ),
    )


def _materialize_closure_sources(
    source_roots: tuple[Path, ...],
    import_roots: tuple[Path, ...],
    common_root: Path,
    target_root: Path,
    target_version: str,
    overrides: Mapping[Path, str],
) -> tuple[dict[Path, Path], set[Path]]:
    placed: dict[Path, Path] = {}
    paths: dict[Path, Path] = {}
    search_paths: set[Path] = set()
    for path, source, relative_path, is_override in _walk_validation_closure(
        source_roots, import_roots, common_root, overrides
    ):
        target = target_root / relative_path
        if is_override:
            if _claim_overlay_destination(placed, target, path, source):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")
            search_paths.add(target.parent)
            if path.name == "create2_address.vy":
                alias = target.with_name("create2.vy")
                alias_source = _target_validation_create2_alias_source(source)
                if _claim_overlay_destination(placed, alias, path, alias_source):
                    alias.write_text(alias_source, encoding="utf-8")
        else:
            _copy_validation_source(
                path,
                source,
                target,
                common_root,
                target_version,
                search_paths,
                placed,
            )
        paths[path] = target
    return paths, search_paths


def _overlay_roots(
    resolved_sources: Mapping[Path, str],
    search_paths: tuple[Path, ...],
) -> tuple[tuple[Path, ...], tuple[Path, ...], Path]:
    roots = tuple(
        dict.fromkeys(
            root for path in resolved_sources for root in _validation_roots(path, search_paths)
        )
    )
    import_roots = tuple(
        dict.fromkeys((*roots, *(search_path.resolve() for search_path in search_paths)))
    )
    common = Path(os.path.commonpath([str(root) for root in roots]))
    return roots, import_roots, common


def _overlay_destination_resolver(
    common_root: Path,
    target_root: Path,
) -> Callable[[Path], Path | None]:
    def destination(path: Path) -> Path | None:
        try:
            return target_root / path.relative_to(common_root)
        except ValueError:
            return None

    return destination


def _claim_overlay_destination(
    placed: dict[Path, Path],
    target: Path,
    source_path: Path,
    content: str,
) -> bool:
    existing_source = placed.get(target)
    if existing_source is None:
        placed[target] = source_path
        return True
    if existing_source == source_path:
        return True
    if target.read_bytes() == content.encode("utf-8"):
        return False
    raise OverlayLayoutConflictError(
        f"overlay destination {target} maps both {existing_source} and {source_path}"
    )


def _overlay_configured_search_paths(
    search_paths: tuple[Path, ...], common_root: Path, target_root: Path
) -> set[Path]:
    paths: set[Path] = set()
    for search_path in search_paths:
        try:
            relative = search_path.resolve().relative_to(common_root)
        except ValueError:
            continue
        target = target_root / relative
        if target.exists():
            paths.add(target)
    return paths


def _overlay_search_paths(
    overlay: TargetOverlay, search_paths: tuple[Path, ...]
) -> tuple[Path, ...]:
    covered = tuple(path.resolve() for path in overlay.source_roots)
    return (
        overlay.root,
        *overlay.search_paths,
        *(
            search_path
            for search_path in search_paths
            if not any(_paths_overlap(search_path.resolve(), root) for root in covered)
        ),
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _validation_roots(path: Path, search_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    resolved = path.resolve()
    candidates: list[Path] = []
    for search_path in search_paths:
        root = search_path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        candidates.append(root)
    if candidates:
        return tuple(sorted(candidates, key=lambda candidate: len(candidate.parts)))
    return (_nearest_project_root(path.parent) or path.parent,)


def _walk_validation_closure(
    source_roots: tuple[Path, ...],
    import_roots: tuple[Path, ...],
    common_root: Path,
    overrides: Mapping[Path, str],
    *,
    entry_paths: Iterable[Path] | None = None,
) -> Iterator[tuple[Path, str, Path, bool]]:
    override_paths = set(overrides)
    if entry_paths is None:
        incoming_overrides: set[Path] = set()
        for path, source in overrides.items():
            import_source = _standard_json_package_dependency_source(path, source, common_root)
            for source_root in _containing_import_roots(path, source_roots):
                relative_path = path.relative_to(source_root)
                incoming_overrides.update(
                    imported.path
                    for imported in _validation_import_sources(
                        path,
                        import_source,
                        source_root,
                        import_roots,
                        relative_path,
                    )
                    if imported.path in override_paths
                )
        initial_paths = override_paths - incoming_overrides
        include_disconnected_overrides = True
    else:
        initial_paths = {path.resolve() for path in entry_paths}
        if not initial_paths <= override_paths:
            missing = ", ".join(str(path) for path in sorted(initial_paths - override_paths))
            raise ValueError(f"closure entry paths are not source overrides: {missing}")
        include_disconnected_overrides = False

    queue: list[tuple[Path, str, Path, Path, bool]] = []
    for path in sorted(initial_paths):
        source_root = _containing_import_roots(path, source_roots)[0]
        queue.append(
            (
                path,
                overrides[path],
                source_root,
                path.relative_to(source_root),
                True,
            )
        )
    layouts: dict[Path, Path] = {}
    processed: set[Path] = set()
    while queue or (include_disconnected_overrides and override_paths - processed):
        if not queue:
            path = min(override_paths - processed)
            source_root = _containing_import_roots(path, source_roots)[0]
            queue.append(
                (
                    path,
                    overrides[path],
                    source_root,
                    path.relative_to(source_root),
                    True,
                )
            )
        (
            current,
            current_source,
            current_root,
            current_relative,
            is_override,
        ) = queue.pop(0)
        existing_relative = layouts.get(current)
        if existing_relative is not None and existing_relative != current_relative:
            raise OverlayLayoutConflictError(
                f"closure member {current} maps to both {existing_relative} and {current_relative}"
            )
        layouts[current] = current_relative
        if current in processed:
            continue
        processed.add(current)
        yield current, current_source, current_relative, is_override
        import_source = _standard_json_package_dependency_source(
            current, current_source, common_root
        )
        for imported in _validation_import_sources(
            current,
            import_source,
            current_root,
            import_roots,
            current_relative,
        ):
            if imported.path in processed:
                existing_relative = layouts[imported.path]
                if existing_relative != imported.relative_path:
                    raise OverlayLayoutConflictError(
                        f"closure member {imported.path} maps to both "
                        f"{existing_relative} and {imported.relative_path}"
                    )
                continue
            source = overrides.get(imported.path)
            if source is None:
                try:
                    source = imported.path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
            queue.append(
                (
                    imported.path,
                    source,
                    imported.root,
                    imported.relative_path,
                    imported.path in override_paths,
                )
            )


def _containing_import_roots(path: Path, import_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots = tuple(root for root in import_roots if _is_relative_to(path, root))
    assert roots, f"closure member is outside import roots: {path}"
    return tuple(sorted(roots, key=lambda root: len(root.parts)))


def _walk_validation_sources(
    source_root: Path,
    import_roots: tuple[Path, ...],
    common_root: Path,
    overrides: Mapping[Path, str],
) -> Iterator[tuple[Path, str]]:
    override_paths = set(overrides)
    queue: list[tuple[Path, str]] = []
    for path, source in overrides.items():
        try:
            path.relative_to(source_root)
        except ValueError:
            continue
        queue.append((path, source))

    processed: set[Path] = set()
    while queue:
        current, current_source = queue.pop()
        if current in processed:
            continue
        processed.add(current)
        current_source = _standard_json_package_dependency_source(
            current, current_source, common_root
        )
        for imported in _validation_import_sources(
            current, current_source, source_root, import_roots
        ):
            resolved = imported.path
            if resolved in processed:
                continue
            source = overrides.get(resolved)
            if source is None:
                try:
                    source = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
            queue.append((resolved, source))
            if resolved in override_paths:
                continue
            yield resolved, source


def _copy_validation_sources(
    source_root: Path,
    import_roots: tuple[Path, ...],
    common_root: Path,
    target_root: Path,
    target_version: str,
    overrides: Mapping[Path, str],
    destination: Callable[[Path], Path | None],
    placed: dict[Path, Path] | None,
) -> tuple[set[Path], dict[Path, Path]]:
    search_paths: set[Path] = set()
    copied: dict[Path, Path] = {}
    for resolved, source in _walk_validation_sources(
        source_root, import_roots, common_root, overrides
    ):
        target = destination(resolved)
        if target is None:
            continue
        _copy_validation_source(
            resolved,
            source,
            target,
            common_root,
            target_version,
            search_paths,
            placed,
        )
        if resolved.suffix in VALIDATION_SOURCE_SUFFIXES:
            copied[resolved] = target
    return search_paths, copied


def _copy_validation_source(
    source_path: Path,
    source: str,
    target: Path,
    common_root: Path,
    target_version: str,
    search_paths: set[Path],
    placed: dict[Path, Path] | None,
) -> None:
    if source_path.suffix not in VALIDATION_SOURCE_SUFFIXES:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source_path.suffix == ".json":
        if placed is None or _claim_overlay_destination(placed, target, source_path, source):
            target.write_text(source, encoding="utf-8")
        search_paths.add(target.parent)
        return
    source = _standard_json_package_dependency_source(source_path, source, common_root)
    content = _target_validation_source(
        source,
        target_version,
        is_interface=source_path.suffix == ".vyi",
    )
    if placed is None or _claim_overlay_destination(placed, target, source_path, content):
        target.write_text(content, encoding="utf-8")
    search_paths.add(target.parent)
    if source_path.name == "create2_address.vy":
        alias = target.with_name("create2.vy")
        alias_content = _target_validation_source(
            _target_validation_create2_alias_source(source),
            target_version,
            is_interface=False,
        )
        if placed is None or _claim_overlay_destination(placed, alias, source_path, alias_content):
            alias.write_text(alias_content, encoding="utf-8")


def _standard_json_package_dependency_source(
    source_path: Path, source: str, common_root: Path
) -> str:
    source = _local_sibling_import_source(source_path, source)
    if source_path.parent.name != "src":
        return source
    replacements = {
        name
        for name in ("auth", "utils")
        if (common_root / name).exists() and not (source_path.parent / name).exists()
    }
    if not replacements:
        return source
    names = "|".join(sorted(re.escape(name) for name in replacements))
    return re.sub(
        rf"(^[ \t]*from[ \t]+)\.({names})(?=\b)",
        r"\1..\2",
        source,
        flags=re.MULTILINE,
    )


def _local_sibling_import_source(source_path: Path, source: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        module = match.group("module")
        if not (source_path.parent / f"{module}.vy").exists():
            return match.group(0)
        alias = match.group("alias") or ""
        return f"{match.group('indent')}from . import {module}{alias}{match.group('trailing')}"

    return re.sub(
        r"^(?P<indent>[ \t]*)import[ \t]+(?P<module>[A-Za-z_][A-Za-z0-9_]*)(?P<alias>[ \t]+as[ \t]+[A-Za-z_][A-Za-z0-9_]*)?(?P<trailing>[ \t]*(?:#.*)?)$",
        replacement,
        source,
        flags=re.MULTILINE,
    )


def _validation_import_sources(
    path: Path,
    source: str,
    source_root: Path,
    import_roots: tuple[Path, ...],
    relative_path: Path | None = None,
) -> tuple[_ResolvedValidationImport, ...]:
    imports: list[_ResolvedValidationImport] = []
    mask = code_mask(source)
    code_source = "".join(
        char if is_code or char in "\r\n" else " "
        for char, is_code in zip(source, mask, strict=True)
    )
    for line in code_source.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        import_match = re.match(
            r"import\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b",
            stripped,
        )
        if import_match:
            imports.extend(
                _resolve_validation_import(
                    path,
                    source_root,
                    import_match.group(1),
                    (),
                    import_roots,
                    relative_path,
                )
            )
            continue
        from_match = re.match(
            r"from\s+([.A-Za-z_][.A-Za-z0-9_]*)\s+import\s+(.+)$",
            stripped,
        )
        if from_match:
            names = tuple(_imported_module_names(from_match.group(2)))
            imports.extend(
                _resolve_validation_import(
                    path,
                    source_root,
                    from_match.group(1),
                    names,
                    import_roots,
                    relative_path,
                )
            )
    return tuple(dict.fromkeys(imports))


def _imported_module_names(imports: str) -> Iterator[str]:
    for part in imports.strip().removeprefix("(").removesuffix(")").split(","):
        name = part.split("#", 1)[0].strip()
        if not name or name == "*":
            continue
        name = name.split()[0]
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*$", name):
            yield name


def _resolve_validation_import(
    path: Path,
    source_root: Path,
    module: str,
    names: tuple[str, ...],
    import_roots: tuple[Path, ...],
    relative_path: Path | None,
) -> tuple[_ResolvedValidationImport, ...]:
    bases, module_parts = _validation_import_bases(
        path, source_root, module, import_roots, relative_path
    )
    candidates: list[tuple[Path, Path, Path]] = []
    for base, import_root, relative_base in bases:
        module_path = base.joinpath(*module_parts) if module_parts else base
        relative_module = relative_base.joinpath(*module_parts) if module_parts else relative_base
        if names:
            for name in names:
                candidates.extend(
                    _validation_module_candidate_layouts(
                        module_path / name,
                        relative_module / name,
                        import_root,
                    )
                )
            candidates.extend(
                _validation_module_candidate_layouts(module_path, relative_module, import_root)
            )
        else:
            candidates.extend(
                _validation_module_candidate_layouts(module_path, relative_module, import_root)
            )
    resolved: dict[Path, tuple[Path, Path]] = {}
    roots = tuple(root.resolve() for root in (source_root, *import_roots))
    for candidate, import_root, candidate_relative in candidates:
        try:
            resolved_candidate = candidate.resolve()
            if not any(_is_relative_to(resolved_candidate, root) for root in roots):
                continue
        except (OSError, ValueError):
            continue
        if resolved_candidate.exists() and resolved_candidate.suffix in VALIDATION_SOURCE_SUFFIXES:
            resolved.setdefault(resolved_candidate, (import_root, candidate_relative))
    return tuple(
        _ResolvedValidationImport(path, import_root, candidate_relative)
        for path, (import_root, candidate_relative) in resolved.items()
    )


def _validation_import_bases(
    path: Path,
    source_root: Path,
    module: str,
    import_roots: tuple[Path, ...],
    relative_path: Path | None,
) -> tuple[tuple[tuple[Path, Path, Path], ...], tuple[str, ...]]:
    relative_parent = relative_path.parent if relative_path is not None else Path()
    if not module.startswith("."):
        bases = (
            (path.parent, source_root, relative_parent),
            (source_root, source_root, Path()),
            *((root, root, Path()) for root in import_roots),
        )
        unique_bases: dict[Path, tuple[Path, Path]] = {}
        for base, root, relative_base in bases:
            unique_bases.setdefault(base, (root, relative_base))
        return tuple(
            (base, root, relative_base) for base, (root, relative_base) in unique_bases.items()
        ), tuple(part for part in module.split(".") if part)
    level = len(module) - len(module.lstrip("."))
    base = path.parent
    relative_base = relative_parent
    for _ in range(max(level - 1, 0)):
        base = base.parent
        relative_base = relative_base.parent
    return ((base, source_root, relative_base),), tuple(
        part for part in module[level:].split(".") if part
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validation_module_candidates(path: Path) -> tuple[Path, ...]:
    paths = [path]
    alias = VALIDATION_MODULE_ALIASES.get(path.name)
    if alias is not None:
        paths.append(path.with_name(alias))
    return tuple(
        candidate.with_suffix(suffix)
        for candidate in paths
        for suffix in sorted(VALIDATION_SOURCE_SUFFIXES)
    )


def _validation_module_candidate_layouts(
    path: Path, relative_path: Path, import_root: Path
) -> tuple[tuple[Path, Path, Path], ...]:
    return tuple(
        (
            candidate,
            import_root,
            relative_path.with_name(candidate.name) if relative_path.name else Path(candidate.name),
        )
        for candidate in _validation_module_candidates(path)
    )


def _project_config_paths(
    source_root: Path,
    excluded_roots: tuple[Path, ...],
) -> Iterator[Path]:
    names = ("pyproject.toml", "uv.lock", "uv.toml")
    if not excluded_roots:
        for name in names:
            yield from source_root.rglob(name)
        return
    for directory, directories, files in os.walk(source_root):
        current = Path(directory)
        directories[:] = [
            name
            for name in directories
            if not any(
                _is_relative_to((current / name).resolve(), excluded_root)
                for excluded_root in excluded_roots
            )
        ]
        for name in names:
            if name in files:
                yield current / name


def _copy_project_configs(
    source_root: Path,
    target_root: Path,
    *,
    excluded_roots: tuple[Path, ...] = (),
) -> None:
    resolved_target = target_root.resolve()
    for config_path in _project_config_paths(source_root, excluded_roots):
        resolved_config = config_path.resolve()
        if (
            resolved_config == resolved_target
            or resolved_target in resolved_config.parents
            or any(
                _is_relative_to(resolved_config, excluded_root) for excluded_root in excluded_roots
            )
        ):
            continue
        if any(part in {".git", ".venv", "venv", "node_modules"} for part in config_path.parts):
            continue
        try:
            relative = config_path.relative_to(source_root)
        except ValueError:
            continue
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config_path, target)


def compile_source_ast(path: Path, config: Config, source_version: str | None) -> CompileResult:
    """Load rewrite facts without requiring a module to be a deployable entry point."""
    if path.suffix != ".vy":
        return CompileResult("skipped")
    command, suppress_warnings = _prepare_command(
        config.source_vyper,
        source_version or infer_pragma(path.read_text()),
        config.source_python,
    )
    result: CompileResult | None = None
    for output_format in ("annotated_ast", "ast"):
        result = _run_compile_with_formats(
            command,
            path,
            config,
            (output_format,),
            (),
            suppress_warnings,
            allow_unsupported_formats=True,
            project_compiler=True,
        )
        artifact = (result.artifacts or {}).get(output_format)
        if result.status == "passed" and isinstance(artifact, dict):
            return replace(result, artifacts={"ast": artifact})
    assert result is not None
    return result


def compare_artifacts(
    source: CompileResult, target: CompileResult
) -> tuple[bool | None, bool | None, bool | None]:
    if source.artifacts is None or target.artifacts is None:
        return None, None, None
    source_layout = parse_storage_layout(source.artifacts.get("layout"))
    target_layout = parse_storage_layout(target.artifacts.get("layout"))
    source_abi = source.artifacts.get("abi")
    target_abi = target.artifacts.get("abi")
    source_methods = source.artifacts.get("method_identifiers")
    target_methods = target.artifacts.get("method_identifiers")
    storage_comparison = (
        None
        if source_layout is None or target_layout is None
        else compare_storage_layouts(
            source_layout,
            target_layout,
            target_ast=target.artifacts.get("ast"),
        )
    )
    return (
        None
        if source_abi is None or target_abi is None
        else _canonical_abi(source_abi) == _canonical_abi(target_abi),
        None
        if source_methods is None or target_methods is None
        else _canonical_method_identifiers(source_methods)
        == _canonical_method_identifiers(target_methods),
        None if storage_comparison is None else storage_comparison.equal,
    )


def compare_artifact_details(
    source: CompileResult,
    target: CompileResult,
) -> tuple[list[str], list[str], list[str]]:
    if source.artifacts is None or target.artifacts is None:
        return [], [], []
    source_abi = source.artifacts.get("abi")
    target_abi = target.artifacts.get("abi")
    source_methods = source.artifacts.get("method_identifiers")
    target_methods = target.artifacts.get("method_identifiers")
    source_layout = parse_storage_layout(source.artifacts.get("layout"))
    target_layout = parse_storage_layout(target.artifacts.get("layout"))
    storage_comparison = (
        None
        if source_layout is None or target_layout is None
        else compare_storage_layouts(
            source_layout,
            target_layout,
            target_ast=target.artifacts.get("ast"),
        )
    )
    return (
        _abi_diff(source_abi, target_abi),
        _method_identifier_diff(source_methods, target_methods),
        [] if storage_comparison is None else list(storage_comparison.differences),
    )


def _target_validation_source(
    source: str, target_version: str, *, is_interface: bool = False
) -> str:
    pattern = re.compile(r"^(\s*)#\s*(?:@version|pragma\s+version)\s+(.+?)\s*$", re.MULTILINE)
    replaced = False

    def replacement(match: re.Match[str]) -> str:
        nonlocal replaced
        if replaced:
            return ""
        replaced = True
        return f"{match.group(1)}#pragma version {target_version}"

    rewritten = pattern.sub(replacement, source)
    rewritten = re.sub(
        r"^[ \t]*#[ \t]*pragma[ \t]+solidity\b.*(?:\n|$)",
        "",
        rewritten,
        flags=re.MULTILINE,
    )
    rewritten = _target_validation_dependency_source(rewritten)
    rewritten = _strip_target_validation_docstrings(rewritten)
    if is_interface:
        return _target_validation_interface_source(rewritten)
    return rewritten


def _strip_target_validation_docstrings(source: str) -> str:
    lines = source.splitlines(keepends=True)
    result: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        quote = _standalone_docstring_quote(stripped)
        if quote is None:
            result.append(line)
            index += 1
            continue
        if not _target_validation_docstring_context(lines, index):
            result.append(line)
            index += 1
            continue
        index += 1
        if stripped.count(quote) >= 2:
            continue
        while index < len(lines):
            current = lines[index]
            index += 1
            if quote in current:
                break
    return "".join(result)


def _standalone_docstring_quote(stripped_line: str) -> str | None:
    for quote in ('"""', "'''"):
        if stripped_line.startswith(quote):
            return quote
    return None


def _target_validation_docstring_context(lines: list[str], index: int) -> bool:
    indent = len(lines[index]) - len(lines[index].lstrip(" \t"))
    previous = _previous_significant_line(lines, index)
    if previous is None:
        return indent == 0
    previous_index, previous_line = previous
    previous_stripped = previous_line.strip()
    previous_indent = len(previous_line) - len(previous_line.lstrip(" \t"))
    if indent == 0:
        return previous_stripped.startswith("#pragma version")
    if previous_indent >= indent or not previous_stripped.endswith(":"):
        return False
    if previous_stripped.startswith("def "):
        return True
    return _previous_function_header(lines, previous_index, previous_indent) is not None


def _previous_significant_line(lines: list[str], index: int) -> tuple[int, str] | None:
    cursor = index - 1
    while cursor >= 0:
        stripped = lines[cursor].strip()
        if stripped and not stripped.startswith("#"):
            return cursor, lines[cursor]
        cursor -= 1
    return None


def _previous_function_header(lines: list[str], index: int, indent: int) -> int | None:
    cursor = index
    while cursor >= 0:
        line = lines[cursor]
        stripped = line.strip()
        current_indent = len(line) - len(line.lstrip(" \t"))
        if current_indent == indent and stripped.startswith("def "):
            return cursor
        if current_indent < indent and stripped.startswith("def "):
            return cursor
        if current_indent < indent:
            return None
        cursor -= 1
    return None


def _target_validation_dependency_source(source: str) -> str:
    source = re.sub(
        r"(^[ \t]*from[ \t]+snekmate\.utils[ \t]+import[ \t]+.*?)\bcreate2_address\b",
        r"\1create2",
        source,
        flags=re.MULTILINE,
    )
    source = re.sub(
        r"(?<![\w.])create2(?:_address)?\._compute_address\b",
        "create2._compute_create2_address",
        source,
    )
    return source


def _target_validation_create2_alias_source(source: str) -> str:
    if "_compute_create2_address" in source or "_compute_address" not in source:
        return source
    return re.sub(r"\b_compute_address\b", "_compute_create2_address", source)


def _target_validation_interface_source(source: str) -> str:
    lines = source.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not re.match(r"def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", stripped):
            output.append(line)
            index += 1
            continue

        def_indent = len(line) - len(line.lstrip(" \t"))
        header_lines = [line]
        index += 1
        while index < len(lines) and not _interface_header_complete("".join(header_lines)):
            header_lines.append(lines[index])
            index += 1
        output.extend(_interface_header_stub_lines(header_lines))
        while index < len(lines):
            next_line = lines[index]
            if not next_line.strip():
                index += 1
                continue
            next_indent = len(next_line) - len(next_line.lstrip(" \t"))
            if next_indent <= def_indent:
                break
            index += 1
    return "".join(output)


def _interface_header_complete(header: str) -> bool:
    depth = 0
    for char in header:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
    return depth == 0 and bool(re.search(r":[ \t]*(?:\w+\s*)?(?:#.*)?$", header.rstrip()))


def _interface_header_stub_lines(header_lines: list[str]) -> list[str]:
    if not header_lines:
        return []
    output = list(header_lines)
    last = output[-1].rstrip("\n")
    output[-1] = re.sub(
        r":[ \t]*(?:view|pure|payable|nonpayable)?([ \t]*(?:#.*)?)$",
        r": ...\1",
        last,
    ) + ("\n" if output[-1].endswith("\n") else "")
    return output


def _canonical_abi(abi: object) -> object:
    if not isinstance(abi, list):
        return abi
    entries = [_strip_abi_metadata(entry) for entry in abi if isinstance(entry, dict)]
    return sorted(entries, key=lambda entry: json.dumps(_abi_sort_key(entry), sort_keys=True))


def _strip_abi_metadata(value: object) -> object:
    if isinstance(value, dict):
        stripped = {
            key: _canonical_abi_value(key, item, value)
            for key, item in sorted(value.items())
            if key not in {"gas", "internalType", "unit"}
            and not (key == "name" and not _is_abi_entry(value))
            and not (
                value.get("type") == "constructor"
                and key == "stateMutability"
                and item == "nonpayable"
            )
            and key != "constant"
            and key != "payable"
            and not (value.get("type") == "constructor" and key == "outputs")
            and not (key == "components" and _is_tuple_abi_type(value.get("type")))
        }
        if _is_abi_entry(value) and "stateMutability" not in stripped:
            mutability = _legacy_abi_mutability(value)
            if mutability is not None:
                stripped["stateMutability"] = mutability
        return stripped
    if isinstance(value, list):
        return [_strip_abi_metadata(item) for item in value]
    return value


def _canonical_abi_value(key: str, value: object, parent: Mapping[str, object]) -> object:
    if key == "stateMutability" and value == "pure":
        return "view"
    if key == "outputs":
        return _canonical_abi_outputs(value)
    if key == "type" and isinstance(value, str):
        return _canonical_abi_type(value, parent.get("components"))
    return _strip_abi_metadata(value)


def _is_abi_entry(value: Mapping[str, object]) -> bool:
    return value.get("type") in {"function", "event", "error"}


def _legacy_abi_mutability(value: Mapping[str, object]) -> str | None:
    if value.get("payable") is True:
        return "payable"
    if value.get("constant") is True:
        return "view"
    if value.get("type") in {"function", "constructor"} and (
        value.get("payable") is False or value.get("constant") is False
    ):
        return "nonpayable"
    return None


def _canonical_abi_outputs(value: object) -> object:
    if (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], dict)
        and _is_tuple_abi_type(value[0].get("type"))
        and isinstance(value[0].get("components"), list)
    ):
        return _strip_abi_metadata(value[0]["components"])
    return _strip_abi_metadata(value)


def _canonical_abi_type(type_name: str, components: object = None) -> str:
    fixed_match = re.fullmatch(r"u?fixed(?P<bits>\d+)x(?P<scale>\d+)", type_name)
    if fixed_match is not None:
        prefix = "uint" if type_name.startswith("ufixed") else "int"
        return f"{prefix}{fixed_match.group('bits')}"
    if _is_tuple_abi_type(type_name):
        suffix = type_name.removeprefix("tuple")
        if isinstance(components, list):
            inner = ",".join(
                _canonical_abi_type(str(component.get("type", "?")), component.get("components"))
                if isinstance(component, dict)
                else "?"
                for component in components
            )
            return f"({inner}){suffix}"
    return type_name


def _is_tuple_abi_type(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"tuple(?:\[[0-9]*\])*", value) is not None


def _abi_sort_key(entry: dict[str, object]) -> tuple[object, ...]:
    inputs = entry.get("inputs")
    input_types: tuple[object, ...] = ()
    if isinstance(inputs, list):
        input_types = tuple(item.get("type") if isinstance(item, dict) else None for item in inputs)
    return (entry.get("type"), entry.get("name", ""), input_types, entry.get("stateMutability", ""))


def _canonical_method_identifiers(methods: object) -> object:
    if not isinstance(methods, dict):
        return methods
    return {key: value for key, value in sorted(methods.items()) if not key.startswith("__init__(")}


def _abi_diff(source: object, target: object) -> list[str]:
    if source is None or target is None:
        return []
    source_entries = _abi_entry_map(_canonical_abi(source))
    target_entries = _abi_entry_map(_canonical_abi(target))
    return _mapping_diff_lines(
        source_entries,
        target_entries,
        removed=lambda key, value: f"removed ABI entry: {key}",
        added=lambda key, value: f"added ABI entry: {key}",
        changed=_changed_abi_lines,
    )


def _abi_entry_diff(source: object, target: object) -> list[str]:
    if not isinstance(source, dict) or not isinstance(target, dict):
        return []
    details: list[str] = []
    for field, label in (
        ("inputs", "inputs"),
        ("outputs", "outputs"),
    ):
        if source.get(field) != target.get(field):
            details.append(
                f"{label} {_format_abi_params(source.get(field))} -> {_format_abi_params(target.get(field))}"
            )
    for field in ("stateMutability", "anonymous"):
        if source.get(field) != target.get(field):
            details.append(f"{field} {source.get(field)!r} -> {target.get(field)!r}")
    if details:
        return details
    return [f"{json.dumps(source, sort_keys=True)} -> {json.dumps(target, sort_keys=True)}"]


def _format_abi_params(value: object) -> str:
    if not isinstance(value, list):
        return str(value)
    if not value:
        return "()"
    return "(" + ", ".join(_format_abi_param(item) for item in value) + ")"


def _format_abi_param(value: object) -> str:
    if not isinstance(value, dict):
        return str(value)
    type_name = str(value.get("type", "?"))
    if type_name == "tuple":
        type_name = f"tuple{_format_abi_params(value.get('components'))}"
    name = value.get("name")
    return f"{name}: {type_name}" if isinstance(name, str) and name else type_name


def _method_identifier_diff(source: object, target: object) -> list[str]:
    if source is None or target is None:
        return []
    source_methods = _canonical_method_identifiers(source)
    target_methods = _canonical_method_identifiers(target)
    if not isinstance(source_methods, dict) or not isinstance(target_methods, dict):
        return []
    return _mapping_diff_lines(
        source_methods,
        target_methods,
        removed=lambda key, value: f"removed selector: {key} = {value}",
        added=lambda key, value: f"added selector: {key} = {value}",
        changed=lambda key, before, after: [f"changed selector: {key} {before} -> {after}"],
    )


def _mapping_diff_lines(
    source: Mapping[str, object],
    target: Mapping[str, object],
    *,
    removed: Callable[[str, object], str],
    added: Callable[[str, object], str],
    changed: Callable[[str, object, object], list[str]],
    skip_removed: set[str] | None = None,
    skip_added: set[str] | None = None,
    equivalent: Callable[[str, object, object], bool] | None = None,
) -> list[str]:
    skip_removed = skip_removed or set()
    skip_added = skip_added or set()
    lines = [
        removed(key, source[key]) for key in sorted((source.keys() - target.keys()) - skip_removed)
    ]
    lines.extend(
        added(key, target[key]) for key in sorted((target.keys() - source.keys()) - skip_added)
    )
    for key in sorted(source.keys() & target.keys()):
        if not (
            equivalent(key, source[key], target[key]) if equivalent else source[key] == target[key]
        ):
            lines.extend(changed(key, source[key], target[key]))
    return lines


def _changed_abi_lines(key: str, source: object, target: object) -> list[str]:
    details = _abi_entry_diff(source, target)
    if not details:
        return [f"changed ABI entry: {key}"]
    return [f"changed ABI entry: {key}: {detail}" for detail in details]


def _abi_entry_map(abi: object) -> dict[str, object]:
    if not isinstance(abi, list):
        return {}
    return {_abi_entry_key(entry): entry for entry in abi if isinstance(entry, dict)}


def _abi_entry_key(entry: dict[str, object]) -> str:
    entry_type = str(entry.get("type", "unknown"))
    if entry_type in {"function", "event", "error"}:
        return f"{entry_type} {entry.get('name', '')}({_abi_input_types(entry)})"
    return entry_type


def _abi_input_types(entry: dict[str, object]) -> str:
    inputs = entry.get("inputs")
    if not isinstance(inputs, list):
        return ""
    return ", ".join(
        str(item.get("type", "?")) if isinstance(item, dict) else "?" for item in inputs
    )


def _prepare_command(
    explicit: str | None, version: str | None, python: str | None
) -> tuple[list[str], bool]:
    normalized = _normalize_version(version)
    return _compiler_command(explicit, normalized, python), _supports_warning_policy(normalized)


def _compiler_command(explicit: str | None, version: str | None, python: str | None) -> list[str]:
    if explicit:
        return [explicit]
    normalized = _normalize_version(version) or "0.4.3"
    python = python or _default_python(normalized)
    command = [
        _uv_bin(),
        "run",
        "--no-project",
        "--python",
        python,
        "--with",
        f"vyper=={normalized}",
    ]
    if legacy_prerelease_version(normalized) is not None:
        return [
            *command,
            "--with",
            "typed-ast",
            "python",
            str(Path(__file__).with_name("legacy_vyper.py")),
        ]
    return [*command, "vyper"]


@cache
def _uv_bin() -> str:
    try:
        return find_uv_bin()
    except TypeError as exc:
        if "NoneType" not in str(exc):
            raise
        uv = shutil.which("uv")
        if uv is None:
            raise exc
        return uv


def _normalize_version(version: str | None) -> str | None:
    return compiler_version_for_spec(version)


def _default_python(version: str) -> str:
    if legacy_prerelease_version(version) is not None:
        return "3.8"
    parsed = parse_version(version)
    if parsed is not None and parsed < VyperVersion("0.3.1"):
        return "3.8"
    # Modern Vyper releases run cleanly on Python 3.11. Pinning the subprocess
    # interpreter avoids accidentally using a bleeding-edge project venv, where
    # old compiler dependencies can break.
    return "3.11"


def _supports_warning_policy(version: str | None) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed >= VyperVersion("0.4.1")


def _target_compile_config(source: str, config: Config) -> Config:
    if config.enable_decimals or not _uses_decimal(source):
        return config
    return replace(config, enable_decimals=True)


def _uses_decimal(source: str) -> bool:
    if re.search(r"\bdecimal\b", source):
        return True
    return bool(
        re.search(r"^\s*(?:from\s+math\s+import\b|import\s+math(?:\s|,|$))", source, re.MULTILINE)
    )


def _run_compile(
    command: list[str],
    path: Path,
    config: Config,
    extra_paths: tuple[Path, ...] = (),
    suppress_warnings: bool = False,
    environment_path: Path | None = None,
) -> CompileResult:
    return _run_compile_with_formats(
        command,
        path,
        config,
        TARGET_FORMATS,
        extra_paths,
        suppress_warnings,
        optional_formats=frozenset({"ast"}),
        environment_path=environment_path,
    )


def _run_compile_with_formats(
    command: list[str],
    path: Path,
    config: Config,
    formats: tuple[str, ...],
    extra_paths: tuple[Path, ...],
    suppress_warnings: bool,
    *,
    allow_unsupported_formats: bool = False,
    allow_format_retries: bool = True,
    unavailable_formats: tuple[str, ...] = (),
    optional_formats: frozenset[str] = frozenset(),
    project_compiler: bool = False,
    environment_path: Path | None = None,
) -> CompileResult:
    environment_path = environment_path or path
    full = [
        *command,
        "-f",
        ",".join(formats),
        *_compiler_search_path_args(command, path, config, extra_paths),
    ]
    if config.enable_decimals:
        full.append("--enable-decimals")
    if suppress_warnings and not (
        project_compiler and _project_declares_vyper(_nearest_pyproject(environment_path.parent))
    ):
        full.extend(["-W", "none"])
    full.append(str(path))
    process = _run_compiler_process(
        full,
        environment_path,
        project_compiler=project_compiler,
        python_pin=config.source_python if project_compiler else config.target_python,
    )
    if process.compiler_started and path.is_file():
        process = replace(
            process,
            validated_sources=(_content_identity(environment_path, content_path=path),),
        )
    if process.returncode is None:
        return _failed_compile_result(
            process,
            process.error or "compiler process did not return a result",
            unavailable_formats,
        )
    if process.returncode != 0:
        stderr = process.stderr.strip() or process.stdout.strip()
        if not allow_format_retries:
            return _failed_compile_result(process, stderr, unavailable_formats)
        unsupported = _unsupported_output_format(stderr, formats)
        if unsupported is not None:
            unavailable = tuple(dict.fromkeys((*unavailable_formats, unsupported)))
            if not allow_unsupported_formats and unsupported not in optional_formats:
                return _failed_compile_result(
                    process,
                    (
                        "target compiler did not support required output format "
                        f"'{unsupported}': {stderr}"
                    ),
                    unavailable,
                )
            fallback_formats = tuple(name for name in formats if name != unsupported)
            if not fallback_formats:
                return _failed_compile_result(
                    process,
                    "source compiler did not support any requested output formats",
                    unavailable,
                )
            return _run_compile_with_formats(
                command,
                path,
                config,
                fallback_formats,
                extra_paths,
                suppress_warnings,
                allow_unsupported_formats=allow_unsupported_formats,
                allow_format_retries=allow_format_retries,
                unavailable_formats=unavailable,
                optional_formats=optional_formats,
                project_compiler=project_compiler,
                environment_path=environment_path,
            )
        span_error_format = _legacy_span_error_format(stderr, formats)
        if (
            span_error_format is not None
            and not allow_unsupported_formats
            and span_error_format not in optional_formats
        ):
            span_error_format = None
        if span_error_format is not None:
            unavailable = tuple(dict.fromkeys((*unavailable_formats, span_error_format)))
            fallback_formats = tuple(name for name in formats if name != span_error_format)
            if not fallback_formats:
                return _failed_compile_result(
                    process,
                    "source compiler could not produce any requested output formats",
                    unavailable,
                )
            return _run_compile_with_formats(
                command,
                path,
                config,
                fallback_formats,
                extra_paths,
                suppress_warnings,
                allow_unsupported_formats=allow_unsupported_formats,
                allow_format_retries=allow_format_retries,
                unavailable_formats=unavailable,
                optional_formats=optional_formats,
                project_compiler=project_compiler,
                environment_path=environment_path,
            )
        return _failed_compile_result(process, stderr, unavailable_formats)
    try:
        artifacts = _parse_outputs(process.stdout, formats)
    except ValueError as exc:
        return _failed_compile_result(
            replace(process, failure_origin="adapter"),
            f"could not parse compiler output: {exc}",
            unavailable_formats,
        )
    return CompileResult(
        "degraded"
        if any(name not in optional_formats for name in unavailable_formats)
        else "passed",
        artifacts=artifacts,
        stderr=process.stderr.strip() or None,
        command=process.command,
        unavailable_formats=unavailable_formats,
        resolved_compiler=process.resolved_compiler,
        dependency_context=process.context,
        compiler_started=process.compiler_started,
        compiler_identity=process.compiler_identity,
        compiler_authority=process.compiler_authority,
        compiler_declarations=process.compiler_declarations,
        completion_status=process.completion_status,
        exit_status=process.exit_status,
        validated_sources=process.validated_sources,
    )


def _failed_compile_result(
    process: _CompilerProcess,
    error: str,
    unavailable_formats: tuple[str, ...] = (),
) -> CompileResult:
    compiler_output = (
        CompilerOutput(process.stdout, process.stderr) if process.compiler_started else None
    )
    return CompileResult(
        "failed",
        stderr=error,
        command=process.command,
        unavailable_formats=unavailable_formats,
        resolved_compiler=process.resolved_compiler,
        dependency_context=process.context,
        compiler_started=process.compiler_started,
        failure_origin=process.failure_origin or "adapter",
        compiler_output=compiler_output,
        compiler_identity=process.compiler_identity,
        compiler_authority=process.compiler_authority,
        compiler_declarations=process.compiler_declarations,
        completion_status=process.completion_status,
        exit_status=process.exit_status,
        validated_sources=process.validated_sources,
    )


def _compiler_search_path_args(
    command: list[str],
    path: Path,
    config: Config,
    extra_paths: tuple[Path, ...],
) -> list[str]:
    if not _supports_search_paths(command):
        return []
    paths = [
        *config.compiler_search_paths,
        *(
            (project_root,)
            if (project_root := _nearest_project_root(path.parent)) is not None
            else ()
        ),
        *extra_paths,
        path.parent,
    ]
    return [argument for search_path in paths for argument in ("-p", str(search_path))]


def _unsupported_output_format(stderr: str, formats: tuple[str, ...]) -> str | None:
    for name in formats:
        if f"Unsupported format type '{name}'" in stderr or f"KeyError: '{name}'" in stderr:
            return name
    return None


def _legacy_span_error_format(stderr: str, formats: tuple[str, ...]) -> str | None:
    if not _legacy_span_error(stderr):
        return None
    for name in ("ast", "layout", "method_identifiers"):
        if name in formats:
            return name
    return None


def _legacy_span_error(stderr: str) -> bool:
    return "ValueError: start (" in stderr and "precedes previous end" in stderr


def _supports_search_paths(command: list[str]) -> bool:
    return legacy_prerelease_version(_command_vyper_version(command)) is None


def _command_vyper_version(command: list[str]) -> str | None:
    for index, arg in enumerate(command):
        if arg == "--with" and index + 1 < len(command):
            package = command[index + 1]
            if package.startswith("vyper=="):
                return package.removeprefix("vyper==")
    return None


def _compiler_coherence(source_spec: str | None) -> str:
    if source_spec is None:
        return ""
    versions = [str(version) for version in known_versions_satisfying(source_spec)]
    return json.dumps(
        {"declaration": source_spec, "versions": versions},
        sort_keys=True,
        separators=(",", ":"),
    )


def _run_compiler_process(
    command: list[str],
    path: Path,
    *,
    project_compiler: bool,
    python_pin: str | None = None,
    cwd: Path | None = None,
) -> _CompilerProcess:
    pyproject = _nearest_pyproject(path.parent)
    marker_environment: Mapping[str, str] | None = None
    if pyproject is not None:
        try:
            marker_environment = _project_marker_environment(
                pyproject,
                command,
                python_pin=python_pin,
                pin_name="source" if project_compiler else "target",
            )
        except ProjectEnvironmentError as exc:
            project = _project_data(pyproject).get("project")
            constraint = project.get("requires-python") if isinstance(project, dict) else None
            return _CompilerProcess(
                command=command,
                context=DependencyContext(
                    mode="project",
                    project_root=str(_owning_uv_workspace(pyproject).parent.resolve()),
                    python_constraint=constraint if isinstance(constraint, str) else None,
                ),
                failure_origin="environment",
                error=str(exc),
                compiler_authority="fixed-target" if not project_compiler else "default",
            )
    project_declares_vyper = project_compiler and _project_declares_vyper(
        pyproject,
        marker_environment,
    )
    source_spec = infer_pragma(path.read_text(encoding="utf-8"))
    authority = _compiler_authority(
        command,
        path,
        project_compiler=project_compiler,
        marker_environment=marker_environment,
    )
    declarations = _compiler_declarations(
        command,
        path,
        project_compiler=project_compiler,
        marker_environment=marker_environment,
    )
    coherence = _compiler_coherence(source_spec) if project_declares_vyper else ""
    with _declared_project_environment(
        command,
        path,
        project_compiler=project_compiler,
        marker_environment=marker_environment,
        python_pin=python_pin,
    ) as (full, context):
        runner_command = _compiler_runner_command(full)
        managed_compiler = (
            _is_uv_run_command(full) and bool(runner_command) and runner_command[0] == "vyper"
        )
        return _run_compiler_adapter(
            full,
            context,
            managed_compiler=managed_compiler,
            compiler_authority=authority,
            compiler_declarations=declarations,
            coherence_spec=coherence,
            cwd=cwd,
        )


def _run_compiler_adapter(
    command: list[str],
    context: DependencyContext,
    *,
    managed_compiler: bool,
    compiler_authority: CompilerAuthority,
    compiler_declarations: tuple[CompilerDeclaration, ...],
    coherence_spec: str | None,
    cwd: Path | None,
) -> _CompilerProcess:
    result_file = tempfile.NamedTemporaryFile(
        prefix="vyupgrade-compiler-result-",
        suffix=".json",
        delete=False,
    )
    result_path = Path(result_file.name)
    result_file.close()
    result_path.unlink()
    runner = [
        *_compiler_runner_prefix(command),
        str(Path(__file__).with_name("compiler_runner.py")),
        str(result_path),
        str(COMPILE_TIMEOUT_SECONDS),
        "managed" if managed_compiler else "explicit",
        coherence_spec or "",
        *_compiler_runner_command(command),
    ]
    try:
        try:
            process = subprocess.run(
                runner,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=COMPILE_TIMEOUT_SECONDS + 30,
            )
        except subprocess.TimeoutExpired:
            payload = _compiler_runner_result(result_path)
            return _CompilerProcess(
                command=command,
                context=_context_with_resolved_packages(context, payload),
                resolved_compiler=_payload_string(payload, "resolved_compiler"),
                compiler_started=payload is not None,
                failure_origin="timeout",
                error=f"compiler timed out after {COMPILE_TIMEOUT_SECONDS} seconds",
                compiler_identity=_payload_compiler_identity(payload),
                compiler_authority=compiler_authority,
                compiler_declarations=compiler_declarations,
                completion_status="timed-out",
            )
        except OSError as exc:
            return _CompilerProcess(
                command=command,
                context=context,
                failure_origin="environment" if _is_uv_run_command(command) else "launch",
                error=f"compiler environment failed to start: {exc}",
                compiler_authority=compiler_authority,
                compiler_declarations=compiler_declarations,
            )

        payload = _compiler_runner_result(result_path)
        if payload is None or payload.get("state") != "complete":
            wrapper_error = process.stderr.strip() or process.stdout.strip()
            signaled = process.returncode < 0
            return _CompilerProcess(
                command=command,
                context=_context_with_resolved_packages(context, payload),
                compiler_started=payload is not None,
                failure_origin=(
                    "compiler-internal"
                    if signaled
                    else "environment"
                    if payload is None and _is_uv_run_command(command) and process.returncode != 0
                    else "adapter"
                ),
                error=wrapper_error or "compiler adapter returned no result",
                compiler_identity=_payload_compiler_identity(payload),
                compiler_authority=compiler_authority,
                compiler_declarations=compiler_declarations,
                completion_status="signaled" if signaled else "adapter-failed",
                exit_status=ExitStatus(
                    process.returncode,
                    -process.returncode if signaled else None,
                ),
            )
        return _compiler_process_from_payload(
            command,
            context,
            payload,
            compiler_authority=compiler_authority,
            compiler_declarations=compiler_declarations,
        )
    finally:
        result_path.unlink(missing_ok=True)


def _compiler_runner_prefix(command: list[str]) -> list[str]:
    if not _is_uv_run_command(command):
        return [sys.executable]
    return [*command[: _uv_run_command_index(command)], "python"]


def _compiler_runner_command(command: list[str]) -> list[str]:
    if not _is_uv_run_command(command):
        return command
    return command[_uv_run_command_index(command) :]


def _compiler_runner_result(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _compiler_process_from_payload(
    command: list[str],
    context: DependencyContext,
    payload: dict[str, object],
    *,
    compiler_authority: CompilerAuthority,
    compiler_declarations: tuple[CompilerDeclaration, ...],
) -> _CompilerProcess:
    raw_origin = payload.get("failure_origin")
    origin = (
        cast(FailureOrigin, raw_origin)
        if raw_origin
        in {"compiler", "compiler-internal", "environment", "launch", "timeout", "adapter"}
        else None
    )
    raw_returncode = payload.get("returncode")
    returncode = raw_returncode if type(raw_returncode) is int else None
    raw_completion = payload.get("completion_status")
    completion_status = (
        cast(CompletionStatus, raw_completion)
        if raw_completion in {"not-started", "completed", "timed-out", "signaled", "adapter-failed"}
        else "adapter-failed"
    )
    return _CompilerProcess(
        command=command,
        context=_context_with_resolved_packages(context, payload),
        returncode=returncode,
        stdout=_payload_string(payload, "stdout") or "",
        stderr=_payload_string(payload, "stderr") or "",
        resolved_compiler=_payload_string(payload, "resolved_compiler"),
        compiler_started=payload.get("compiler_started") is True,
        failure_origin=origin,
        error=_payload_string(payload, "error"),
        compiler_identity=_payload_compiler_identity(payload),
        compiler_authority=compiler_authority,
        compiler_declarations=compiler_declarations,
        completion_status=completion_status,
        exit_status=ExitStatus(
            returncode,
            -returncode if returncode is not None and returncode < 0 else None,
        ),
    )


def _payload_string(payload: dict[str, object] | None, key: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _payload_compiler_identity(
    payload: dict[str, object] | None,
) -> ResolvedCompiler | None:
    if payload is None:
        return None
    value = payload.get("compiler_identity")
    if not isinstance(value, dict):
        return None
    version = value.get("version")
    executable = _payload_content_identity(value.get("executable"))
    artifact = _payload_content_identity(value.get("artifact"))
    if not isinstance(version, str) or executable is None or artifact is None:
        return None
    return ResolvedCompiler(version, executable, artifact)


def _payload_content_identity(value: object) -> ContentIdentity | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(path, str) or not isinstance(digest, str):
        return None
    return ContentIdentity(path, digest)


def _context_with_resolved_packages(
    context: DependencyContext,
    payload: dict[str, object] | None,
) -> DependencyContext:
    if payload is None:
        return context
    raw_packages = payload.get("resolved_packages")
    if not isinstance(raw_packages, list):
        return context
    packages: list[ResolvedPackage] = []
    for value in raw_packages:
        if not isinstance(value, dict):
            return context
        name = value.get("name")
        version = value.get("version")
        source = value.get("source")
        artifact_sha256 = value.get("artifact_sha256")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or (source is not None and not isinstance(source, str))
            or not isinstance(artifact_sha256, str)
        ):
            return context
        packages.append(ResolvedPackage(name, version, source, artifact_sha256))
    return replace(context, resolved_packages=tuple(packages))


def _content_identity(path: Path, *, content_path: Path | None = None) -> ContentIdentity:
    content = content_path or path
    return ContentIdentity(
        str(path.resolve()),
        hashlib.sha256(content.read_bytes()).hexdigest(),
    )


def _compiler_authority(
    command: list[str],
    path: Path,
    *,
    project_compiler: bool,
    marker_environment: Mapping[str, str] | None = None,
) -> CompilerAuthority:
    pyproject = _nearest_pyproject(path.parent)
    if not _is_uv_run_command(command):
        return "explicit-executable"
    if project_compiler and _project_declares_vyper(pyproject, marker_environment):
        assert pyproject is not None
        workspace_manifest = _owning_uv_workspace(pyproject)
        return (
            "project-lock"
            if (workspace_manifest.parent / "uv.lock").is_file()
            else "project-manifest"
        )
    if not project_compiler:
        return "fixed-target"
    source_spec = infer_pragma(path.read_text(encoding="utf-8"))
    if source_spec is None:
        return "default"
    return "source-exact" if parse_version(source_spec) is not None else "source-range"


def _compiler_declarations(
    command: list[str],
    path: Path,
    *,
    project_compiler: bool,
    marker_environment: Mapping[str, str] | None = None,
) -> tuple[CompilerDeclaration, ...]:
    declarations: list[CompilerDeclaration] = []
    pyproject = _nearest_pyproject(path.parent)
    if project_compiler:
        if pyproject is not None:
            declarations.extend(
                CompilerDeclaration("project", spec, str(pyproject.resolve()))
                for spec in _project_vyper_specs(pyproject, marker_environment)
            )
        source_spec = infer_pragma(path.read_text(encoding="utf-8"))
        if source_spec is not None:
            declarations.append(
                CompilerDeclaration("source-pragma", source_spec, str(path.resolve()))
            )
    else:
        target_version = _command_vyper_version(command)
        if target_version is not None:
            declarations.append(CompilerDeclaration("target-version", target_version))
    if not _is_uv_run_command(command):
        declarations.append(CompilerDeclaration("explicit-executable", command[0]))
    return tuple(declarations)


def _project_vyper_specs(
    pyproject: Path,
    marker_environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    return tuple(
        dependency
        for dependency in _project_dependency_specs(
            _project_data(pyproject),
            marker_environment,
        )
        if _dependency_name(dependency) == "vyper"
    )


def _owning_uv_workspace(pyproject: Path) -> Path:
    selected_root = pyproject.parent.resolve()
    for root in (selected_root, *selected_root.parents):
        workspace_manifest = root / "pyproject.toml"
        workspace = _uv_workspace(_project_data(workspace_manifest))
        if workspace is None:
            continue
        if root == selected_root or selected_root in _uv_workspace_member_roots(root, workspace):
            return workspace_manifest
    return pyproject


def _uv_workspace(data: dict[str, object]) -> dict[str, object] | None:
    tool = data.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    workspace = uv.get("workspace") if isinstance(uv, dict) else None
    return workspace if isinstance(workspace, dict) else None


def _uv_workspace_member_roots(
    root: Path,
    workspace: dict[str, object],
) -> tuple[Path, ...]:
    members = _workspace_glob_roots(root, workspace.get("members"))
    excluded = set(_workspace_glob_roots(root, workspace.get("exclude")))
    return tuple(
        member
        for member in members
        if member not in excluded and (member / "pyproject.toml").is_file()
    )


def _workspace_glob_roots(root: Path, value: object) -> tuple[Path, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        sorted(
            {
                candidate.resolve()
                for pattern in value
                if isinstance(pattern, str)
                for candidate in root.glob(pattern)
                if candidate.is_dir()
            }
        )
    )


def _project_marker_environment(
    pyproject: Path,
    command: Sequence[str],
    *,
    python_pin: str | None,
    pin_name: str,
) -> dict[str, str]:
    requested_python = (
        None
        if _use_project_interpreter(command, pyproject, python_pin=python_pin)
        else python_pin or _uv_option_value(command, "--python")
    )
    marker_command = [
        _uv_bin(),
        "run",
        "--isolated",
        "--project",
        str(pyproject.parent.resolve()),
        "--no-sync",
        *((("--python", requested_python)) if requested_python is not None else ()),
        "python",
        "-I",
        "-c",
        _MARKER_ENVIRONMENT_SCRIPT,
    ]
    try:
        process = subprocess.run(
            marker_command,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectEnvironmentError(f"could not select project Python: {exc}") from exc
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "interpreter selection failed"
        project = _project_data(pyproject).get("project")
        constraint = project.get("requires-python") if isinstance(project, dict) else None
        if (
            python_pin is not None
            and isinstance(constraint, str)
            and "incompatible with the project's Python requirement" in detail
        ):
            raise ProjectEnvironmentError(
                f"{pin_name} Python pin {python_pin!r} conflicts with "
                f"project requires-python {constraint!r}: {detail}"
            )
        raise ProjectEnvironmentError(f"could not select project Python: {detail}")
    try:
        environment = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ProjectEnvironmentError(
            "selected project Python returned invalid marker data"
        ) from exc
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise ProjectEnvironmentError("selected project Python returned invalid marker data")
    return environment


@contextmanager
def _declared_project_environment(
    command: list[str],
    path: Path,
    *,
    project_compiler: bool,
    marker_environment: Mapping[str, str] | None = None,
    python_pin: str | None = None,
) -> Iterator[tuple[list[str], DependencyContext]]:
    pyproject = _nearest_pyproject(path.parent)
    if pyproject is None:
        yield command, DependencyContext(mode="isolated")
        return

    if marker_environment is None:
        marker_environment = _project_marker_environment(
            pyproject,
            command,
            python_pin=python_pin,
            pin_name="source" if project_compiler else "target",
        )

    workspace_manifest = _owning_uv_workspace(pyproject)
    root = workspace_manifest.parent.resolve()
    selected_root = pyproject.parent.resolve()
    lockfile = root / "uv.lock"
    selected_data = _project_data(pyproject)
    root_data = _project_data(workspace_manifest)
    project = selected_data.get("project")
    python_constraint = project.get("requires-python") if isinstance(project, dict) else None
    workspace = _uv_workspace(root_data)
    workspace_members = _uv_workspace_member_roots(root, workspace) if workspace is not None else ()
    declared_source_paths = _declared_local_source_paths(
        workspace_manifest,
        root_data,
        workspace_members,
    )
    context = DependencyContext(
        mode="project",
        project_root=str(root),
        manifest=_content_identity(workspace_manifest),
        lockfile=_content_identity(lockfile) if lockfile.is_file() else None,
        python_constraint=python_constraint if isinstance(python_constraint, str) else None,
        declared_sources=tuple(
            sorted(
                (_tree_identity(path) for path in declared_source_paths),
                key=lambda identity: identity.path,
            )
        ),
    )
    if lockfile.is_file():
        yield (
            _project_environment_command(
                command,
                selected_root,
                pyproject,
                project_compiler=project_compiler,
                frozen=True,
                marker_environment=marker_environment,
                python_pin=python_pin,
            ),
            context,
        )
        return

    with tempfile.TemporaryDirectory(prefix="vyupgrade-project-") as raw_directory:
        temporary_root = _mirror_declared_project(
            root,
            Path(raw_directory),
            _declared_relative_source_paths(workspace_manifest, root_data),
        )
        temporary_project = temporary_root / selected_root.relative_to(root)
        yield (
            _project_environment_command(
                command,
                temporary_project,
                pyproject,
                project_compiler=project_compiler,
                frozen=False,
                marker_environment=marker_environment,
                python_pin=python_pin,
            ),
            context,
        )


def _project_environment_command(
    command: list[str],
    root: Path,
    pyproject: Path,
    *,
    project_compiler: bool,
    frozen: bool,
    marker_environment: Mapping[str, str],
    python_pin: str | None,
) -> list[str]:
    environment = [
        _uv_bin(),
        "run",
        "--isolated",
        "--project",
        str(root),
        *(("--frozen",) if frozen else ()),
        "--all-extras",
        "--all-groups",
    ]
    if not _is_uv_run_command(command):
        return [*environment, *command]
    project_declares_vyper = _project_declares_vyper(pyproject, marker_environment)

    command_index = _uv_run_command_index(command)
    options = _project_uv_options(
        command[2:command_index],
        remove_vyper=project_compiler and project_declares_vyper,
        remove_python=_use_project_interpreter(command, pyproject, python_pin=python_pin),
    )
    return [*environment, *options, *command[command_index:]]


def _use_project_interpreter(
    command: Sequence[str],
    pyproject: Path,
    *,
    python_pin: str | None,
) -> bool:
    if python_pin is not None:
        return False
    if _project_has_vyper_requirement(pyproject):
        return True
    constraint = _project_data(pyproject).get("project")
    constraint = constraint.get("requires-python") if isinstance(constraint, dict) else None
    requested = _uv_option_value(command, "--python")
    if not isinstance(constraint, str) or requested is None:
        return False
    try:
        return not SpecifierSet(constraint).contains(Version(requested), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return True


def _uv_option_value(command: Sequence[str], option: str) -> str | None:
    try:
        index = command.index(option)
    except ValueError:
        return None
    return command[index + 1] if index + 1 < len(command) else None


def _project_data(pyproject: Path) -> dict[str, object]:
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _declared_local_source_paths(
    pyproject: Path,
    data: dict[str, object],
    workspace_members: tuple[Path, ...] = (),
) -> tuple[Path, ...]:
    tool = data.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    sources = uv.get("sources") if isinstance(uv, dict) else None
    if not isinstance(sources, dict):
        return ()
    paths: list[Path] = []
    for name, source in sources.items():
        if not isinstance(source, dict):
            continue
        path_value = source.get("path")
        path = (
            (pyproject.parent / path_value).resolve()
            if isinstance(path_value, str)
            else _workspace_source_path(name, source, workspace_members)
        )
        if path is not None and path.exists():
            paths.append(path)
    return tuple(sorted(set(paths)))


def _declared_relative_source_paths(
    pyproject: Path,
    data: dict[str, object],
) -> tuple[Path, ...]:
    tool = data.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    sources = uv.get("sources") if isinstance(uv, dict) else None
    if not isinstance(sources, dict):
        return ()
    return tuple(
        sorted(
            {
                (pyproject.parent / path_value).resolve()
                for source in sources.values()
                if isinstance(source, dict)
                and isinstance((path_value := source.get("path")), str)
                and not Path(path_value).is_absolute()
                and (pyproject.parent / path_value).exists()
            }
        )
    )


def _workspace_source_path(
    name: object,
    source: dict[str, object],
    workspace_members: tuple[Path, ...],
) -> Path | None:
    if not isinstance(name, str) or source.get("workspace") is not True:
        return None
    normalized_name = _normalized_dependency_name(name)
    matching = [
        member
        for member in workspace_members
        if _project_name(member / "pyproject.toml") == normalized_name
    ]
    return matching[0] if len(matching) == 1 else None


def _project_name(pyproject: Path) -> str | None:
    project = _project_data(pyproject).get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return _normalized_dependency_name(name) if isinstance(name, str) else None


def _tree_identity(path: Path) -> ContentIdentity:
    if path.is_file():
        return _content_identity(path)
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path)
        if not child.is_file() or any(part in OVERLAY_EXCLUDED_PARTS for part in relative.parts):
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return ContentIdentity(str(path.resolve()), digest.hexdigest())


def _mirror_declared_project(
    root: Path,
    target: Path,
    relative_sources: tuple[Path, ...],
) -> Path:
    layout_root = Path(os.path.commonpath((root, *relative_sources)))
    entries = sorted(
        {root, *relative_sources},
        key=lambda path: len(path.relative_to(layout_root).parts),
        reverse=True,
    )
    for source in entries:
        destination = target / source.relative_to(layout_root)
        if source.is_dir():
            _mirror_project_root(source, destination)
            continue
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source)
    return target / root.relative_to(layout_root)


def _mirror_project_root(source: Path, target: Path) -> None:
    for config in _project_config_paths(source, ()):
        relative = config.relative_to(source)
        if config.name == "uv.lock" or any(
            part in OVERLAY_EXCLUDED_PARTS for part in relative.parts
        ):
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config, destination)
    _mirror_project_entries(source, target)


def _mirror_project_entries(source: Path, target: Path) -> None:
    for child in source.iterdir():
        if child.name == "uv.lock" or child.name in OVERLAY_EXCLUDED_PARTS:
            continue
        destination = target / child.name
        if destination.exists():
            if child.is_dir():
                _mirror_project_entries(child, destination)
            continue
        destination.symlink_to(child, target_is_directory=child.is_dir())


def _project_uv_options(
    options: Sequence[str], *, remove_vyper: bool, remove_python: bool
) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(options):
        option = options[index]
        if option == "--no-project":
            index += 1
            continue
        if remove_python and option == "--python":
            index += 2
            continue
        if (
            remove_vyper
            and option == "--with"
            and index + 1 < len(options)
            and options[index + 1].startswith("vyper==")
        ):
            index += 2
            continue
        kept.append(option)
        index += 1
        if option in {"--with", "--with-editable", "--with-requirements", "--env-file"}:
            kept.append(options[index])
            index += 1
    return kept


def _uv_run_command_index(command: Sequence[str]) -> int:
    index = 2
    options_with_value = {
        "--python",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--env-file",
        "--project",
    }
    while index < len(command):
        argument = command[index]
        if argument == "--":
            return index + 1
        if not argument.startswith("-"):
            return index
        index += 1
        if argument in options_with_value and index < len(command):
            index += 1
    return len(command)


def _is_uv_run_command(command: Sequence[str]) -> bool:
    return len(command) >= 3 and Path(command[0]).name == "uv" and command[1] == "run"


def _project_has_vyper_requirement(pyproject: Path) -> bool:
    return any(
        _dependency_name(dependency) == "vyper"
        for dependency in _project_dependency_specs(
            _project_data(pyproject),
            evaluate_markers=False,
        )
    )


def _project_declares_vyper(
    pyproject: Path | None,
    marker_environment: Mapping[str, str] | None = None,
) -> bool:
    return pyproject is not None and bool(_project_vyper_specs(pyproject, marker_environment))


def _project_dependency_specs(
    data: dict[str, object],
    marker_environment: Mapping[str, str] | None = None,
    *,
    evaluate_markers: bool = True,
) -> Iterator[str]:
    project = data.get("project")
    if isinstance(project, dict):
        yield from _active_string_dependencies(
            project.get("dependencies"),
            marker_environment=marker_environment,
            evaluate_markers=evaluate_markers,
        )
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for extra, dependencies in optional.items():
                yield from _active_string_dependencies(
                    dependencies,
                    marker_environment=marker_environment,
                    evaluate_markers=evaluate_markers,
                    extra=extra if isinstance(extra, str) else None,
                )
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for dependencies in groups.values():
            yield from _active_string_dependencies(
                dependencies,
                marker_environment=marker_environment,
                evaluate_markers=evaluate_markers,
            )
    tool = data.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    if isinstance(uv, dict):
        yield from _active_string_dependencies(
            uv.get("dev-dependencies"),
            marker_environment=marker_environment,
            evaluate_markers=evaluate_markers,
        )


def _active_string_dependencies(
    value: object,
    *,
    marker_environment: Mapping[str, str] | None = None,
    evaluate_markers: bool = True,
    extra: str | None = None,
) -> Iterator[str]:
    if not isinstance(value, list):
        return
    for dependency in value:
        if not isinstance(dependency, str):
            continue
        try:
            requirement = Requirement(dependency)
        except InvalidRequirement:
            continue
        environment = (
            {**marker_environment, "extra": extra or ""}
            if marker_environment is not None
            else {"extra": extra or ""}
        )
        if (
            not evaluate_markers
            or requirement.marker is None
            or requirement.marker.evaluate(environment)
        ):
            yield dependency


def _dependency_name(dependency: str) -> str | None:
    try:
        return _normalized_dependency_name(Requirement(dependency).name)
    except InvalidRequirement:
        return None


def _normalized_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _nearest_project_root(start: Path) -> Path | None:
    pyproject = _nearest_pyproject(start)
    return pyproject.parent if pyproject is not None else None


def _nearest_pyproject(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        pyproject = directory / "pyproject.toml"
        if pyproject.exists():
            return pyproject
    return None


def _parse_outputs(stdout: str, formats: tuple[str, ...] = FORMATS) -> dict[str, object]:
    chunks = [line for line in stdout.splitlines() if line.strip()]
    if len(chunks) != len(formats):
        raise ValueError(f"expected {len(formats)} compiler outputs, received {len(chunks)}")
    artifacts: dict[str, object] = {}
    for name, raw in zip(formats, chunks, strict=True):
        artifacts[name] = json.loads(raw)
    return artifacts
