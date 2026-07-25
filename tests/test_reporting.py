from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from vyupgrade import __version__
from vyupgrade.models import (
    ClosureReport,
    Diagnostic,
    FileReport,
    Fix,
    RunReport,
    ValidationDecision,
    ValidationIssue,
)
from vyupgrade.reporting import (
    THEME,
    HumanReporter,
    render_rich,
    render_text,
    write_human_report,
)


class FlushCountingStream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1


def test_render_text_includes_compile_errors() -> None:
    file_report = FileReport(
        path=Path("bad.vy"),
        changed=True,
        source_compile="passed",
        target_compile="failed",
        target_error='Version specification "0.2.11" is not compatible',
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])

    text = render_text(report)

    assert "target compile: failed" in text
    assert "target error:" in text
    assert 'Version specification "0.2.11" is not compatible' in text


def test_json_report_declares_attestation_schema_version() -> None:
    report = RunReport(source_version=None, target_version="0.4.3", files=[])

    data = report.to_json_obj()

    assert data["schema_version"] == 4
    assert data["files"] == []
    assert data["target_version"] == "0.4.3"
    assert data["closure"] is None


def test_closure_summary_reports_upgraded_dependency_count() -> None:
    report = RunReport(
        source_version=None,
        target_version="0.4.3",
        files=[],
        closure=ClosureReport(
            requested=True,
            dependencies=("/deps/a.vy", "/deps/b.json"),
        ),
    )
    stream = StringIO()
    console = Console(file=stream, no_color=True, width=120)

    text = render_text(report)
    render_rich(report, console)

    assert "dependencies: 2 upgraded" in text
    assert "dependencies: 2 upgraded" in stream.getvalue()


def test_render_text_hides_stderr_for_successful_compiles() -> None:
    file_report = FileReport(
        path=Path("ok.vy"),
        changed=True,
        source_compile="passed",
        source_error="uv cache warning",
        target_compile="passed",
        target_error="warning output",
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])

    text = render_text(report)

    assert "source compile: passed" in text
    assert "target compile: passed" in text
    assert "source error:" not in text
    assert "target error:" not in text
    assert "uv cache warning" not in text
    assert "warning output" not in text


def test_render_text_labels_resolved_source_compiler() -> None:
    file_report = FileReport(
        path=Path("ok.vy"),
        changed=True,
        source_compiler="0.4.3",
        source_compile="passed",
        target_compile="passed",
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])

    text = render_text(report)

    assert "source compile (0.4.3): passed" in text


def test_render_text_records_validation_blockers_and_waivers() -> None:
    path = Path("changed.vy")
    blocker = ValidationIssue("abi_changed", "ABI changed after migration", path)
    waiver = ValidationIssue(
        "source_compile_failed",
        "source compilation did not pass",
        path,
        "--allow-unvalidated-source",
    )
    file_report = FileReport(
        path=path,
        changed=True,
        source_compile="failed",
        target_compile="passed",
        validation_decision=ValidationDecision("blocked", False, (blocker,), (waiver,)),
    )
    report = RunReport(
        source_version=None,
        target_version="0.4.3",
        files=[file_report],
        validation_decision=file_report.validation_decision,
    )

    text = render_text(report)

    assert "validation decision: blocked" in text
    assert "validation blocker: ABI changed after migration" in text
    assert "validation waiver: --allow-unvalidated-source" in text
    assert "write validation: blocked" in text
    assert "validation waivers: --allow-unvalidated-source" in text


def test_render_text_groups_repeated_fixes_and_diagnostics_by_rule_message() -> None:
    file_report = FileReport(
        path=Path("grouped.vy"),
        changed=True,
        fixes=[
            Fix("VY112", 10, "changed positional event log to keyword arguments", "", ""),
            Fix("VY112", 20, "changed positional event log to keyword arguments", "", ""),
            Fix("VY001", 1, "modernized version pragma", "", ""),
        ],
        diagnostics=[
            Diagnostic("VYD014", 30, "range(stop) has a runtime bound; add bound=... manually"),
            Diagnostic("VYD014", 40, "range(stop) has a runtime bound; add bound=... manually"),
        ],
        source_compile="passed",
        target_compile="passed",
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])

    text = render_text(report)

    assert "VY112 changed positional event log to keyword arguments (lines 10, 20)" in text
    assert "VY001 modernized version pragma (line 1)" in text
    assert "VYD014 range(stop) has a runtime bound; add bound=... manually (lines 30, 40)" in text
    assert "VY112:10" not in text


def test_write_human_report_uses_plain_text_for_non_tty_streams() -> None:
    report = RunReport(
        source_version=None,
        target_version="0.4.3",
        files=[
            FileReport(
                path=Path("ok.vy"), changed=True, source_compile="passed", target_compile="passed"
            )
        ],
    )
    stream = StringIO()

    write_human_report(report, stream)

    assert stream.getvalue() == render_text(report)


def test_render_text_summary_labels_default_dry_run_changes() -> None:
    report = RunReport(
        source_version=None,
        target_version="0.4.3",
        files=[
            FileReport(
                path=Path("ok.vy"),
                changed=True,
                fixes=[Fix("VY001", 1, "modernized version pragma", "", "")],
                source_compile="passed",
                target_compile="passed",
            )
        ],
    )

    text = render_text(report)

    assert "would change 1 files" in text
    assert "would apply 1 fixes" in text
    assert "run with --write to apply these changes" in text


def test_render_text_summary_labels_written_changes() -> None:
    report = RunReport(
        source_version=None,
        target_version="0.4.3",
        files=[
            FileReport(
                path=Path("ok.vy"),
                changed=True,
                fixes=[Fix("VY001", 1, "modernized version pragma", "", "")],
                source_compile="passed",
                target_compile="passed",
            )
        ],
        write_requested=True,
        wrote_changes=True,
    )

    text = render_text(report)

    assert "changed 1 files" in text
    assert "applied 1 fixes" in text
    assert "run with --write" not in text


def test_human_reporter_flushes_incremental_plain_text_output() -> None:
    file_report = FileReport(
        path=Path("streamed.vy"),
        changed=True,
        fixes=[
            Fix("VY112", 12, "changed positional event log to keyword arguments", "", ""),
        ],
        source_compile="passed",
        target_compile="passed",
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])
    stream = FlushCountingStream()
    reporter = HumanReporter(stream)

    reporter.start(report.source_version, report.target_version)

    assert stream.getvalue() == (
        f"vyupgrade {__version__}\nsource: inferred per file\ntarget: 0.4.3\n"
    )
    assert stream.flush_count == 1

    reporter.file(file_report)

    text_after_file = stream.getvalue()
    assert "streamed.vy" in text_after_file
    assert "changed 1 files" not in text_after_file
    assert stream.flush_count == 2

    reporter.summary(report)

    assert "would change 1 files" in stream.getvalue()
    assert stream.flush_count == 3


def test_render_rich_marks_success_warning_and_error_output() -> None:
    file_report = FileReport(
        path=Path("mixed.vy"),
        changed=True,
        diagnostics=[
            Diagnostic("VYD001", 1, "warning message", "warning"),
            Diagnostic("VYD002", 2, "error message", "error"),
        ],
        source_compile="passed",
        target_compile="failed",
        target_error="compiler failed",
        abi_equal=True,
        method_ids_equal=False,
        method_id_diff=["changed selector: f() 0x11111111 -> 0x22222222"],
        storage_layout_equal=False,
        storage_layout_diff=["changed storage: balance slot 1 uint256 -> 0 uint256"],
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="standard",
        no_color=False,
        theme=THEME,
        width=120,
    )

    render_rich(report, console)

    text = stream.getvalue()
    assert "\x1b[" in text
    assert "source compile: \x1b[32mpassed" in text
    assert "target compile: \x1b[1;31mfailed" in text
    assert "  \x1b[1;33mVYD001\x1b[0m\x1b[33m warning message\x1b[0m\x1b[2m (line 1)\x1b[0m" in text
    assert "  \x1b[1;31mVYD002\x1b[0m\x1b[31m error message\x1b[0m\x1b[2m (line 2)\x1b[0m" in text
    assert "changed selector: f() 0x11111111 -> 0x22222222" in text
    assert "storage layout unchanged: \x1b[33mFalse" in text
    assert "changed storage: balance slot 1 uint256 -> 0 uint256" in text


def test_render_rich_splits_diagnostic_line_styles() -> None:
    file_report = FileReport(
        path=Path("styled.vy"),
        changed=True,
        fixes=[
            Fix("VY001", 2, "modernized version pragma", "", ""),
        ],
        source_compile="passed",
        target_compile="passed",
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="standard",
        no_color=False,
        theme=THEME,
        width=120,
    )

    render_rich(report, console)

    assert (
        "  \x1b[1;32mVY001\x1b[0m\x1b[32m modernized version pragma\x1b[0m\x1b[2m (line 2)\x1b[0m"
    ) in stream.getvalue()


def test_closure_summary_omits_impossible_write_hint() -> None:
    report = RunReport(
        source_version=None,
        target_version="0.4.3",
        files=[
            FileReport(
                path=Path("/deps/changed.vy"),
                role="dependency",
                changed=True,
            )
        ],
        closure=ClosureReport(
            requested=True,
            dependencies=("/deps/changed.vy",),
        ),
    )
    stream = StringIO()
    console = Console(file=stream, no_color=True, width=120)

    text = render_text(report)
    render_rich(report, console)

    assert "run with --write" not in text
    assert "run with --write" not in stream.getvalue()
