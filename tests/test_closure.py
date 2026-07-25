from __future__ import annotations

from pathlib import Path

import pytest

from vyupgrade import compiler
from vyupgrade.closure import write_closure_archive, write_closure_output
from vyupgrade.models import Config


def _write_closure_fixture(
    tmp_path: Path,
) -> tuple[dict[Path, str], tuple[Path, ...], tuple[Path, ...]]:
    project_root = tmp_path / "project"
    project = project_root / "src" / "main.vy"
    search_root = tmp_path / "site-packages"
    dependency = search_root / "depkg" / "util.vy"
    interface = dependency.with_name("IUtil.json")
    project.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text(
        '[project]\nname = "closure-project"\n', encoding="utf-8"
    )
    project.write_text("# @version 0.3.10\nfrom depkg import util, IUtil\n", encoding="utf-8")
    dependency.write_text("# @version 0.3.10\nVALUE: constant(uint256) = 1\n", encoding="utf-8")
    interface.write_text("[]\n", encoding="utf-8")
    candidates = {
        project: "#pragma version 0.4.3\nfrom depkg import util, IUtil\n",
        dependency: (
            "#pragma version 0.4.3\nVALUE: constant(uint256) = 2\n# exact dependency candidate\n"
        ),
    }
    return candidates, (search_root,), (project, dependency, interface)


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_write_closure_output_materializes_import_root_relative_tree(
    tmp_path: Path,
) -> None:
    sources, search_paths, (_project, dependency, _interface) = _write_closure_fixture(tmp_path)
    output = tmp_path / "output"

    result = write_closure_output(output, sources, "0.4.3", search_paths)

    assert result.status == "written"
    assert result.error is None
    assert (output / "src" / "main.vy").read_text() == sources[next(iter(sources))]
    assert (output / "depkg" / "util.vy").read_text() == sources[dependency]
    assert (output / "depkg" / "IUtil.json").read_bytes() == b"[]\n"
    assert (output / "pyproject.toml").is_file()
    assert result.files == tuple(
        sorted(
            (
                output / "src" / "main.vy",
                output / "depkg" / "util.vy",
                output / "depkg" / "IUtil.json",
            )
        )
    )


def test_write_closure_output_files_match_resolved_closure(tmp_path: Path) -> None:
    sources, search_paths, _inputs = _write_closure_fixture(tmp_path)
    output = tmp_path / "output"
    source_closure = compiler.resolve_import_closure(sources, search_paths)

    result = write_closure_output(output, sources, "0.4.3", search_paths)

    assert result.status == "written"
    assert len(result.files) == len(source_closure.files)
    assert {path.relative_to(output) for path in result.files} == {
        Path("src/main.vy"),
        Path("depkg/util.vy"),
        Path("depkg/IUtil.json"),
    }


def test_write_closure_output_allows_non_empty_dir_and_keeps_extras(
    tmp_path: Path,
) -> None:
    sources, search_paths, _inputs = _write_closure_fixture(tmp_path)
    output = tmp_path / "output"
    stale = output / "src" / "main.vy"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")
    extra = output / "keep.txt"
    extra.write_text("keep")

    result = write_closure_output(output, sources, "0.4.3", search_paths)

    assert result.status == "written"
    assert stale.read_text() == sources[next(iter(sources))]
    assert extra.read_text() == "keep"


def test_write_closure_output_is_idempotent(tmp_path: Path) -> None:
    sources, search_paths, _inputs = _write_closure_fixture(tmp_path)
    output = tmp_path / "output"

    first = write_closure_output(output, sources, "0.4.3", search_paths)
    first_tree = _tree_bytes(output)
    second = write_closure_output(output, sources, "0.4.3", search_paths)

    assert first.status == second.status == "written"
    assert first.files == second.files
    assert _tree_bytes(output) == first_tree


def test_write_closure_output_refuses_dir_containing_project_sources(
    tmp_path: Path,
) -> None:
    sources, search_paths, inputs = _write_closure_fixture(tmp_path)
    output = inputs[0].parents[1]
    before = _tree_bytes(output)

    result = write_closure_output(output, sources, "0.4.3", search_paths)

    assert result.status == "failed"
    assert result.files == ()
    assert result.error == (
        "refusing to write the closure into a directory that contains migration sources"
    )
    assert _tree_bytes(output) == before


def test_write_closure_output_refuses_dir_containing_site_package_sources(
    tmp_path: Path,
) -> None:
    sources, search_paths, inputs = _write_closure_fixture(tmp_path)
    output = search_paths[0]
    before = _tree_bytes(output)

    result = write_closure_output(output, sources, "0.4.3", search_paths)

    assert result.status == "failed"
    assert result.files == ()
    assert result.error is not None
    assert "contains migration sources" in result.error
    assert _tree_bytes(output) == before
    assert all(path.read_bytes() for path in inputs)


def test_write_closure_output_never_touches_sources(tmp_path: Path) -> None:
    sources, search_paths, inputs = _write_closure_fixture(tmp_path)
    before = {path: path.read_bytes() for path in inputs}

    result = write_closure_output(tmp_path / "output", sources, "0.4.3", search_paths)

    assert result.status == "written"
    assert {path: path.read_bytes() for path in inputs} == before


def test_write_closure_output_reports_oserror(tmp_path: Path) -> None:
    sources, search_paths, _inputs = _write_closure_fixture(tmp_path)
    output = tmp_path / "read-only"
    output.mkdir()
    output.chmod(0o500)
    try:
        result = write_closure_output(output, sources, "0.4.3", search_paths)
    finally:
        output.chmod(0o700)

    assert result.status == "failed"
    assert result.files == ()
    assert result.error


def test_write_closure_output_reports_layout_conflict_as_failed(
    tmp_path: Path,
) -> None:
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

    result = write_closure_output(
        tmp_path / "output",
        {first: first_source, second: second_source},
        "0.4.3",
    )

    assert result.status == "failed"
    assert result.files == ()
    assert result.error is not None
    assert str(first.resolve()) in result.error
    assert str(second.resolve()) in result.error


@pytest.mark.parametrize("link_kind", ["hardlink", "symlink"])
def test_write_closure_output_refuses_linked_dependency_destination(
    tmp_path: Path, link_kind: str
) -> None:
    sources, search_paths, (_project, dependency, _interface) = _write_closure_fixture(tmp_path)
    original = dependency.read_bytes()
    output = tmp_path / "output"
    linked = output / "depkg" / "util.vy"
    linked.parent.mkdir(parents=True)
    if link_kind == "hardlink":
        linked.hardlink_to(dependency)
    else:
        linked.symlink_to(dependency)

    result = write_closure_output(output, sources, "0.4.3", search_paths)

    assert result.status == "failed"
    assert result.files == ()
    assert result.error is not None
    assert "linked output path" in result.error
    assert dependency.read_bytes() == original


def test_write_closure_output_empty_sources_is_written_without_directory(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"

    result = write_closure_output(output, {}, "0.4.3")

    assert result.status == "written"
    assert result.root == output.resolve()
    assert result.files == ()
    assert result.error is None
    assert not output.exists()


def test_write_closure_output_deduplicates_identical_destinations(
    tmp_path: Path,
) -> None:
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
    output = tmp_path / "output"

    result = write_closure_output(output, {first: source, second: source}, "0.4.3")

    assert result.status == "written"
    assert result.files == (output / "pkg" / "util.vy",)


@pytest.mark.parametrize(
    ("compile_result", "expected_status", "expected_error"),
    [
        (compiler.CompileResult("passed"), "written", None),
        (compiler.CompileResult("failed", stderr="archive failed"), "failed", "archive failed"),
    ],
)
def test_write_closure_archive_passes_through_compile_result(
    tmp_path: Path,
    monkeypatch,
    compile_result: compiler.CompileResult,
    expected_status: str,
    expected_error: str | None,
) -> None:
    sources, search_paths, (entry, _dependency, _interface) = _write_closure_fixture(tmp_path)
    output = tmp_path / "out.vyz"
    captured: list[tuple[Path, str, Path]] = []

    def fake_compile_target_archive(
        path: Path,
        source: str,
        _config: Config,
        _overlay: compiler.TargetOverlay,
        archive: Path,
    ) -> compiler.CompileResult:
        captured.append((path, source, archive))
        return compile_result

    monkeypatch.setattr(compiler, "compile_target_archive", fake_compile_target_archive)

    result = write_closure_archive(
        output,
        entry,
        sources,
        Config(
            paths=(entry,),
            target_version="0.4.3",
            compiler_search_paths=search_paths,
        ),
    )

    assert result.status == expected_status
    assert result.error == expected_error
    assert result.files == ((output.resolve(),) if expected_status == "written" else ())
    assert captured == [(entry, sources[entry], output.resolve())]


def test_write_closure_archive_missing_entry_fails(tmp_path: Path) -> None:
    sources, search_paths, (entry, _dependency, _interface) = _write_closure_fixture(tmp_path)
    missing = entry.with_name("missing.vy")
    output = tmp_path / "out.vyz"

    result = write_closure_archive(
        output,
        missing,
        sources,
        Config(
            paths=(entry,),
            target_version="0.4.3",
            compiler_search_paths=search_paths,
        ),
    )

    assert result.status == "failed"
    assert result.files == ()
    assert result.error is not None
    assert str(missing) in result.error
    assert not output.exists()


@pytest.mark.parametrize("destination", ["entry", "dependency-symlink"])
def test_write_closure_archive_rejects_source_destinations(
    tmp_path: Path, monkeypatch, destination: str
) -> None:
    sources, search_paths, (entry, dependency, interface) = _write_closure_fixture(tmp_path)
    if destination == "entry":
        output = entry
    else:
        output = tmp_path / "out.vyz"
        output.symlink_to(dependency)
    original_bytes = {path: path.read_bytes() for path in (entry, dependency, interface)}

    def unexpected_compile(*_args, **_kwargs):
        pytest.fail("source destination must be rejected before archive compilation")

    monkeypatch.setattr(compiler, "compile_target_archive", unexpected_compile)

    result = write_closure_archive(
        output,
        entry,
        sources,
        Config(
            paths=(entry,),
            target_version="0.4.3",
            compiler_search_paths=search_paths,
        ),
    )

    assert result.status == "failed"
    assert result.files == ()
    assert result.root == output.resolve()
    assert result.error == (
        f"refusing to overwrite closure source with archive: {output.resolve()}"
    )
    assert {path: path.read_bytes() for path in original_bytes} == original_bytes
    if destination == "dependency-symlink":
        assert output.is_symlink()


def test_write_closure_archive_uses_closure_mode_overlay(tmp_path: Path, monkeypatch) -> None:
    sources, search_paths, (entry, dependency, _interface) = _write_closure_fixture(tmp_path)
    captured_relative_paths: dict[Path, Path] = {}

    def fake_compile_target_archive(
        _path: Path,
        _source: str,
        _config: Config,
        overlay: compiler.TargetOverlay,
        _output: Path,
    ) -> compiler.CompileResult:
        captured_relative_paths.update(
            {
                source_path: staged.relative_to(overlay.root)
                for source_path, staged in overlay.paths.items()
            }
        )
        return compiler.CompileResult("passed")

    monkeypatch.setattr(compiler, "compile_target_archive", fake_compile_target_archive)

    result = write_closure_archive(
        tmp_path / "out.vyz",
        entry,
        sources,
        Config(
            paths=(entry,),
            target_version="0.4.3",
            compiler_search_paths=search_paths,
        ),
    )

    assert result.status == "written"
    assert captured_relative_paths[dependency.resolve()] == Path("depkg/util.vy")
