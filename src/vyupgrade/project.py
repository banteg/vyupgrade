from __future__ import annotations

from pathlib import Path


DEFAULT_INCLUDE = {".vy", ".vyi"}
DEFAULT_EXCLUDES = {".git", ".venv", "venv", "build", "dist", "__pycache__"}


def discover_files(paths: tuple[Path, ...], *, excluded_roots: tuple[Path, ...] = ()) -> list[Path]:
    files: list[Path] = []
    excluded = tuple(
        dict.fromkeys(
            candidate for root in excluded_roots for candidate in (root.absolute(), root.resolve())
        )
    )

    def is_excluded(path: Path) -> bool:
        absolute, resolved = path.absolute(), path.resolve()
        return any(
            absolute.is_relative_to(root) or resolved.is_relative_to(root) for root in excluded
        )

    for path in paths:
        if path.is_file():
            if path.suffix in DEFAULT_INCLUDE:
                files.append(path)
            continue

        if not path.exists():
            raise FileNotFoundError(path)

        filter_excluded = not is_excluded(path)
        for child in path.rglob("*"):
            if filter_excluded and is_excluded(child):
                continue
            if any(part in DEFAULT_EXCLUDES for part in child.parts):
                continue
            if child.is_file() and child.suffix in DEFAULT_INCLUDE:
                files.append(child)

    return sorted(dict.fromkeys(file.resolve() for file in files))
