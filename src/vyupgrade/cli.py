from __future__ import annotations

import argparse
import difflib
import os
from dataclasses import replace
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import TextIO

from . import closure, compiler, engine
from .models import ClosureReport, Config, FileReport, RunReport, ValidationDecision
from .project import discover_files
from .reporting import HumanReporter, write_json_report
from .validation import decide_run_validation, validation_exit_code
from .versions import MigrationContext
from .write_plan import MigrationPlan, PlanConflictError, WriteTransactionError


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    pyproject = _load_pyproject_config(Path(args.config) if args.config else Path("pyproject.toml"))
    paths = [Path(path) for path in args.paths] or [
        Path(path) for path in pyproject.get("paths", [])
    ]
    if not paths:
        print("no paths supplied", file=sys.stderr)
        return 4
    config = Config(
        paths=tuple(paths),
        target_version=args.target_version or pyproject.get("target-version", "0.4.3"),
        source_version=args.source_version or _none_if_infer(pyproject.get("source-version")),
        write=args.write,
        check=args.check,
        diff=args.diff,
        report_json=Path(args.report_json or pyproject["report-json"])
        if args.report_json or pyproject.get("report-json")
        else None,
        select=_split_rules(args.select),
        ignore=_split_rules(args.ignore),
        aggressive=args.aggressive or bool(pyproject.get("aggressive", False)),
        include_dependencies=args.include_dependencies
        or bool(pyproject.get("include-dependencies", False)),
        closure_output=Path(args.closure_output or pyproject["closure-output"])
        if args.closure_output or pyproject.get("closure-output")
        else None,
        closure_archive=Path(args.closure_archive or pyproject["closure-archive"])
        if args.closure_archive or pyproject.get("closure-archive")
        else None,
        test_command=args.test_command,
        source_vyper=args.source_vyper,
        target_vyper=args.target_vyper,
        source_python=args.source_python or _string_or_none(pyproject.get("source-python")),
        target_python=args.target_python or _string_or_none(pyproject.get("target-python")),
        compiler_search_paths=tuple(
            Path(path)
            for path in (args.compiler_search_paths or pyproject.get("compiler-search-paths", []))
        ),
        enable_decimals=args.enable_decimals,
        split_interfaces=args.split_interfaces or bool(pyproject.get("split-interfaces", False)),
        format=args.format or pyproject.get("format", "none"),
        allow_unvalidated_source=args.allow_unvalidated_source
        or bool(pyproject.get("allow-unvalidated-source", False)),
        allow_abi_change=args.allow_abi_change or bool(pyproject.get("allow-abi-change", False)),
        allow_method_id_change=args.allow_method_id_change
        or bool(pyproject.get("allow-method-id-change", False)),
        allow_storage_layout_change=args.allow_storage_layout_change
        or bool(pyproject.get("allow-storage-layout-change", False)),
    )

    if config.write and config.check:
        print("--write and --check are mutually exclusive", file=sys.stderr)
        return 4
    if config.closure_output and not config.include_dependencies:
        print("--closure-output requires --include-dependencies", file=sys.stderr)
        return 4
    if config.check and config.closure_output:
        print("--check cannot be combined with --closure-output", file=sys.stderr)
        return 4
    if config.closure_archive and not config.include_dependencies:
        print("--closure-archive requires --include-dependencies", file=sys.stderr)
        return 4
    if config.check and config.closure_archive:
        print("--check cannot be combined with --closure-archive", file=sys.stderr)
        return 4
    if (
        config.write
        and config.include_dependencies
        and not (config.closure_output or config.closure_archive)
    ):
        print(
            "--write with --include-dependencies requires --closure-output or --closure-archive",
            file=sys.stderr,
        )
        return 4
    if config.closure_archive and not MigrationContext.from_specs(
        config.source_version, config.target_version
    ).target_at_least(compiler.ARCHIVE_TARGET_FLOOR):
        print(
            f"target {config.target_version} predates Vyper archive output "
            f"(requires >= {compiler.ARCHIVE_TARGET_FLOOR})",
            file=sys.stderr,
        )
        return 4

    files = discover_files(
        config.paths,
        excluded_roots=(config.closure_output,) if config.closure_output is not None else (),
    )
    human_reporter = None if config.diff else HumanReporter(sys.stdout)
    if human_reporter:
        human_reporter.start(config.source_version, config.target_version)

    requests = [
        engine.bounded_migration_request(path, path.read_bytes().decode("utf-8"), config)
        for path in files
    ]
    archive_entry: Path | None = None
    if config.closure_archive is not None:
        try:
            archive_entry = _archive_entry(requests)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 4
    closure_report = ClosureReport(requested=True) if config.include_dependencies else None
    if closure_report is not None and requests:
        dependency_requests, closure = _dependency_requests(requests, config)
        requests += dependency_requests
        closure_report.dependencies = tuple(str(path) for path in sorted(closure.dependencies))
    batch = engine.prepare_migrations(requests, config)
    reports, plan, plan_error = _build_migration_plan(batch)
    if plan_error is None:
        validation_decision = _validate_or_layout_conflict(batch, config, plan.candidate_source)
        if validation_decision is None:
            return 4
    else:
        validation_decision = decide_run_validation((), config)

    run_report = RunReport(
        source_version=config.source_version,
        target_version=config.target_version,
        files=reports,
        write_requested=config.write,
        validation_decision=validation_decision,
        test_command=config.test_command,
        closure=closure_report,
    )
    if plan_error is not None:
        run_report.write_status = "failed"
        run_report.write_output = plan_error

    if config.write and plan_error is None and validation_decision.write_allowed:
        if config.format == "mamushi" and plan.writes:
            _run_mamushi(plan, run_report)
            if run_report.formatter_status == "passed":
                validation_decision = _validate_or_layout_conflict(
                    batch, config, plan.candidate_source
                )
                if validation_decision is None:
                    return 4
                run_report.validation_decision = validation_decision
                if not validation_decision.write_allowed:
                    run_report.write_status = "blocked"
                    run_report.write_output = "formatted candidates did not pass final validation"
            else:
                run_report.write_status = "failed"
                run_report.write_output = "formatter failed before the write transaction"
        if run_report.formatter_status != "failed" and validation_decision.write_allowed:
            try:
                run_report.wrote_changes = plan.commit()
                run_report.write_status = "committed" if run_report.wrote_changes else "no-op"
                run_report.wrote_changes, _mismatches = plan.refresh_final_state()
            except WriteTransactionError as exc:
                changed, mismatches = plan.refresh_final_state()
                run_report.wrote_changes = changed
                run_report.write_status = (
                    "rollback-incomplete" if exc.rollback_incomplete else "failed"
                )
                run_report.write_output = str(exc)
                if mismatches:
                    run_report.write_output += (
                        "; on-disk bytes differ from candidates: "
                        + ", ".join(str(path) for path in mismatches)
                    )

    if config.include_dependencies and (
        config.closure_output is not None or config.closure_archive is not None
    ):
        assert run_report.closure is not None
        if config.closure_output is not None:
            run_report.closure.output_dir = str(config.closure_output.resolve())
        if config.closure_archive is not None:
            run_report.closure.archive = str(config.closure_archive.resolve())
        if (
            plan_error is not None
            or not validation_decision.write_allowed
            or not _closure_files_validated(reports)
            or run_report.write_status in {"failed", "rollback-incomplete"}
        ):
            if config.closure_output is not None:
                run_report.closure.output_status = "blocked"
            if config.closure_archive is not None:
                run_report.closure.archive_status = "blocked"
        else:
            if config.closure_output is not None:
                _emit_closure_output(config, batch, plan, run_report)
            if config.closure_archive is not None:
                assert archive_entry is not None
                _emit_closure_archive(config, archive_entry, batch, plan, run_report)

    diff_chunks = plan.diff_chunks()
    if config.include_dependencies:
        diff_chunks += _dependency_diff_chunks(batch)
    if config.diff and diff_chunks:
        _write_diff(diff_chunks, sys.stdout)

    if (
        config.test_command
        and config.write
        and validation_decision.write_allowed
        and run_report.write_status in {"committed", "no-op"}
    ):
        _run_test_command(config.test_command, run_report)
        run_report.wrote_changes, mismatches = plan.refresh_final_state()
        if mismatches:
            drift = "test command changed planned destinations: " + ", ".join(
                str(path) for path in mismatches
            )
            run_report.test_status = "failed"
            run_report.test_output = "\n".join(
                part for part in (run_report.test_output, drift) if part
            )

    if config.report_json:
        write_json_report(config.report_json, run_report)

    if human_reporter:
        for report in reports:
            human_reporter.file(report)
        human_reporter.summary(run_report)

    if run_report.write_status in {"failed", "rollback-incomplete"} or (
        run_report.closure is not None
        and (
            run_report.closure.output_status == "failed"
            or run_report.closure.archive_status == "failed"
        )
    ):
        if run_report.formatter_status == "failed":
            return 6
        return 9
    if (exit_code := validation_exit_code(validation_decision)) is not None:
        return exit_code
    if run_report.formatter_status == "failed":
        return 6
    if run_report.test_status == "failed":
        return 8
    if config.check and any(file.changed for file in reports):
        return 1
    if any(diag.severity == "error" for file in reports for diag in file.diagnostics):
        return 5
    if run_report.closure is not None and (
        run_report.closure.output_status == "blocked"
        or run_report.closure.archive_status == "blocked"
    ):
        return 9
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vyupgrade")
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--target-version")
    parser.add_argument("--source-version")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--report-json")
    parser.add_argument("--closure-output")
    parser.add_argument("--closure-archive")
    parser.add_argument("--select", default="")
    parser.add_argument("--ignore", default="")
    parser.add_argument("--aggressive", action="store_true")
    parser.add_argument("--include-dependencies", "--upgrade-closure", action="store_true")
    parser.add_argument("--test-command")
    parser.add_argument("--source-vyper")
    parser.add_argument("--target-vyper")
    parser.add_argument("--source-python")
    parser.add_argument("--target-python")
    parser.add_argument("--compiler-search-paths", nargs="*", default=[])
    parser.add_argument("--enable-decimals", action="store_true")
    parser.add_argument("--split-interfaces", "--interfaces-to-vyi", action="store_true")
    parser.add_argument("--format", choices=["none", "mamushi"])
    parser.add_argument("--allow-unvalidated-source", action="store_true")
    parser.add_argument("--allow-abi-change", action="store_true")
    parser.add_argument("--allow-method-id-change", action="store_true")
    parser.add_argument("--allow-storage-layout-change", action="store_true")
    parser.add_argument("--config", help="path to a pyproject.toml file")
    return parser


def _build_migration_plan(
    batch: engine.MigrationBatch,
) -> tuple[list[FileReport], MigrationPlan, str | None]:
    reports = batch.reports
    plan = MigrationPlan()
    try:
        for migration in batch.files:
            if migration.request.role == "dependency":
                continue
            plan.add_source(
                migration.path,
                migration.original,
                migration.rewrite.source,
                migration.report,
            )
        for migration in batch.generated:
            plan.add_generated(
                migration.file.path,
                migration.file.source,
                migration.report,
            )
    except PlanConflictError as exc:
        return reports, plan, str(exc)
    return reports, plan, None


def _archive_entry(requests: list[engine.MigrationRequest]) -> Path:
    entries = [
        request.path
        for request in requests
        if request.role == "project" and request.path.suffix == ".vy"
    ]
    if len(entries) != 1:
        raise ValueError(
            f"--closure-archive requires exactly one entry contract; got {len(entries)}"
        )
    return entries[0]


def _dependency_requests(
    requests: list[engine.MigrationRequest], config: Config
) -> tuple[list[engine.MigrationRequest], compiler.ImportClosure]:
    closure = compiler.resolve_import_closure(
        {request.path: request.original for request in requests},
        config.compiler_search_paths,
    )
    dependency_config = replace(config, source_version=None)
    dependencies = [
        engine.bounded_migration_request(
            path,
            path.read_text(encoding="utf-8"),
            dependency_config,
            role="dependency",
        )
        for path in closure.dependencies
        if path.suffix in {".vy", ".vyi"}
    ]
    return dependencies, closure


def _dependency_diff_chunks(batch: engine.MigrationBatch) -> list[str]:
    chunks: list[str] = []
    for migration in batch.files:
        if migration.request.role != "dependency" or migration.original == migration.rewrite.source:
            continue
        chunks.extend(
            difflib.unified_diff(
                migration.original.splitlines(keepends=True),
                migration.rewrite.source.splitlines(keepends=True),
                fromfile=str(migration.path),
                tofile=str(migration.path),
            )
        )
    return chunks


def _emit_closure_output(
    config: Config,
    batch: engine.MigrationBatch,
    plan: MigrationPlan,
    run_report: RunReport,
) -> None:
    assert config.closure_output is not None
    assert run_report.closure is not None
    result = closure.write_closure_output(
        config.closure_output,
        engine.candidate_sources(batch, plan.candidate_source),
        config.target_version,
        config.compiler_search_paths,
    )
    run_report.closure.output_dir = str(result.root)
    run_report.closure.output_status = result.status
    run_report.closure.output_error = result.error


def _emit_closure_archive(
    config: Config,
    entry: Path,
    batch: engine.MigrationBatch,
    plan: MigrationPlan,
    run_report: RunReport,
) -> None:
    assert config.closure_archive is not None
    assert run_report.closure is not None
    result = closure.write_closure_archive(
        config.closure_archive,
        entry,
        engine.candidate_sources(batch, plan.candidate_source),
        config,
    )
    run_report.closure.archive = str(result.root)
    run_report.closure.archive_status = result.status
    run_report.closure.archive_error = result.error


def _closure_files_validated(reports: list[FileReport]) -> bool:
    return all(report.validation_decision.status in {"passed", "waived"} for report in reports)


def _validate_or_layout_conflict(
    batch: engine.MigrationBatch,
    config: Config,
    candidate_source: engine.CandidateSource,
) -> ValidationDecision | None:
    try:
        return engine.validate_migrations(batch, config, candidate_source)
    except compiler.OverlayLayoutConflictError as exc:
        print(str(exc), file=sys.stderr)
        return None


def _write_diff(diff_chunks: list[str], stream: TextIO) -> None:
    if _should_color(stream):
        text = "".join(_colorize_diff_line(line) for line in diff_chunks)
    else:
        text = "".join(diff_chunks)
    stream.write(text)
    if not diff_chunks[-1].endswith("\n"):
        stream.write("\n")


def _should_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") is not None or os.environ.get("CLICOLOR") == "0":
        return False
    return stream.isatty()


def _colorize_diff_line(line: str) -> str:
    if line.startswith(("--- ", "+++ ")):
        return _ansi(line, "1")
    if line.startswith("@@"):
        return _ansi(line, "36")
    if line.startswith("+"):
        return _ansi(line, "32")
    if line.startswith("-"):
        return _ansi(line, "31")
    if line.startswith("\\"):
        return _ansi(line, "2")
    return line


def _ansi(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


def _split_rules(raw: str) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _load_pyproject_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        return {}
    config = tool.get("vyupgrade", {})
    return config if isinstance(config, dict) else {}


def _none_if_infer(value: object) -> str | None:
    if value in {None, "infer"}:
        return None
    return str(value)


def _string_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _run_mamushi(plan: MigrationPlan, report: RunReport) -> None:
    with tempfile.TemporaryDirectory(prefix="vyupgrade-format-") as raw_directory:
        directory = Path(raw_directory)
        staged: dict[Path, Path] = {}
        try:
            common_parent = Path(
                os.path.commonpath([str(entry.path.parent) for entry in plan.writes])
            )
            for entry in plan.writes:
                staged_path = directory / entry.path.relative_to(common_parent)
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_bytes(entry.candidate)
                staged[entry.path] = staged_path
        except OSError as exc:
            report.formatter_status = "failed"
            report.formatter_output = f"could not stage formatter inputs: {exc}"
            return

        command = ["mamushi", *map(str, staged.values())]
        report.formatter_command = shlex.join(command)
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            report.formatter_status = "failed"
            report.formatter_output = "mamushi executable not found"
            return
        except subprocess.TimeoutExpired as exc:
            report.formatter_status = "failed"
            output = _command_output(exc.stdout, exc.stderr)
            report.formatter_output = "\n".join(
                part
                for part in (
                    f"mamushi timed out after {exc.timeout:g} seconds",
                    output,
                )
                if part
            )
            return
        except OSError as exc:
            report.formatter_status = "failed"
            report.formatter_output = f"mamushi failed to start: {exc}"
            return

        report.formatter_status = "passed" if proc.returncode == 0 else "failed"
        output = _command_output(proc.stdout, proc.stderr)
        if proc.returncode != 0:
            report.formatter_output = "\n".join(
                part
                for part in (
                    f"mamushi exited with status {proc.returncode}",
                    output,
                )
                if part
            )
            return

        try:
            formatted = {
                destination: staged_path.read_bytes() for destination, staged_path in staged.items()
            }
            for candidate in formatted.values():
                candidate.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.formatter_status = "failed"
            report.formatter_output = f"could not read formatted candidates: {exc}"
            return
        plan.update_candidates(formatted)
        report.formatter_output = output or None


def _run_test_command(command: str, report: RunReport) -> None:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        report.test_status = "failed"
        output = _command_output(exc.stdout, exc.stderr)
        report.test_output = "\n".join(
            part
            for part in (f"test command timed out after {exc.timeout:g} seconds", output)
            if part
        )
        return
    except OSError as exc:
        report.test_status = "failed"
        report.test_output = f"test command failed to start: {exc}"
        return

    report.test_status = "passed" if proc.returncode == 0 else "failed"
    output = _command_output(proc.stdout, proc.stderr)
    if proc.returncode == 0:
        report.test_output = output or None
    else:
        report.test_output = "\n".join(
            part for part in (f"test command exited with status {proc.returncode}", output) if part
        )


def _command_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    return "\n".join(
        text for text in (_decode_output(stdout), _decode_output(stderr)) if text
    ).strip()


def _decode_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


if __name__ == "__main__":
    raise SystemExit(main())
