from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


FailureOrigin = Literal[
    "compiler",
    "compiler-internal",
    "environment",
    "launch",
    "timeout",
    "adapter",
]
TargetFailureOrigin = Literal[
    "fixed-target-rewrite",
    "fixed-target-dependency",
    "compiler-internal",
    "environment",
    "launch",
    "timeout",
    "adapter",
]
CompilerAuthority = Literal[
    "project-lock",
    "project-manifest",
    "source-exact",
    "source-range",
    "explicit-executable",
    "fixed-target",
    "default",
]
CompletionStatus = Literal[
    "not-started",
    "completed",
    "timed-out",
    "signaled",
    "adapter-failed",
]
Severity = Literal["info", "warning", "error"]
ValidationDecisionStatus = Literal["not-required", "passed", "waived", "blocked"]
ValidationIssueCode = Literal[
    "target_compile_failed",
    "target_artifacts_unavailable",
    "source_compile_failed",
    "source_artifacts_unavailable",
    "artifact_comparison_unavailable",
    "abi_changed",
    "method_identifiers_changed",
    "storage_layout_changed",
]
REPORT_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class Fix:
    rule: str
    line: int
    message: str
    before: str
    after: str


@dataclass(frozen=True)
class Diagnostic:
    rule: str
    line: int
    message: str
    severity: Severity = "warning"


@dataclass(frozen=True)
class GeneratedFile:
    path: Path
    source: str
    fix: Fix


@dataclass
class RewriteResult:
    source: str
    fixes: list[Fix]
    diagnostics: list[Diagnostic]
    generated_files: list[GeneratedFile] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationIssue:
    code: ValidationIssueCode
    message: str
    path: Path
    waiver: str | None = None

    def to_json_obj(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "path": str(self.path),
            "waiver": self.waiver,
        }


@dataclass(frozen=True)
class ValidationDecision:
    status: ValidationDecisionStatus = "not-required"
    write_allowed: bool = True
    blockers: tuple[ValidationIssue, ...] = ()
    waivers: tuple[ValidationIssue, ...] = ()

    def to_json_obj(self) -> dict[str, object]:
        return {
            "status": self.status,
            "write_allowed": self.write_allowed,
            "blockers": [issue.to_json_obj() for issue in self.blockers],
            "waivers": [issue.to_json_obj() for issue in self.waivers],
        }


@dataclass(frozen=True)
class CompilerOutput:
    stdout: str
    stderr: str

    def to_json_obj(self) -> dict[str, str]:
        return {"stdout": self.stdout, "stderr": self.stderr}


@dataclass(frozen=True)
class ContentIdentity:
    path: str
    sha256: str

    def to_json_obj(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class CompilerDeclaration:
    kind: Literal["project", "source-pragma", "target-version", "explicit-executable"]
    value: str
    path: str | None = None

    def to_json_obj(self) -> dict[str, str | None]:
        return {"kind": self.kind, "value": self.value, "path": self.path}


@dataclass(frozen=True)
class DeclaredSpec:
    snapshot_sha256: str
    sources: tuple[ContentIdentity, ...]
    compiler_declarations: tuple[CompilerDeclaration, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "snapshot": {"sha256": self.snapshot_sha256},
            "sources": [source.to_json_obj() for source in self.sources],
            "compiler_declarations": [
                declaration.to_json_obj() for declaration in self.compiler_declarations
            ],
        }


@dataclass(frozen=True)
class ResolvedCompiler:
    version: str
    executable: ContentIdentity
    artifact: ContentIdentity

    def to_json_obj(self) -> dict[str, object]:
        return {
            "version": self.version,
            "executable": self.executable.to_json_obj(),
            "artifact": self.artifact.to_json_obj(),
        }


@dataclass(frozen=True)
class ResolvedPackage:
    name: str
    version: str
    source: str | None
    artifact_sha256: str

    def to_json_obj(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass(frozen=True)
class DependencyContext:
    mode: Literal["isolated", "project"]
    project_root: str | None = None
    manifest: ContentIdentity | None = None
    lockfile: ContentIdentity | None = None
    python_constraint: str | None = None
    declared_sources: tuple[ContentIdentity, ...] = ()
    resolved_packages: tuple[ResolvedPackage, ...] = ()

    def to_json_obj(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "project_root": self.project_root,
            "manifest": self.manifest.to_json_obj() if self.manifest is not None else None,
            "lockfile": self.lockfile.to_json_obj() if self.lockfile is not None else None,
            "python_constraint": self.python_constraint,
            "declared_sources": [source.to_json_obj() for source in self.declared_sources],
            "resolved_packages": [package.to_json_obj() for package in self.resolved_packages],
        }


@dataclass(frozen=True)
class ExitStatus:
    code: int | None
    signal: int | None = None

    def to_json_obj(self) -> dict[str, int | None]:
        return {"code": self.code, "signal": self.signal}


@dataclass(frozen=True)
class CompileAttempt:
    sequence: int
    source: ContentIdentity
    compiler_started: bool
    completion_status: CompletionStatus
    exit_status: ExitStatus
    failure_origin: FailureOrigin | TargetFailureOrigin | None

    def to_json_obj(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "source": self.source.to_json_obj(),
            "compiler_started": self.compiler_started,
            "completion_status": self.completion_status,
            "exit_status": self.exit_status.to_json_obj(),
            "failure_origin": self.failure_origin,
        }


@dataclass(frozen=True)
class ValidationAttestation:
    declared_spec: DeclaredSpec
    authority_rule: CompilerAuthority
    resolved_compiler: ResolvedCompiler | None
    dependency_context: DependencyContext
    compiler_started: bool
    completion_status: CompletionStatus
    exit_status: ExitStatus
    validated_source_set: tuple[ContentIdentity, ...]
    attempt_sequence: tuple[CompileAttempt, ...]
    failure_origin: FailureOrigin | TargetFailureOrigin | None
    compiler_output: CompilerOutput | None

    def to_json_obj(self) -> dict[str, object]:
        return {
            "declared_spec": self.declared_spec.to_json_obj(),
            "authority_rule": self.authority_rule,
            "resolved_compiler": (
                self.resolved_compiler.to_json_obj() if self.resolved_compiler is not None else None
            ),
            "dependency_context": self.dependency_context.to_json_obj(),
            "compiler_started": self.compiler_started,
            "completion_status": self.completion_status,
            "exit_status": self.exit_status.to_json_obj(),
            "validated_source_set": [source.to_json_obj() for source in self.validated_source_set],
            "attempt_sequence": [attempt.to_json_obj() for attempt in self.attempt_sequence],
            "failure_origin": self.failure_origin,
            "compiler_output": (
                self.compiler_output.to_json_obj() if self.compiler_output is not None else None
            ),
        }


@dataclass
class FileReport:
    path: Path
    role: str = "project"
    changed: bool = False
    fixes: list[Fix] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    source_version: str | None = None
    source_compiler: str | None = None
    source_compile: str = "skipped"
    target_compile: str = "skipped"
    source_error: str | None = None
    source_attestation: ValidationAttestation | None = None
    target_attestation: ValidationAttestation | None = None
    target_error: str | None = None
    abi_equal: bool | None = None
    method_ids_equal: bool | None = None
    storage_layout_equal: bool | None = None
    abi_diff: list[str] = field(default_factory=list)
    method_id_diff: list[str] = field(default_factory=list)
    storage_layout_diff: list[str] = field(default_factory=list)
    source_unavailable_artifacts: list[str] = field(default_factory=list)
    target_unavailable_artifacts: list[str] = field(default_factory=list)
    source_unavailable_formats: list[str] = field(default_factory=list)
    target_unavailable_formats: list[str] = field(default_factory=list)
    validation_decision: ValidationDecision = field(default_factory=ValidationDecision)
    original_sha256: str | None = None
    candidate_sha256: str | None = None
    final_sha256: str | None = None
    final_matches_candidate: bool | None = None


@dataclass
class ClosureReport:
    requested: bool = False
    dependencies: tuple[str, ...] = ()
    output_dir: str | None = None
    output_status: str = "skipped"
    output_error: str | None = None
    archive: str | None = None
    archive_status: str = "skipped"
    archive_error: str | None = None

    def to_json_obj(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "dependencies": list(self.dependencies),
            "output_dir": self.output_dir,
            "output_status": self.output_status,
            "output_error": self.output_error,
            "archive": self.archive,
            "archive_status": self.archive_status,
            "archive_error": self.archive_error,
        }


@dataclass(frozen=True)
class Config:
    paths: tuple[Path, ...]
    target_version: str = "0.4.3"
    source_version: str | None = None
    write: bool = False
    check: bool = False
    diff: bool = False
    report_json: Path | None = None
    select: frozenset[str] = frozenset()
    ignore: frozenset[str] = frozenset()
    aggressive: bool = False
    test_command: str | None = None
    source_vyper: str | None = None
    target_vyper: str | None = None
    source_python: str | None = None
    target_python: str | None = None
    compiler_search_paths: tuple[Path, ...] = ()
    enable_decimals: bool = False
    split_interfaces: bool = False
    include_dependencies: bool = False
    closure_output: Path | None = None
    closure_archive: Path | None = None
    format: str = "none"
    allow_unvalidated_source: bool = False
    allow_abi_change: bool = False
    allow_method_id_change: bool = False
    allow_storage_layout_change: bool = False
    source_ast: dict[str, Any] | None = None


@dataclass
class RunReport:
    source_version: str | None
    target_version: str
    files: list[FileReport]
    write_requested: bool = False
    wrote_changes: bool = False
    write_status: str = "skipped"
    write_output: str | None = None
    validation_decision: ValidationDecision = field(default_factory=ValidationDecision)
    formatter_command: str | None = None
    formatter_status: str = "skipped"
    formatter_output: str | None = None
    test_command: str | None = None
    test_status: str = "skipped"
    test_output: str | None = None
    closure: ClosureReport | None = None

    @property
    def changed_count(self) -> int:
        return sum(1 for file in self.files if file.changed)

    @property
    def fix_count(self) -> int:
        return sum(len(file.fixes) for file in self.files)

    @property
    def diagnostic_count(self) -> int:
        return sum(len(file.diagnostics) for file in self.files)

    def to_json_obj(self) -> dict[str, Any]:
        from . import __version__

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "producer": {"name": "vyupgrade", "version": __version__},
            "source_version": self.source_version,
            "target_version": self.target_version,
            "write_requested": self.write_requested,
            "wrote_changes": self.wrote_changes,
            "write_status": self.write_status,
            "write_output": self.write_output,
            "validation_decision": self.validation_decision.to_json_obj(),
            "files": [
                {
                    "role": file.role,
                    "path": str(file.path),
                    "changed": file.changed,
                    "original_sha256": file.original_sha256,
                    "candidate_sha256": file.candidate_sha256,
                    "final_sha256": file.final_sha256,
                    "final_matches_candidate": file.final_matches_candidate,
                    "fixes": [fix.__dict__ for fix in file.fixes],
                    "diagnostics": [diag.__dict__ for diag in file.diagnostics],
                    "validation": {
                        "source_version": file.source_version,
                        "source_compiler": file.source_compiler,
                        "source_compile": file.source_compile,
                        "source_attestation": (
                            file.source_attestation.to_json_obj()
                            if file.source_attestation is not None
                            else None
                        ),
                        "target_attestation": (
                            file.target_attestation.to_json_obj()
                            if file.target_attestation is not None
                            else None
                        ),
                        "target_compile": file.target_compile,
                        "abi_equal": file.abi_equal,
                        "method_ids_equal": file.method_ids_equal,
                        "storage_layout_equal": file.storage_layout_equal,
                        "abi_diff": file.abi_diff,
                        "method_id_diff": file.method_id_diff,
                        "storage_layout_diff": file.storage_layout_diff,
                        "source_unavailable_artifacts": file.source_unavailable_artifacts,
                        "target_unavailable_artifacts": file.target_unavailable_artifacts,
                        "source_unavailable_formats": file.source_unavailable_formats,
                        "target_unavailable_formats": file.target_unavailable_formats,
                        "decision": file.validation_decision.to_json_obj(),
                    },
                    "source_error": file.source_error,
                    "target_error": file.target_error,
                }
                for file in self.files
            ],
            "formatter_command": self.formatter_command,
            "formatter_status": self.formatter_status,
            "formatter_output": self.formatter_output,
            "test_command": self.test_command,
            "test_status": self.test_status,
            "test_output": self.test_output,
            "closure": self.closure.to_json_obj() if self.closure else None,
        }
