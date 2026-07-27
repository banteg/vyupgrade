from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .models import (
    Config,
    FileReport,
    ValidationDecision,
    ValidationIssue,
    ValidationIssueCode,
)


def decide_run_validation(
    reports: Iterable[FileReport], config: Config
) -> ValidationDecision:
    report_list = tuple(reports)
    reports_by_path = {report.path.resolve(): report for report in report_list}
    decisions: list[ValidationDecision] = []
    for report in report_list:
        if report.validation_mode != "direct":
            continue
        decision = decide_file_validation(report, config)
        report.validation_decision = decision
        decisions.append(decision)
    for report in report_list:
        if report.validation_mode != "consumer-roots":
            continue
        decision = _decide_consumer_root_validation(report, reports_by_path)
        report.validation_decision = decision
        decisions.append(decision)

    blockers = tuple(issue for decision in decisions for issue in decision.blockers)
    waivers = tuple(issue for decision in decisions for issue in decision.waivers)
    if blockers:
        return ValidationDecision("blocked", False, blockers, waivers)
    if waivers:
        return ValidationDecision("waived", True, (), waivers)
    if any(decision.status != "not-required" for decision in decisions):
        return ValidationDecision("passed", True)
    return ValidationDecision()


def _decide_consumer_root_validation(
    report: FileReport,
    reports_by_path: dict[Path, FileReport],
) -> ValidationDecision:
    consumer_paths = tuple(path.resolve() for path in report.consumer_roots)
    missing = tuple(path for path in consumer_paths if path not in reports_by_path)
    if not consumer_paths or missing:
        detail = (
            "no consumer roots were resolved"
            if not consumer_paths
            else "consumer root reports are unavailable: " + ", ".join(str(path) for path in missing)
        )
        issue = ValidationIssue(
            "consumer_roots_unavailable",
            f"dependency validation cannot be attributed because {detail}",
            report.path,
        )
        return ValidationDecision("blocked", False, (issue,))

    consumers = tuple(reports_by_path[path] for path in consumer_paths)
    report.source_compile = _aggregate_compile_status(
        consumer.source_compile for consumer in consumers
    )
    report.target_compile = _aggregate_compile_status(
        consumer.target_compile for consumer in consumers
    )
    compilers = {consumer.source_compiler for consumer in consumers}
    report.source_compiler = compilers.pop() if len(compilers) == 1 else None
    report.source_error = None
    report.target_error = None
    report.source_error_type = None
    report.target_error_type = None
    report.source_attestation = None
    report.target_attestation = None
    report.source_unavailable_artifacts.clear()
    report.target_unavailable_artifacts.clear()
    report.source_unavailable_formats.clear()
    report.target_unavailable_formats.clear()
    report.abi_equal = None
    report.method_ids_equal = None
    report.storage_layout_equal = None
    report.abi_diff.clear()
    report.method_id_diff.clear()
    report.storage_layout_diff.clear()

    decisions = tuple(consumer.validation_decision for consumer in consumers)
    if any(decision.status == "blocked" for decision in decisions):
        return ValidationDecision("blocked", False)
    if any(decision.status == "waived" for decision in decisions):
        return ValidationDecision("waived", True)
    if any(decision.status == "passed" for decision in decisions):
        return ValidationDecision("passed", True)
    return ValidationDecision()


def _aggregate_compile_status(statuses: Iterable[str]) -> str:
    status_set = set(statuses)
    for status in ("failed", "degraded", "passed"):
        if status in status_set:
            return status
    return "skipped"


def decide_file_validation(report: FileReport, config: Config) -> ValidationDecision:
    if report.source_compile == "skipped" and report.target_compile == "skipped":
        return ValidationDecision()

    blockers: list[ValidationIssue] = []
    waivers: list[ValidationIssue] = []

    def require(
        code: ValidationIssueCode,
        message: str,
        *,
        allowed: bool = False,
        waiver: str | None = None,
    ) -> None:
        issue = ValidationIssue(code, message, report.path, waiver if allowed else None)
        (waivers if allowed else blockers).append(issue)

    if report.target_compile != "passed":
        require("target_compile_failed", "target compilation did not pass")
    elif report.target_unavailable_artifacts:
        require(
            "target_artifacts_unavailable",
            "target compiler did not produce required artifacts: "
            + ", ".join(report.target_unavailable_artifacts),
        )

    # Interfaces have no deployable source artifacts to compare. Their safety
    # boundary is successful target compilation through an import harness.
    if report.path.suffix == ".vyi":
        return _decision(blockers, waivers)

    source_validated = report.source_compile in {"passed", "degraded"}
    if not source_validated:
        require(
            "source_compile_failed",
            "source compilation did not pass",
            allowed=config.allow_unvalidated_source,
            waiver="--allow-unvalidated-source",
        )
    elif report.source_unavailable_artifacts:
        require(
            "source_artifacts_unavailable",
            "source compiler did not produce required artifacts: "
            + ", ".join(report.source_unavailable_artifacts),
            allowed=config.allow_unvalidated_source,
            waiver="--allow-unvalidated-source",
        )

    comparisons = (
        (
            "abi_changed",
            "ABI changed after migration",
            report.abi_equal,
            config.allow_abi_change,
            "--allow-abi-change",
        ),
        (
            "method_identifiers_changed",
            "method identifiers changed after migration",
            report.method_ids_equal,
            config.allow_method_id_change,
            "--allow-method-id-change",
        ),
        (
            "storage_layout_changed",
            "storage layout changed after migration",
            report.storage_layout_equal,
            config.allow_storage_layout_change,
            "--allow-storage-layout-change",
        ),
    )
    for code, message, equal, allowed, waiver in comparisons:
        if equal is False:
            require(code, message, allowed=allowed, waiver=waiver)

    if (
        source_validated
        and not report.source_unavailable_artifacts
        and report.target_compile == "passed"
        and not report.target_unavailable_artifacts
        and any(equal is None for _, _, equal, _, _ in comparisons)
    ):
        require(
            "artifact_comparison_unavailable",
            "one or more artifact comparisons were unavailable",
            allowed=config.allow_unvalidated_source,
            waiver="--allow-unvalidated-source",
        )

    return _decision(blockers, waivers)


def validation_exit_code(decision: ValidationDecision) -> int | None:
    if decision.status != "blocked":
        return None
    codes = {issue.code for issue in decision.blockers}
    if codes & {"target_compile_failed", "target_artifacts_unavailable"}:
        return 2
    if codes & {
        "consumer_roots_unavailable",
        "source_compile_failed",
        "source_artifacts_unavailable",
        "artifact_comparison_unavailable",
    }:
        return 3
    return 7


def _decision(
    blockers: list[ValidationIssue], waivers: list[ValidationIssue]
) -> ValidationDecision:
    if blockers:
        return ValidationDecision("blocked", False, tuple(blockers), tuple(waivers))
    if waivers:
        return ValidationDecision("waived", True, (), tuple(waivers))
    return ValidationDecision("passed", True)
