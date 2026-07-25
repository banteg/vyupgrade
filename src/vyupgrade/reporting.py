from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.text import Text
from rich.theme import Theme

from . import __version__
from .models import Diagnostic, FileReport, Fix, RunReport


THEME = Theme(
    {
        "vy.header": "bold cyan",
        "vy.path": "bold",
        "vy.muted": "dim",
        "vy.success": "green",
        "vy.warning": "yellow",
        "vy.error": "bold red",
        "vy.fix": "green",
        "vy.info": "cyan",
        "vy.fix.rule": "bold green",
        "vy.info.rule": "bold cyan",
        "vy.warning.rule": "bold yellow",
        "vy.error.rule": "bold red",
        "vy.error.message": "red",
    }
)


@dataclass(frozen=True)
class ReportGroup:
    rule: str
    message: str
    lines: tuple[int, ...]
    style: str


class HumanReporter:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.console = Console(file=stream, theme=THEME) if stream.isatty() else None

    def start(self, source_version: str | None, target_version: str) -> None:
        if self.console:
            _render_header(self.console, source_version, target_version)
        else:
            self.stream.write(_text_header(source_version, target_version))
        self._flush()

    def file(self, file_report: FileReport) -> None:
        if not _should_render_file(file_report):
            return
        if self.console:
            _render_file(self.console, file_report)
        else:
            self.stream.write(_render_text_file(file_report))
        self._flush()

    def summary(self, report: RunReport) -> None:
        if self.console:
            _render_summary(self.console, report)
        else:
            self.stream.write(_render_text_summary(report))
        self._flush()

    def _flush(self) -> None:
        self.stream.flush()


def render_text(report: RunReport) -> str:
    return (
        _text_header(report.source_version, report.target_version)
        + "".join(_render_text_file(file) for file in report.files if _should_render_file(file))
        + _render_text_summary(report)
    )


def write_human_report(report: RunReport, stream: TextIO) -> None:
    reporter = HumanReporter(stream)
    reporter.start(report.source_version, report.target_version)
    for file in report.files:
        reporter.file(file)
    reporter.summary(report)


def render_rich(report: RunReport, console: Console) -> None:
    _render_header(console, report.source_version, report.target_version)
    for file in report.files:
        if _should_render_file(file):
            _render_file(console, file)
    _render_summary(console, report)


def _text_header(source_version: str | None, target_version: str) -> str:
    source, target = _header_values(source_version, target_version)
    return "\n".join(
        [
            f"vyupgrade {__version__}",
            f"source: {source}",
            f"target: {target}",
            "",
        ]
    )


def _render_header(console: Console, source_version: str | None, target_version: str) -> None:
    source, target = _header_values(source_version, target_version)
    console.print(Text(f"vyupgrade {__version__}", style="vy.header"))
    console.print(_label_value("source", source))
    console.print(_label_value("target", target))


def _header_values(source_version: str | None, target_version: str) -> tuple[str, str]:
    return source_version or "inferred per file", target_version


def _render_text_file(file: FileReport) -> str:
    lines = ["", str(file.path)]
    for group in _group_items(file.fixes, lambda _fix: ""):
        lines.append(f"  {_group_text(group)}")
    for group in _group_items(file.diagnostics, lambda diag: _severity_style(diag.severity)):
        lines.append(f"  {_group_text(group)}")
    for label, status, error_label, error in _compile_outputs(file):
        lines.append(f"  {label}: {status}")
        if status == "failed" and error:
            lines.append(f"  {error_label}:")
            lines.extend(f"    {line}" for line in error.splitlines())
    if file.source_unavailable_formats:
        lines.append("  source unavailable outputs: " + ", ".join(file.source_unavailable_formats))
    if file.target_unavailable_formats:
        lines.append("  target unavailable outputs: " + ", ".join(file.target_unavailable_formats))
    for label, equal, diff in _artifact_checks(file):
        if equal is not None:
            lines.append(f"  {label}: {equal}")
            lines.extend(f"    {line}" for line in diff)
    if file.original_sha256 is not None:
        lines.append(f"  original SHA-256: {file.original_sha256}")
    if file.candidate_sha256 is not None:
        lines.append(f"  candidate SHA-256: {file.candidate_sha256}")
    if file.final_sha256 is not None:
        lines.append(f"  final SHA-256: {file.final_sha256}")
    if file.final_matches_candidate is not None:
        lines.append(f"  final bytes match candidate: {file.final_matches_candidate}")
    lines.append(f"  validation decision: {file.validation_decision.status}")
    lines.extend(
        f"  validation blocker: {issue.message}" for issue in file.validation_decision.blockers
    )
    lines.extend(
        f"  validation waiver: {issue.waiver} ({issue.message})"
        for issue in file.validation_decision.waivers
    )
    return "\n".join(lines) + "\n"


def _render_text_summary(report: RunReport) -> str:
    lines = [
        "",
        *(f"{verb} {count} {label}" for verb, count, label in _summary_items(report)),
        f"left {report.diagnostic_count} review diagnostics",
        f"write validation: {report.validation_decision.status}",
    ]
    if report.closure is not None:
        lines.append(f"dependencies: {len(report.closure.dependencies)} upgraded")
        if report.closure.output_status != "skipped":
            lines.append(
                f"closure output: {report.closure.output_dir} ({report.closure.output_status})"
            )
        if report.closure.archive_status != "skipped":
            lines.append(
                f"closure archive: {report.closure.archive} ({report.closure.archive_status})"
            )
    waiver_flags = sorted(
        {issue.waiver for issue in report.validation_decision.waivers if issue.waiver}
    )
    if waiver_flags:
        lines.append(f"validation waivers: {', '.join(waiver_flags)}")
    if not report.write_requested and report.changed_count and report.closure is None:
        lines.append("run with --write to apply these changes")
    if report.write_status != "skipped":
        lines.append(f"write transaction: {report.write_status}")
        if report.write_output:
            lines.append(report.write_output.rstrip())
    if report.formatter_command:
        lines.append(f"formatter: {report.formatter_status}")
        if report.formatter_output:
            lines.append(report.formatter_output.rstrip())
    if report.test_command:
        lines.append(f"test command: {report.test_status}")
        if report.test_output:
            lines.append(report.test_output.rstrip())
    return "\n".join(lines) + "\n"


def _render_summary(console: Console, report: RunReport) -> None:
    console.print()
    for verb, count, label in _summary_items(report):
        console.print(_summary_line(verb, count, label, _count_style(count)))
    console.print(
        _summary_line(
            "left",
            report.diagnostic_count,
            "review diagnostics",
            _count_style(report.diagnostic_count),
        )
    )
    console.print(
        _label_value(
            "write validation",
            report.validation_decision.status,
            _status_style(report.validation_decision.status),
        )
    )
    if report.closure is not None:
        console.print(
            _label_value(
                "dependencies",
                f"{len(report.closure.dependencies)} upgraded",
                _count_style(len(report.closure.dependencies)),
            )
        )
        if report.closure.output_status != "skipped":
            console.print(
                _label_value(
                    "closure output",
                    f"{report.closure.output_dir} ({report.closure.output_status})",
                    _status_style(report.closure.output_status),
                )
            )
        if report.closure.archive_status != "skipped":
            console.print(
                _label_value(
                    "closure archive",
                    f"{report.closure.archive} ({report.closure.archive_status})",
                    _status_style(report.closure.archive_status),
                )
            )
    waiver_flags = sorted(
        {issue.waiver for issue in report.validation_decision.waivers if issue.waiver}
    )
    if waiver_flags:
        console.print(_label_value("validation waivers", ", ".join(waiver_flags), "vy.warning"))
    if not report.write_requested and report.changed_count and report.closure is None:
        console.print(Text("run with --write to apply these changes", style="vy.muted"))
    if report.write_status != "skipped":
        console.print(
            _label_value(
                "write transaction",
                report.write_status,
                _status_style(report.write_status),
            )
        )
        if report.write_output:
            console.print(report.write_output.rstrip())
    if report.formatter_command:
        console.print(
            _label_value(
                "formatter", report.formatter_status, _status_style(report.formatter_status)
            )
        )
        if report.formatter_output:
            console.print(report.formatter_output.rstrip())
    if report.test_command:
        console.print(
            _label_value("test command", report.test_status, _status_style(report.test_status))
        )
        if report.test_output:
            console.print(report.test_output.rstrip())


def _render_file(console: Console, file: FileReport) -> None:
    console.print()
    console.print(Text(str(file.path), style="vy.path"))
    for group in _group_items(file.fixes, lambda _fix: "vy.fix"):
        console.print(_group_rich_text(group))
    for group in _group_items(file.diagnostics, lambda diag: _severity_style(diag.severity)):
        console.print(_group_rich_text(group))
    for label, status, error_label, error in _compile_outputs(file):
        console.print(_compile_line(label, status))
        if status == "failed" and error:
            console.print(_indented(f"{error_label}:", "vy.error"))
            for line in error.splitlines():
                console.print(_indented(f"  {line}", "vy.error"))
    if file.source_unavailable_formats:
        console.print(
            _indented(
                "source unavailable outputs: " + ", ".join(file.source_unavailable_formats),
                "vy.warning",
            )
        )
    if file.target_unavailable_formats:
        console.print(
            _indented(
                "target unavailable outputs: " + ", ".join(file.target_unavailable_formats),
                "vy.error",
            )
        )
    for label, equal, diff in _artifact_checks(file):
        if equal is not None:
            console.print(_bool_line(label, equal))
            _render_detail_lines(console, diff)
    if file.original_sha256 is not None:
        console.print(_indented(f"original SHA-256: {file.original_sha256}", "vy.muted"))
    if file.candidate_sha256 is not None:
        console.print(_indented(f"candidate SHA-256: {file.candidate_sha256}", "vy.muted"))
    if file.final_sha256 is not None:
        console.print(_indented(f"final SHA-256: {file.final_sha256}", "vy.muted"))
    if file.final_matches_candidate is not None:
        console.print(_bool_line("final bytes match candidate", file.final_matches_candidate))
    console.print(
        _label_value(
            "  validation decision",
            file.validation_decision.status,
            _status_style(file.validation_decision.status),
        )
    )
    for issue in file.validation_decision.blockers:
        console.print(_indented(f"validation blocker: {issue.message}", "vy.error"))
    for issue in file.validation_decision.waivers:
        console.print(
            _indented(f"validation waiver: {issue.waiver} ({issue.message})", "vy.warning")
        )


def _compile_outputs(file: FileReport) -> tuple[tuple[str, str, str, str | None], ...]:
    source_label = _compile_label("source compile", file.source_compiler)
    return (
        (source_label, file.source_compile, "source error", file.source_error),
        ("target compile", file.target_compile, "target error", file.target_error),
    )


def _compile_label(label: str, version: str | None) -> str:
    return f"{label} ({version})" if version else label


def _artifact_checks(file: FileReport) -> tuple[tuple[str, bool | None, list[str]], ...]:
    return (
        ("ABI unchanged", file.abi_equal, file.abi_diff),
        ("method IDs unchanged", file.method_ids_equal, file.method_id_diff),
        ("storage layout unchanged", file.storage_layout_equal, file.storage_layout_diff),
    )


def _summary_items(report: RunReport) -> tuple[tuple[str, int, str], ...]:
    return (
        ("changed" if report.wrote_changes else "would change", report.changed_count, "files"),
        ("applied" if report.wrote_changes else "would apply", report.fix_count, "fixes"),
    )


def _should_render_file(file: FileReport) -> bool:
    return file.changed or bool(file.diagnostics) or file.target_compile != "skipped"


def _label_value(label: str, value: object, value_style: str = "") -> Text:
    text = Text(f"{label}: ", style="vy.muted")
    text.append(str(value), style=value_style)
    return text


def _summary_line(verb: str, count: int, label: str, count_style: str) -> Text:
    text = Text(f"{verb} ")
    text.append(str(count), style=count_style)
    text.append(f" {label}")
    return text


def _compile_line(label: str, status: str) -> Text:
    text = Text(f"  {label}: ")
    text.append(status, style=_status_style(status))
    return text


def _bool_line(label: str, value: bool) -> Text:
    text = Text(f"  {label}: ")
    text.append(str(value), style="vy.success" if value else "vy.warning")
    return text


def _indented(value: str, style: str) -> Text:
    return Text(f"  {value}", style=style)


def _render_detail_lines(console: Console, lines: list[str]) -> None:
    for line in lines:
        console.print(Text(f"    {line}", style="vy.warning"))


def _group_items(
    items: Iterable[Fix | Diagnostic],
    style_for: Callable[[Fix | Diagnostic], str],
) -> list[ReportGroup]:
    groups: dict[tuple[str, str, str], list[int]] = {}
    for item in items:
        rule = item.rule
        message = item.message
        style = style_for(item)
        groups.setdefault((rule, message, style), []).append(item.line)
    return [
        ReportGroup(rule, message, tuple(lines), style)
        for (rule, message, style), lines in groups.items()
    ]


def _group_text(group: ReportGroup) -> str:
    return f"{group.rule} {group.message} ({_line_list(group.lines)})"


def _group_rich_text(group: ReportGroup) -> Text:
    text = Text("  ")
    text.append(group.rule, style=_rule_style(group.style))
    text.append(f" {group.message}", style=_message_style(group.style))
    text.append(f" ({_line_list(group.lines)})", style="vy.muted")
    return text


def _line_list(lines: tuple[int, ...]) -> str:
    prefix = "line" if len(lines) == 1 else "lines"
    return f"{prefix} {', '.join(map(str, lines))}"


def _count_style(count: int) -> str:
    return "vy.warning" if count else "vy.success"


def _status_style(status: str) -> str:
    return {
        "passed": "vy.success",
        "waived": "vy.warning",
        "blocked": "vy.error",
        "rollback-incomplete": "vy.error",
        "committed": "vy.success",
        "no-op": "vy.muted",
        "not-required": "vy.muted",
        "degraded": "vy.warning",
        "failed": "vy.error",
        "skipped": "vy.muted",
    }.get(status, "vy.warning")


def _severity_style(severity: str) -> str:
    return {
        "error": "vy.error",
        "warning": "vy.warning",
        "info": "vy.info",
    }.get(severity, "vy.warning")


def _rule_style(style: str) -> str:
    return {
        "vy.error": "vy.error.rule",
        "vy.fix": "vy.fix.rule",
        "vy.info": "vy.info.rule",
        "vy.warning": "vy.warning.rule",
    }.get(style, "bold")


def _message_style(style: str) -> str:
    return {
        "vy.error": "vy.error.message",
    }.get(style, style)


def write_json_report(path: Path, report: RunReport) -> None:
    path.write_text(json.dumps(report.to_json_obj(), indent=2, sort_keys=True) + "\n")
