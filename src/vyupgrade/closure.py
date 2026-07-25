from __future__ import annotations

import os
import stat

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from . import compiler
from .models import Config


@dataclass(frozen=True)
class ClosureWriteResult:
    status: str
    root: Path
    files: tuple[Path, ...]
    error: str | None = None


def write_closure_output(
    output_root: Path,
    sources: Mapping[Path, str],
    target_version: str,
    search_paths: tuple[Path, ...] = (),
) -> ClosureWriteResult:
    resolved = output_root
    try:
        resolved = output_root.resolve()
        if resolved.exists() and not resolved.is_dir():
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                "closure output destination is not a directory",
            )
        if not sources:
            return ClosureWriteResult("written", resolved, ())
        members = compiler.resolve_import_closure(sources, search_paths).files
        if any(member.is_relative_to(resolved) for member in members):
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                "refusing to write the closure into a directory that contains migration sources",
            )
        relative_files = _closure_relative_files(sources, target_version, search_paths)
        linked = _linked_output_path(resolved, relative_files)
        if linked is not None:
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                f"refusing to write the closure through linked output path: {linked}",
            )
        resolved.mkdir(parents=True, exist_ok=True)
        overlay = compiler.materialize_target_overlay(
            sources,
            target_version,
            resolved,
            search_paths,
            include_dependencies=True,
        )
        assert overlay is not None
        return ClosureWriteResult("written", resolved, tuple(sorted(set(overlay.paths.values()))))
    except (compiler.OverlayLayoutConflictError, OSError) as exc:
        return ClosureWriteResult("failed", resolved, (), str(exc))


def write_closure_archive(
    output: Path,
    entry: Path,
    sources: Mapping[Path, str],
    config: Config,
) -> ClosureWriteResult:
    resolved = output
    try:
        resolved = output.resolve()
        if entry not in sources:
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                f"archive entry is missing from closure sources: {entry}",
            )
        members = compiler.resolve_import_closure(sources, config.compiler_search_paths).files
        if resolved in members:
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                f"refusing to overwrite closure source with archive: {resolved}",
            )
        with compiler.target_overlay(
            sources,
            config.target_version,
            config.compiler_search_paths,
            include_dependencies=True,
        ) as overlay:
            assert overlay is not None
            result = compiler.compile_target_archive(
                entry, sources[entry], config, overlay, resolved
            )
        if result.status == "passed":
            return ClosureWriteResult("written", resolved, (resolved,))
        return ClosureWriteResult("failed", resolved, (), result.stderr)
    except (compiler.OverlayLayoutConflictError, OSError) as exc:
        return ClosureWriteResult("failed", resolved, (), str(exc))


def _closure_relative_files(
    sources: Mapping[Path, str],
    target_version: str,
    search_paths: tuple[Path, ...],
) -> tuple[Path, ...]:
    with TemporaryDirectory(prefix="vyupgrade-closure-") as tmp:
        root = Path(tmp)
        overlay = compiler.materialize_target_overlay(
            sources,
            target_version,
            root,
            search_paths,
            include_dependencies=True,
        )
        assert overlay is not None
        return tuple(path.relative_to(root) for path in root.rglob("*") if path.is_file())


def _linked_output_path(root: Path, relative_files: tuple[Path, ...]) -> Path | None:
    for relative in relative_files:
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if os.path.islink(current):
                return current
        destination = root / relative
        if os.path.islink(destination):
            return destination
        if destination.exists():
            destination_stat = os.lstat(destination)
            if stat.S_ISREG(destination_stat.st_mode) and destination_stat.st_nlink > 1:
                return destination
    return None
