from __future__ import annotations

from importlib.metadata import version

from vyupgrade import __version__
from vyupgrade.versions import (
    LEGACY_PRERELEASE_VERSIONS,
    MigrationContext,
    VyperVersion,
    compiler_version_for_source_validation,
    compiler_version_for_spec,
    default_evm_version_for_spec,
    infer_pragma,
    is_supported_source_version,
    known_versions_satisfying,
    minimum_satisfying_version,
)


def test_package_version_is_exposed() -> None:
    assert __version__ == version("vyupgrade")


def test_known_versions_cover_full_supported_range() -> None:
    assert all(is_supported_source_version(version) for version in LEGACY_PRERELEASE_VERSIONS)
    assert is_supported_source_version("0.2.1")
    assert is_supported_source_version("0.4.3")
    assert is_supported_source_version("0.5.0a1")
    assert is_supported_source_version("0.5.0a2")
    assert is_supported_source_version("0.5.0a3")
    assert not is_supported_source_version("0.1.0")
    assert not is_supported_source_version("0.1.0b18")
    assert not is_supported_source_version("0.1.0b99")
    assert not is_supported_source_version("0.2.0")
    assert not is_supported_source_version("0.4.4")
    assert not is_supported_source_version("0.5.0")
    assert VyperVersion("0.1.0b1") in known_versions_satisfying(">=0.1.0b1,<0.2.1")
    assert VyperVersion("0.2.1") in known_versions_satisfying(">=0.2.1,<0.2.3")
    assert VyperVersion("0.4.3") in known_versions_satisfying(">=0.4.0")
    assert VyperVersion("0.5.0a2") in known_versions_satisfying(">=0.5.0a1,<0.6.0")
    assert VyperVersion("0.5.0a3") in known_versions_satisfying(">=0.5.0a1,<0.6.0")
    assert VyperVersion("0.5.0a3") in known_versions_satisfying("^0.5.0a1")
    # PEP 440 exclusive ordered bounds reject prereleases of the bound itself,
    # exactly like the compiler's own pragma check does.
    assert known_versions_satisfying(">=0.5.0a1,<0.5.0") == ()


def test_version_specs_pick_lowest_satisfying_source_floor() -> None:
    for legacy_version in LEGACY_PRERELEASE_VERSIONS:
        assert compiler_version_for_spec(legacy_version) == legacy_version
    assert minimum_satisfying_version("^0.3.10") == VyperVersion("0.3.10")
    assert minimum_satisfying_version(">=0.1.0b4,<0.2.1") == VyperVersion("0.1.0b4")
    assert minimum_satisfying_version(">=0.3.4,<0.4.0") == VyperVersion("0.3.4")
    assert minimum_satisfying_version(">0.3.10") == VyperVersion("0.4.0")
    assert compiler_version_for_spec("<=0.3.10") == "0.3.10"
    assert compiler_version_for_spec(">=0.5.0a1,<0.6.0") == "0.5.0a1"
    assert compiler_version_for_spec("<=0.5.0a2") == "0.5.0a2"
    assert compiler_version_for_spec("<=0.5.0a3") == "0.5.0a3"
    assert compiler_version_for_spec("~=0.4") == "0.4.0"
    assert compiler_version_for_spec("~=0.4.2") == "0.4.2"
    assert compiler_version_for_spec("==0.4.*") == "0.4.3"


def test_infer_pragma_ignores_string_and_docstring_contents() -> None:
    source = '''"""
# @version 0.4.3
"""
VERSION: constant(String[32]) = "# pragma version 0.4.2"
# @version 0.3.10
'''

    assert infer_pragma(source) == "0.3.10"
    assert infer_pragma('''"""\n# @version 0.4.3\n"""\n''') is None


def test_source_validation_compiler_uses_newest_target_bounded_version() -> None:
    assert minimum_satisfying_version(">0.3.10") == VyperVersion("0.4.0")
    assert compiler_version_for_source_validation(">0.3.10", "0.4.3", "") == "0.4.3"
    assert compiler_version_for_source_validation(">=0.4.0", "0.4.3", "") == "0.4.3"
    assert compiler_version_for_source_validation(">=0.4.0", "0.5.0a2", "") == "0.5.0a2"


def test_prerelease_targets_never_satisfy_bounded_stable_pragmas() -> None:
    assert known_versions_satisfying("^0.4.2") == (VyperVersion("0.4.2"), VyperVersion("0.4.3"))
    assert compiler_version_for_spec("<0.5.0") == "0.4.3"
    source = "# pragma version ^0.4.2\n\n@external\ndef value() -> uint256:\n    return 1\n"
    assert compiler_version_for_source_validation("^0.4.2", "0.5.0a3", source) == "0.4.3"
    # Legacy caret pragmas that name a prerelease still resolve to prereleases.
    assert minimum_satisfying_version("^0.1.0b14") == VyperVersion("0.1.0b14")
    assert VyperVersion("0.1.0b17") in known_versions_satisfying("^0.1.0b14")


def test_migration_context_tracks_patch_level_crossings() -> None:
    older = MigrationContext.from_specs("0.4.1", "0.4.3")
    current = MigrationContext.from_specs("0.4.2", "0.4.3")
    below_target = MigrationContext.from_specs("0.3.10", "0.4.1")

    assert older.crosses("0.4.2")
    assert not current.crosses("0.4.2")
    assert not below_target.crosses("0.4.2")
    assert MigrationContext.from_specs("0.1.0b17", "0.2.1").crosses("0.2.1")


def test_default_evm_versions_track_vyper_release_defaults() -> None:
    assert default_evm_version_for_spec("0.1.0b16") == "istanbul"
    assert default_evm_version_for_spec("0.2.4") == "istanbul"
    assert default_evm_version_for_spec("0.2.12") == "berlin"
    assert default_evm_version_for_spec("0.3.7") == "paris"
    assert default_evm_version_for_spec("0.3.10") == "shanghai"
    assert default_evm_version_for_spec("0.4.2") == "cancun"
    assert default_evm_version_for_spec("0.4.3") == "prague"
