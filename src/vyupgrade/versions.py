from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .source import code_mask, line_starts_in_code


PRAGMA_RE = re.compile(r"^\s*#\s*(?:@version|pragma\s+version)\s+(.+?)\s*$", re.MULTILINE)
VERSION_RE = re.compile(r"0\.(?:1|2|3|4|5)\.\d+(?:(?:a|b|rc)\d+)?")
# PyPI has no final 0.1.0 release; these are the installable Vyper releases before 0.2.1.
LEGACY_PRERELEASE_VERSIONS = tuple(f"0.1.0b{number}" for number in range(1, 18))
LEGACY_PRERELEASES = frozenset(Version(version) for version in LEGACY_PRERELEASE_VERSIONS)
ALPHA_RELEASE_VERSIONS = ("0.5.0a1", "0.5.0a2", "0.5.0a3")
ALPHA_RELEASES = frozenset(Version(version) for version in ALPHA_RELEASE_VERSIONS)


VyperVersion = Version


KNOWN_VERSIONS = tuple(
    [
        *(Version(version) for version in LEGACY_PRERELEASE_VERSIONS),
        *(
            Version(f"0.{minor}.{patch}")
            for minor, last_patch in ((2, 16), (3, 10), (4, 3))
            for patch in range(1 if minor == 2 else 0, last_patch + 1)
        ),
        *(Version(version) for version in ALPHA_RELEASE_VERSIONS),
    ]
)

SUPPORTED_RELEASE_VERSIONS = (
    frozenset(
        Version(f"0.{minor}.{patch}")
        for minor, last_patch in ((2, 16), (3, 10), (4, 3))
        for patch in range(1 if minor == 2 else 0, last_patch + 1)
    )
    | ALPHA_RELEASES
)


@dataclass(frozen=True)
class MigrationContext:
    source_spec: str | None
    target_spec: str
    source_floor: VyperVersion | None
    target_version: VyperVersion

    @classmethod
    def from_specs(cls, source_spec: str | None, target_spec: str) -> MigrationContext:
        target = parse_version(compiler_version_for_spec(target_spec)) or parse_version(target_spec)
        if target is None:
            target = Version("0.4.3")
        return cls(
            source_spec=source_spec,
            target_spec=target_spec,
            source_floor=minimum_satisfying_version(source_spec),
            target_version=target,
        )

    def target_at_least(self, version: str | VyperVersion) -> bool:
        return self.target_version >= ensure_version(version)

    def crosses(self, version: str | VyperVersion) -> bool:
        introduced = ensure_version(version)
        if self.target_version < introduced:
            return False
        if self.source_floor is None:
            return True
        return self.source_floor < introduced

    def source_spec_unsupported(self) -> bool:
        return self.source_spec is not None and not known_versions_satisfying(self.source_spec)

    def source_newer_than_target(self) -> bool:
        return self.source_floor is not None and self.source_floor > self.target_version

    def source_can_migrate_to_target(self) -> bool:
        return not self.source_spec_unsupported() and not self.source_newer_than_target()


def infer_pragma(source: str) -> str | None:
    mask = code_mask(source)
    for match in PRAGMA_RE.finditer(source):
        if line_starts_in_code(source, mask, match.start()):
            return match.group(1).strip()
    return None


def parse_version(raw: str | None) -> VyperVersion | None:
    if raw is None:
        return None
    match = VERSION_RE.search(raw)
    if match is None:
        return None
    try:
        return Version(match.group(0))
    except InvalidVersion:
        return None


def ensure_version(raw: str | VyperVersion) -> VyperVersion:
    if isinstance(raw, VyperVersion):
        return raw
    version = parse_version(raw)
    if version is None:
        raise ValueError(f"unsupported Vyper version: {raw!r}")
    return version


def minimum_satisfying_version(spec: str | None) -> VyperVersion | None:
    if not spec:
        return None
    versions = known_versions_satisfying(spec)
    if versions:
        return versions[0]
    return parse_version(spec)


def compiler_version_for_spec(spec: str | None) -> str | None:
    versions = known_versions_satisfying(spec)
    if versions:
        return str(versions[0] if _has_lower_bound(spec or "") else versions[-1])
    version = parse_version(spec)
    return str(version) if version else None


def compiler_version_for_source_validation(
    spec: str | None, target_spec: str, _source: str
) -> str | None:
    """Select the newest known satisfying release that is no newer than the target."""
    versions = known_versions_satisfying(spec)
    if not versions:
        return compiler_version_for_spec(spec)

    target = parse_version(compiler_version_for_spec(target_spec)) or parse_version(target_spec)
    candidates = tuple(version for version in versions if target is None or version <= target)
    return str(candidates[-1] if candidates else versions[-1])


def default_evm_version_for_spec(spec: str | None) -> str | None:
    compiler_version = compiler_version_for_spec(spec)
    version = parse_version(compiler_version)
    return default_evm_version(version)


def default_evm_version(version: VyperVersion | None) -> str | None:
    if version is None:
        return None
    if version < Version("0.2.12"):
        return "istanbul"
    if version < Version("0.3.7"):
        return "berlin"
    if version < Version("0.3.8"):
        return "paris"
    if version < Version("0.4.0"):
        return "shanghai"
    if version < Version("0.4.3"):
        return "cancun"
    return "prague"


def known_versions_satisfying(spec: str | None) -> tuple[VyperVersion, ...]:
    if not spec:
        return ()
    specifiers = _specifier_set(spec)
    if specifiers is not None:
        return tuple(
            version for version in KNOWN_VERSIONS if specifiers.contains(version, prereleases=True)
        )
    clauses = _parse_clauses(spec)
    if not clauses:
        version = parse_version(spec)
        return (version,) if version else ()
    return tuple(
        version
        for version in KNOWN_VERSIONS
        if all(_satisfies(version, op, bound) for op, bound in clauses)
    )


def _specifier_set(spec: str) -> SpecifierSet | None:
    # Match the compiler's own pragma check (vyper.ast.pre_parser): a bare
    # version pins exactly, an npm-style caret becomes a PEP 440
    # compatible-release clause, and candidates are tested with
    # SpecifierSet.contains(..., prereleases=True). PEP 440 ordered exclusive
    # comparisons still reject prereleases of their bound, so "<0.5.0" never
    # admits "0.5.0a3" even though it sorts lower.
    normalized = spec.strip()
    if re.match(r"[v0-9]", normalized):
        normalized = f"=={normalized}"
    normalized = re.sub(r"^\^", "~=", normalized)
    try:
        return SpecifierSet(normalized)
    except InvalidSpecifier:
        return None


def is_supported_source_version(version: str | None) -> bool:
    parsed = parse_version(version)
    return parsed in LEGACY_PRERELEASES or parsed in SUPPORTED_RELEASE_VERSIONS


def legacy_prerelease_version(spec: str | None) -> str | None:
    version = parse_version(spec)
    return str(version) if version in LEGACY_PRERELEASES else None


def _parse_clauses(spec: str) -> list[tuple[str, VyperVersion]]:
    clauses: list[tuple[str, VyperVersion]] = []
    for match in re.finditer(
        r"(?P<op>\^|==|!=|<=|>=|<|>|=)?\s*(?P<version>0\.(?:1|2|3|4|5)\.\d+(?:(?:a|b|rc)\d+)?)",
        spec,
    ):
        op = match.group("op") or "=="
        version = ensure_version(match.group("version"))
        if op == "^":
            clauses.append((">=", version))
            clauses.append(("<", _caret_upper_bound(version)))
        elif op == "=":
            clauses.append(("==", version))
        else:
            clauses.append((op, version))
    return clauses


def _has_lower_bound(spec: str) -> bool:
    specifiers = _specifier_set(spec)
    if specifiers is not None:
        return any(
            _specifier_is_lower_bound(specifier.operator, specifier.version)
            for specifier in specifiers
        )
    return any(op in {">=", ">", "=="} for op, _version in _parse_clauses(spec))


def _specifier_is_lower_bound(operator: str, version: str) -> bool:
    if operator in {"~=", ">=", ">"}:
        return True
    if operator in {"==", "==="}:
        return "*" not in version
    return False


def _caret_upper_bound(version: VyperVersion) -> VyperVersion:
    # Vyper versions in this tool are all 0.x. The useful historical caret
    # range is therefore the current minor line.
    return Version(f"{version.major}.{version.minor + 1}.0")


def _satisfies(version: VyperVersion, op: str, bound: VyperVersion) -> bool:
    if op == "==":
        return version == bound
    if op == "!=":
        return version != bound
    if op == ">=":
        return version >= bound
    if op == ">":
        return version > bound
    if op == "<=":
        return version <= bound
    if op == "<":
        return version < bound
    raise ValueError(f"unsupported version operator: {op}")
