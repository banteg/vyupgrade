from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import zipfile
import sys
from pathlib import Path

import pytest
from uv import find_uv_bin

from vyupgrade.cli import main
from vyupgrade.compiler import (
    CompileResult,
    compare_artifact_details,
    compare_artifacts,
    compile_source_file,
    compile_target_source,
    unavailable_validation_artifacts,
)
from vyupgrade.models import Config


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_real_compiler_nested_module_layout_is_comparable(tmp_path: Path) -> None:
    module = tmp_path / "ledger.vy"
    module.write_text(
        """#pragma version 0.4.3
owner: public(address)

@deploy
def __init__():
    self.owner = msg.sender
""",
        encoding="utf-8",
    )
    contract = tmp_path / "main.vy"
    source = """#pragma version 0.4.3
import ledger

initializes: ledger
exports: ledger.__interface__

@deploy
def __init__():
    ledger.__init__()
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(
        paths=(contract,),
        target_version="0.4.3",
        compiler_search_paths=(tmp_path,),
    )

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    assert layout["storage_layout"] == {
        "ledger": {"owner": {"type": "address", "n_slots": 1, "slot": 0}}
    }


def test_real_compiler_code_only_immutable_layout_is_empty_storage(tmp_path: Path) -> None:
    contract = tmp_path / "immutable_only.vy"
    source = """#pragma version 0.4.3
OWNER: immutable(address)

@deploy
def __init__():
    OWNER = msg.sender

@view
@external
def owner() -> address:
    return OWNER
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    assert layout == {"code_layout": {"OWNER": {"type": "address", "length": 32, "offset": 0}}}


def test_real_legacy_immutable_layout_compares_with_modern_code_only_target(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "legacy_immutable_only.vy"
    legacy_source = """# @version 0.3.10
OWNER: immutable(address)

@external
def __init__():
    OWNER = msg.sender

@view
@external
def owner() -> address:
    return OWNER
"""
    target_source = legacy_source.replace("# @version 0.3.10", "#pragma version 0.4.3").replace(
        "@external\ndef __init__", "@deploy\ndef __init__", 1
    )
    contract.write_text(legacy_source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.3.10")
    target_compile = compile_target_source(contract, target_source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert source_compile.artifacts is not None
    assert source_compile.artifacts["layout"] == {
        "code_layout": {"OWNER": {"type": "address", "length": 32, "offset": 0}},
        "storage_layout": {},
    }


def test_real_legacy_width_inference_matches_modern_layout(tmp_path: Path) -> None:
    contract = tmp_path / "storage_widths.vy"
    legacy_source = """# @version 0.3.10
fixed_values: uint256[3]
dynamic_values: DynArray[uint256, 3]
blob: Bytes[64]
label: String[32]
pools: HashMap[address, uint256]
"""
    target_source = legacy_source.replace("# @version 0.3.10", "#pragma version 0.4.3")
    contract.write_text(legacy_source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.3.10")
    target_compile = compile_target_source(contract, target_source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    storage = layout["storage_layout"]
    assert isinstance(storage, dict)
    assert {
        name: entry["n_slots"] for name, entry in storage.items() if isinstance(entry, dict)
    } == {
        "fixed_values": 3,
        "dynamic_values": 4,
        "blob": 3,
        "label": 2,
        "pools": 1,
    }


def test_real_compiler_nested_array_layouts_are_comparable(tmp_path: Path) -> None:
    contract = tmp_path / "nested_storage_widths.vy"
    source = """#pragma version 0.4.3
from ethereum.ercs import IERC20

tokens: IERC20[3]
dynamic_values: DynArray[uint256, 3][3]
mixed_values: DynArray[DynArray[uint256, 3][3], 3][5]
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    storage = layout["storage_layout"]
    assert isinstance(storage, dict)
    tokens = storage["tokens"]
    dynamic_values = storage["dynamic_values"]
    assert isinstance(tokens, dict)
    assert isinstance(dynamic_values, dict)
    assert str(tokens["type"]).endswith("IERC20.vyi[3]")
    assert dynamic_values["type"] == "DynArray[uint256, 3][3]"
    assert {
        name: entry["n_slots"] for name, entry in storage.items() if isinstance(entry, dict)
    } == {
        "tokens": 3,
        "dynamic_values": 12,
        "mixed_values": 185,
    }


def test_real_legacy_flag_layout_matches_modern_layout(tmp_path: Path) -> None:
    contract = tmp_path / "flag_storage.vy"
    source = """#pragma version 0.4.0
flag Permission:
    READ
    WRITE

permission: Permission
permissions: HashMap[address, Permission]
"""
    target = source.replace("#pragma version 0.4.0", "#pragma version 0.4.3")
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.0")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert source_compile.artifacts is not None
    assert target_compile.artifacts is not None
    source_layout = source_compile.artifacts["layout"]
    target_layout = target_compile.artifacts["layout"]
    assert isinstance(source_layout, dict)
    assert isinstance(target_layout, dict)
    source_storage = source_layout["storage_layout"]
    target_storage = target_layout["storage_layout"]
    assert isinstance(source_storage, dict)
    assert isinstance(target_storage, dict)
    assert source_storage["permission"] == {
        "slot": 0,
        "type": "flag Permission('READ','WRITE')",
        "n_slots": 1,
    }
    assert target_storage["permission"] == {
        "slot": 0,
        "type": "Permission",
        "n_slots": 1,
    }


def test_real_inline_interface_marker_omission_uses_target_ast(tmp_path: Path) -> None:
    contract = tmp_path / "interface_storage.vy"
    source = """#pragma version 0.4.0
interface Pool:
    def ping() -> uint256: view

pool: Pool
pools: HashMap[address, Pool]
"""
    target = source.replace("#pragma version 0.4.0", "#pragma version 0.4.3")
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.0")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert target_compile.artifacts is not None
    assert target_compile.artifacts["ast"]["ast"]["ast_type"] == "Module"
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)


def test_real_interface_marker_does_not_alias_one_slot_struct(tmp_path: Path) -> None:
    contract = tmp_path / "interface_to_struct.vy"
    source = """#pragma version 0.4.0
interface Pool:
    def ping() -> uint256: view

pool: Pool
pools: HashMap[address, Pool]
"""
    target = """#pragma version 0.4.3
struct Pool:
    value: uint256

pool: Pool
pools: HashMap[address, Pool]
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.0")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert compare_artifacts(source_compile, target_compile) == (True, True, False)
    storage_diff = compare_artifact_details(source_compile, target_compile)[2]
    assert len(storage_diff) == 2
    assert any("changed storage: pool " in line for line in storage_diff)
    assert any("changed storage: pools " in line for line in storage_diff)


def test_real_root_interface_does_not_prove_imported_struct_annotation(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.vy"
    child.write_text(
        """#pragma version 0.4.3
struct Pool:
    value: uint256
""",
        encoding="utf-8",
    )
    contract = tmp_path / "root.vy"
    source = """#pragma version 0.4.0
interface Pool:
    def ping() -> uint256: view

item: Pool
items: HashMap[address, Pool]
"""
    target = """#pragma version 0.4.3
interface Pool:
    def ping() -> uint256: view

import child

item: child.Pool
items: HashMap[address, child.Pool]
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(
        paths=(contract,),
        target_version="0.4.3",
        compiler_search_paths=(tmp_path,),
    )

    source_compile = compile_source_file(contract, config, "0.4.0")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert compare_artifacts(source_compile, target_compile) == (True, True, False)
    storage_diff = compare_artifact_details(source_compile, target_compile)[2]
    assert len(storage_diff) == 2
    assert any("changed storage: item " in line for line in storage_diff)
    assert any("changed storage: items " in line for line in storage_diff)


@pytest.mark.parametrize("struct_name", ["IFoo", "ERC20"])
def test_real_interface_named_struct_keeps_two_slot_width(
    tmp_path: Path,
    struct_name: str,
) -> None:
    contract = tmp_path / f"{struct_name.lower()}_struct.vy"
    source = f"""#pragma version 0.4.3
struct {struct_name}:
    left: uint256
    right: uint256

value: {struct_name}
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    storage = layout["storage_layout"]
    assert isinstance(storage, dict)
    assert storage["value"] == {"slot": 0, "type": struct_name, "n_slots": 2}

    legacy_without_width = CompileResult(
        "passed",
        artifacts={"layout": {"value": {"slot": 0, "type": struct_name}}},
    )
    assert compare_artifacts(legacy_without_width, target_compile) == (
        None,
        None,
        False,
    )


def test_real_ierc20_and_erc20_struct_names_remain_distinct(tmp_path: Path) -> None:
    contract = tmp_path / "token_struct.vy"
    source = """#pragma version 0.4.3
struct IERC20:
    left: uint256
    right: uint256

value: IERC20
"""
    target = source.replace("IERC20", "ERC20")
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, False)
    assert compare_artifact_details(source_compile, target_compile)[2] == [
        "changed storage: value slot 0 IERC20 -> 0 ERC20"
    ]


def test_write_mode_validates_against_target_compiler(tmp_path: Path) -> None:
    contract = tmp_path / "migration_03.vy"
    shutil.copyfile(Path("tests/fixtures/migration_03.vy"), contract)

    report = tmp_path / "report.json"
    code = main(
        [
            str(contract),
            "--write",
            "--allow-unvalidated-source",
            "--report-json",
            str(report),
        ]
    )

    assert code == 0
    rewritten = contract.read_text()
    assert "#pragma version 0.4.3" in rewritten
    assert "staticcall self.token.balanceOf(msg.sender)" in rewritten
    assert "for i: uint256 in range(3):" in rewritten
    data = json.loads(report.read_text())
    assert data["write_requested"] is True
    assert data["wrote_changes"] is True
    assert data["validation_decision"]["status"] == "waived"
    assert data["files"][0]["validation"]["target_compile"] == "passed"


def test_target_validation_uses_rewritten_import_overlay(tmp_path: Path) -> None:
    (tmp_path / "lib.vy").write_text(
        """# pragma version 0.4.0
X: constant(uint256) = 1
""",
        encoding="utf-8",
    )
    (tmp_path / "main.vy").write_text(
        """# pragma version 0.4.0
import lib

@external
def x() -> uint256:
    return lib.X
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(tmp_path), "--check", "--report-json", str(report)])

    assert code == 1
    data = json.loads(report.read_text())
    assert {
        file["path"].rsplit("/", 1)[-1]: file["validation"]["target_compile"]
        for file in data["files"]
    } == {"lib.vy": "passed", "main.vy": "passed"}


def test_repeated_singleton_natspec_fields_compile_after_rewrite(tmp_path: Path) -> None:
    contract = tmp_path / "documented.vy"
    contract.write_text(
        '''# @version 0.3.10
@external
def collect_fees() -> uint256:
    """
    @dev First detail
    @dev Second detail
    @dev Third detail
    @dev Fourth detail
    """
    return 0
''',
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code == 1
    file = json.loads(report.read_text(encoding="utf-8"))["files"][0]
    assert file["validation"]["target_compile"] == "passed"
    assert file["validation"]["decision"]["status"] == "passed"


def test_legacy_erc4626_interface_getter_stub_compiles_but_width_is_unproven(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "ERC4626Mock.vy"
    shutil.copyfile(Path("tests/fixtures/erc4626_interface_getter.vy"), contract)
    report = tmp_path / "report.json"

    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code == 7
    file = json.loads(report.read_text(encoding="utf-8"))["files"][0]
    assert file["validation"]["target_compile"] == "passed"
    assert file["validation"]["decision"]["status"] == "blocked"
    assert file["validation"]["storage_layout_equal"] is False
    assert file["validation"]["storage_layout_diff"] == [
        "changed storage: asset slot 0 ERC20 -> 0 interface IERC20 (n_slots unknown -> 1)"
    ]


def test_alpha_target_validation_uses_alpha_compiler(tmp_path: Path) -> None:
    contract = tmp_path / "sqrt.vy"
    contract.write_text(
        """#pragma version 0.4.3
@external
def f(x: uint256) -> uint256:
    return isqrt(x)
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main(
        [
            str(contract),
            "--check",
            "--target-version",
            "0.5.0a2",
            "--report-json",
            str(report),
        ]
    )

    assert code == 1
    data = json.loads(report.read_text())
    file = data["files"][0]
    assert file["validation"]["target_compile"] == "passed"
    assert any(fix["rule"] == "VY101" for fix in file["fixes"])


def test_alpha_isqrt_rewrite_avoids_existing_math_binding(tmp_path: Path) -> None:
    dependency = tmp_path / "dep_math.vy"
    dependency.write_text(
        """# pragma version 0.4.1

@internal
@pure
def _identity(x: uint256) -> uint256:
    return x
""",
        encoding="utf-8",
    )
    contract = tmp_path / "Repro.vy"
    contract.write_text(
        '''# pragma version 0.4.1
"""
@title Minimal VY101 import collision
"""

from . import dep_math as math

@external
@pure
def sqrt_plus_zero(x: uint256) -> uint256:
    return isqrt(x) + math._identity(0)
''',
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main(
        [
            str(contract),
            "--check",
            "--target-version",
            "0.5.0a3",
            "--compiler-search-paths",
            str(tmp_path),
            "--report-json",
            str(report),
        ]
    )

    assert code == 1
    file = json.loads(report.read_text())["files"][0]
    assert file["validation"]["target_compile"] == "passed"
    assert file["validation"]["decision"]["status"] == "passed"
    assert any(
        fix["after"] == "builtin_math.isqrt" for fix in file["fixes"] if fix["rule"] == "VY101"
    )


def test_standalone_interface_receives_target_validation(tmp_path: Path) -> None:
    interface = tmp_path / "IToken.vyi"
    interface.write_text(
        """# @version 0.3.10
@view
@external
def balanceOf(owner: address) -> uint256: ...
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(interface), "--check", "--report-json", str(report)])

    assert code == 1
    file = json.loads(report.read_text())["files"][0]
    assert file["validation"]["source_compile"] == "skipped"
    assert file["validation"]["source_unavailable_artifacts"] == []
    assert file["validation"]["target_compile"] == "passed"
    assert file["validation"]["decision"]["status"] == "passed"


def test_invalid_standalone_interface_is_not_written(tmp_path: Path) -> None:
    interface = tmp_path / "IBroken.vyi"
    original = "# @version 0.3.10\n@external\ndef broken(: ...\n"
    interface.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"

    code = main([str(interface), "--write", "--report-json", str(report)])

    assert code == 2
    assert interface.read_text(encoding="utf-8") == original
    file = json.loads(report.read_text())["files"][0]
    assert file["validation"]["target_compile"] == "failed"
    assert file["validation"]["decision"]["status"] == "blocked"


def test_target_validation_does_not_normalize_selected_candidate(tmp_path: Path) -> None:
    contract = tmp_path / "selected.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")

    code = main([str(contract), "--write", "--select", "VY002"])

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original


def test_real_compiler_include_dependencies_upgrades_closure(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project = project_root / "main.vy"
    search_path = tmp_path / "site-packages"
    dependency = search_path / "depkg" / "mod.vy"
    dependency.parent.mkdir(parents=True)
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='project'\nversion='0.1.0'\n")
    (project_root / "depkg").symlink_to(dependency.parent, target_is_directory=True)
    project_source = (
        "# @version 0.3.10\n"
        "from depkg import mod\n"
        "\n"
        "@external\n"
        "def value() -> uint256:\n"
        "    return 1\n"
    )
    dependency_source = "# @version 0.3.10\nVALUE: constant(uint256) = 1\n"
    project.write_text(project_source, encoding="utf-8")
    dependency.write_text(dependency_source, encoding="utf-8")
    report = tmp_path / "report.json"
    second_report = tmp_path / "second-report.json"
    arguments = [
        str(project),
        "--include-dependencies",
        "--compiler-search-paths",
        str(search_path),
        "--report-json",
    ]

    code = main([*arguments, str(report)])
    second_code = main([*arguments, str(second_report)])

    assert code == 0
    assert second_code == 0
    assert project.read_text(encoding="utf-8") == project_source
    assert dependency.read_text(encoding="utf-8") == dependency_source
    data = json.loads(report.read_text())
    second_data = json.loads(second_report.read_text())
    assert data == second_data
    files = {Path(file["path"]): file for file in data["files"]}
    assert files[project.resolve()]["validation"]["target_compile"] == "passed"
    assert files[dependency.resolve()]["validation"]["target_compile"] == "passed"
    assert files[dependency.resolve()]["role"] == "dependency"


def test_real_fixed_target_dependency_rejection_has_typed_origin(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = project_root / "main.vy"
    search_path = tmp_path / "site-packages"
    dependency = search_path / "depkg" / "mod.vy"
    dependency.parent.mkdir(parents=True)
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='project'\nversion='0.1.0'\n")
    (project_root / "depkg").symlink_to(dependency.parent, target_is_directory=True)
    project.write_text(
        "# @version 0.3.10\nfrom depkg import mod\n",
        encoding="utf-8",
    )
    dependency.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n",
        encoding="utf-8",
    )
    report = tmp_path / "target-origin-report.json"

    code = main(
        [
            str(project),
            "--target-version",
            "0.4.3",
            "--include-dependencies",
            "--compiler-search-paths",
            str(search_path),
            "--select",
            "VY001",
            "--report-json",
            str(report),
        ]
    )

    assert code != 0
    files = {Path(file["path"]): file for file in json.loads(report.read_text())["files"]}
    dependency_report = files[dependency.resolve()]
    assert dependency_report["role"] == "dependency"
    assert dependency_report["validation"]["source_compile"] == "passed"
    assert dependency_report["validation"]["target_compile"] == "failed"
    target_attestation = dependency_report["validation"]["target_attestation"]
    assert target_attestation["authority_rule"] == "fixed-target"
    assert target_attestation["failure_origin"] == "fixed-target-dependency"
    assert target_attestation["compiler_started"] is True
    assert target_attestation["exit_status"] == {"code": 1, "signal": None}
    assert target_attestation["compiler_output"]["stderr"]


def test_real_compiler_closure_output_tree_compiles_standalone(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project = project_root / "main.vy"
    search_path = tmp_path / "site-packages"
    dependency = search_path / "depkg" / "mod.vy"
    dependency.parent.mkdir(parents=True)
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='project'\nversion='0.1.0'\n")
    (project_root / "depkg").symlink_to(dependency.parent, target_is_directory=True)
    project_source = (
        "# @version 0.3.10\n"
        "from depkg import mod\n"
        "\n"
        "@external\n"
        "def value() -> uint256:\n"
        "    return 1\n"
    )
    dependency_source = "# @version 0.3.10\nVALUE: constant(uint256) = 1\n"
    project.write_text(project_source, encoding="utf-8")
    dependency.write_text(dependency_source, encoding="utf-8")
    output = project_root / "output"
    arguments = [
        str(project_root),
        "--include-dependencies",
        "--compiler-search-paths",
        str(search_path),
        "--closure-output",
        str(output),
    ]

    code = main(arguments)
    first_hash = _tree_sha256(output)
    entry = output / "main.vy"
    result = compile_target_source(
        entry,
        entry.read_text(encoding="utf-8"),
        Config(
            paths=(entry,),
            target_version="0.4.3",
            compiler_search_paths=(output,),
        ),
    )
    second_code = main(arguments)

    assert code == 0
    assert second_code == 0
    assert result.status == "passed", result.stderr
    assert project.read_text(encoding="utf-8") == project_source
    assert dependency.read_text(encoding="utf-8") == dependency_source
    assert (output / "depkg" / "mod.vy").is_file()
    assert _tree_sha256(output) == first_hash


def test_real_compiler_closure_archive_round_trips(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = project_root / "main.vy"
    search_path = tmp_path / "site-packages"
    dependency = search_path / "depkg" / "mod.vy"
    dependency.parent.mkdir(parents=True)
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='project'\nversion='0.1.0'\n")
    (project_root / "depkg").symlink_to(dependency.parent, target_is_directory=True)
    project_source = (
        "# @version 0.3.10\n"
        "from depkg import mod\n"
        "\n"
        "@external\n"
        "def value() -> uint256:\n"
        "    return 1\n"
    )
    dependency_source = "# @version 0.3.10\nVALUE: constant(uint256) = 1\n"
    project.write_text(project_source, encoding="utf-8")
    dependency.write_text(dependency_source, encoding="utf-8")
    archive = tmp_path / "out.vyz"
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    arguments = [
        str(project),
        "--include-dependencies",
        "--compiler-search-paths",
        str(search_path),
        "--closure-archive",
        str(archive),
    ]
    before = {
        project: project.read_bytes(),
        dependency: dependency.read_bytes(),
    }

    code = main(arguments)
    archive_compile = subprocess.run(
        [
            find_uv_bin(),
            "run",
            "--no-project",
            "--with",
            "vyper==0.4.3",
            "vyper",
            "-f",
            "abi",
            str(archive),
        ],
        cwd=foreign_cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    second_code = main(arguments)

    assert code == 0
    assert second_code == 0
    assert zipfile.is_zipfile(archive)
    assert archive_compile.returncode == 0, archive_compile.stderr
    assert json.loads(archive_compile.stdout) == [
        {
            "stateMutability": "nonpayable",
            "type": "function",
            "name": "value",
            "inputs": [],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]
    assert {path: path.read_bytes() for path in before} == before


def test_real_compiler_archive_floor_0_4_0(tmp_path: Path, capsys) -> None:
    contract = tmp_path / "floor.vy"
    contract_source = "#pragma version 0.4.0\n\n@external\ndef value() -> uint256:\n    return 1\n"
    contract.write_text(contract_source, encoding="utf-8")
    archive = tmp_path / "floor.vyz"

    code = main(
        [
            str(contract),
            "--target-version",
            "0.4.0",
            "--include-dependencies",
            "--closure-archive",
            str(archive),
        ]
    )
    below_floor_code = main(
        [
            str(contract),
            "--target-version",
            "0.3.10",
            "--include-dependencies",
            "--closure-archive",
            str(tmp_path / "below-floor.vyz"),
        ]
    )

    assert code == 0
    assert zipfile.is_zipfile(archive)
    assert below_floor_code == 4
    assert "requires >= 0.4.0" in capsys.readouterr().err
    assert contract.read_text(encoding="utf-8") == contract_source


def test_real_twocrypto_source_reports_declared_environment(tmp_path: Path) -> None:
    project = tmp_path / "twocrypto-ng"
    contract = project / "contracts" / "main" / "lp_token.vy"
    contract.parent.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        """[project]
name = "twocrypto-ng"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "vyper==0.4.1",
    "snekmate==0.1.1",
]
""",
        encoding="utf-8",
    )
    contract.write_text(
        """# pragma version 0.4.1

from ethereum.ercs import IERC20
from ethereum.ercs import IERC20Detailed

implements: IERC20
implements: IERC20Detailed

from snekmate.auth import ownable
initializes: ownable

from snekmate.tokens import erc20
initializes: erc20[ownable := ownable]
exports: (
    erc20.transfer,
    erc20.transferFrom,
    erc20.approve,
    erc20.balanceOf,
    erc20.allowance,
    erc20.totalSupply,
)

DECIMALS: constant(uint8) = 18
symbol: public(String[32])
name: public(String[64])

@deploy
def __init__(name: String[64], symbol: String[32]):
    ownable.__init__()
    erc20.__init__("", "", DECIMALS, "", "")
    self.name = name
    self.symbol = symbol

@view
@external
def decimals() -> uint8:
    return DECIMALS
""",
        encoding="utf-8",
    )
    subprocess.run(
        [find_uv_bin(), "lock", "--project", str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    report = tmp_path / "twocrypto-report.json"

    assert (
        main(
            [
                "--target-version",
                "0.4.1",
                "--report-json",
                str(report),
                str(contract),
            ]
        )
        == 0
    )

    report_data = json.loads(report.read_text(encoding="utf-8"))
    assert report_data["schema_version"] == 4
    assert report_data["producer"] == {"name": "vyupgrade", "version": "0.6.2"}
    validation = report_data["files"][0]["validation"]
    attestation = validation["source_attestation"]
    declarations = attestation["declared_spec"]["compiler_declarations"]
    assert {declaration["value"] for declaration in declarations} == {
        "vyper==0.4.1",
        "0.4.1",
    }
    assert attestation["resolved_compiler"]["version"] == "0.4.1"
    assert attestation["authority_rule"] == "project-lock"
    assert attestation["dependency_context"]["mode"] == "project"
    assert attestation["dependency_context"]["project_root"] == str(project.resolve())
    assert attestation["dependency_context"]["manifest"]["path"] == str(
        (project / "pyproject.toml").resolve()
    )
    assert len(attestation["dependency_context"]["manifest"]["sha256"]) == 64
    lock_identity = attestation["dependency_context"]["lockfile"]
    assert lock_identity["path"] == str((project / "uv.lock").resolve())
    assert lock_identity["sha256"] == hashlib.sha256((project / "uv.lock").read_bytes()).hexdigest()
    resolved_packages = attestation["dependency_context"]["resolved_packages"]
    assert {"vyper", "snekmate"} <= {package["name"].lower() for package in resolved_packages}
    compiler_identity = attestation["resolved_compiler"]
    assert compiler_identity["executable"]["path"] == "vyper"
    assert len(compiler_identity["executable"]["sha256"]) == 64
    assert len(compiler_identity["artifact"]["sha256"]) == 64
    source_identity = {
        "path": str(contract.resolve()),
        "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
    }
    assert attestation["declared_spec"]["sources"] == [source_identity]
    assert len(attestation["declared_spec"]["snapshot"]["sha256"]) == 64
    assert attestation["validated_source_set"] == [source_identity]
    assert attestation["attempt_sequence"] == [
        {
            "sequence": 1,
            "source": source_identity,
            "compiler_started": True,
            "completion_status": "completed",
            "exit_status": {"code": 0, "signal": None},
            "failure_origin": None,
        }
    ]
    assert attestation["compiler_started"] is True
    assert attestation["completion_status"] == "completed"
    assert attestation["exit_status"] == {"code": 0, "signal": None}
    assert attestation["failure_origin"] is None
    assert attestation["compiler_output"] is None


def test_real_uv_workspace_member_uses_root_lock_and_workspace_sources(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = workspace / "packages" / "app"
    helper = workspace / "packages" / "helper"
    helper_package = helper / "src" / "vyupgrade_pr33_helper"
    app.mkdir(parents=True)
    helper_package.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text(
        """[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
vyupgrade-pr33-helper = { workspace = true }
""",
        encoding="utf-8",
    )
    (app / "pyproject.toml").write_text(
        """[project]
name = "workspace-app"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["vyper==0.4.1", "vyupgrade-pr33-helper"]
""",
        encoding="utf-8",
    )
    (helper / "pyproject.toml").write_text(
        """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "vyupgrade-pr33-helper"
version = "0.1.0"
""",
        encoding="utf-8",
    )
    (helper_package / "__init__.py").write_text("", encoding="utf-8")
    contract = app / "contract.vy"
    contract.write_text("# pragma version 0.4.1\nvalue: public(uint256)\n", encoding="utf-8")
    subprocess.run(
        [find_uv_bin(), "lock", "--project", str(app)],
        check=True,
        capture_output=True,
        text=True,
    )

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3"),
        "0.4.1",
    )

    assert result.status == "passed"
    assert result.compiler_authority == "project-lock"
    assert result.dependency_context is not None
    assert result.dependency_context.project_root == str(workspace.resolve())
    assert result.dependency_context.manifest is not None
    assert result.dependency_context.manifest.path == str((workspace / "pyproject.toml").resolve())
    assert result.dependency_context.lockfile is not None
    assert result.dependency_context.lockfile.path == str((workspace / "uv.lock").resolve())
    assert {source.path for source in result.dependency_context.declared_sources} == {
        str(helper.resolve())
    }
    assert "vyupgrade-pr33-helper" in {
        package.name.lower() for package in result.dependency_context.resolved_packages
    }


def test_real_unlocked_project_preserves_sibling_path_sources(tmp_path: Path) -> None:
    project = tmp_path / "project"
    helper = tmp_path / "helper"
    helper_package = helper / "src" / "vyupgrade_pr33_helper"
    project.mkdir()
    helper_package.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        """[project]
name = "sibling-path-app"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["vyper==0.4.1", "vyupgrade-pr33-helper"]

[tool.uv.sources]
vyupgrade-pr33-helper = { path = "../helper" }
""",
        encoding="utf-8",
    )
    (helper / "pyproject.toml").write_text(
        """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "vyupgrade-pr33-helper"
version = "0.1.0"
""",
        encoding="utf-8",
    )
    (helper_package / "__init__.py").write_text("", encoding="utf-8")
    source = "# pragma version 0.4.1\nvalue: public(uint256)\n"
    contract = project / "contract.vy"
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.1")

    source_result = compile_source_file(contract, config, "0.4.1")
    target_result = compile_target_source(contract, source, config)

    assert source_result.status == "passed"
    assert target_result.status == "passed"
    assert source_result.dependency_context is not None
    assert {source.path for source in source_result.dependency_context.declared_sources} == {
        str(helper.resolve())
    }



def test_real_explicit_source_compiler_overrides_declared_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "explicit-source-compiler"
version = "0.1.0"
dependencies = ["vyper==0.4.3"]
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text("# pragma version 0.4.3\n", encoding="utf-8")
    requested_compiler = project / "missing-vyper"

    result = compile_source_file(
        contract,
        Config(
            paths=(contract,),
            target_version="0.4.3",
            source_vyper=str(requested_compiler),
        ),
        "0.4.3",
    )

    assert result.status == "failed"
    assert result.compiler_started is False
    assert result.failure_origin == "launch"
    assert result.compiler_authority == "explicit-executable"
    assert result.command is not None
    compiler_index = result.command.index(str(requested_compiler))
    assert result.command[compiler_index:] == [
        str(requested_compiler),
        "-f",
        "abi,method_identifiers,layout,ast",
        "-p",
        str(project),
        "-p",
        str(project),
        str(contract),
    ]


@pytest.mark.parametrize(
    ("source", "source_version", "resolved_version"),
    [
        ("# pragma version 0.4.1\n", "0.4.1", "0.4.1+commit.8a93dd27"),
        ("value: public(uint256)\n", None, "0.4.1"),
    ],
    ids=["local-build-metadata", "missing-pragma"],
)
def test_real_explicit_project_compiler_accepts_coherent_source(
    tmp_path: Path,
    source: str,
    source_version: str | None,
    resolved_version: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "explicit-source-coherence"
version = "0.1.0"
dependencies = ["vyper==0.4.1"]
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text(source, encoding="utf-8")
    executable = _write_test_compiler(
        tmp_path,
        """print("[]")
print("{}")
print("{}")
print('{"ast_type": "Module", "body": []}')
""",
        version=resolved_version,
    )

    result = compile_source_file(
        contract,
        Config(
            paths=(contract,),
            target_version="0.4.3",
            source_vyper=str(executable),
        ),
        source_version,
    )

    assert result.status == "passed"
    assert result.resolved_compiler == resolved_version
    assert result.compiler_authority == "explicit-executable"


def test_real_compiler_overlay_uses_project_interpreter(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "python-constraint"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = []
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text("# pragma version 0.4.3\nvalue: public(uint256)\n", encoding="utf-8")

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3"),
        "0.4.3",
    )

    assert result.status == "passed"
    assert result.compiler_authority == "source-exact"
    assert result.resolved_compiler == "0.4.3"
    assert result.dependency_context is not None
    assert result.dependency_context.python_constraint == ">=3.13"


def test_real_project_markers_use_the_selected_python(tmp_path: Path) -> None:
    if sys.version_info[:2] == (3, 11):
        python_constraint = ">=3.12,<3.13"
        marker = "python_version >= '3.12'"
    else:
        python_constraint = ">=3.11,<3.12"
        marker = "python_version < '3.12'"
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        f"""[project]
name = "selected-python-markers"
version = "0.1.0"
requires-python = "{python_constraint}"
dependencies = ["vyper==0.4.1; {marker}"]
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text("# pragma version >=0.4.0\nvalue: public(uint256)\n", encoding="utf-8")

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3"),
        "0.4.3",
    )

    assert result.status == "passed"
    assert result.compiler_authority == "project-manifest"
    assert result.resolved_compiler == "0.4.1"
    assert {declaration.value for declaration in result.compiler_declarations} == {
        "vyper==0.4.1; " + marker,
        ">=0.4.0",
    }


def test_real_target_python_pin_is_preserved_when_compatible(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "compatible-target-python"
version = "0.1.0"
requires-python = ">=3.11,<3.12"
dependencies = []
""",
        encoding="utf-8",
    )
    source = "# pragma version 0.4.3\nvalue: public(uint256)\n"
    contract = project / "contract.vy"
    contract.write_text(source, encoding="utf-8")

    result = compile_target_source(
        contract,
        source,
        Config(paths=(contract,), target_version="0.4.3", target_python="3.11"),
    )

    assert result.status == "passed"
    assert result.command is not None
    assert result.command[result.command.index("--python") + 1] == "3.11"


def test_real_target_python_pin_conflict_is_an_environment_failure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "conflicting-target-python"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = []
""",
        encoding="utf-8",
    )
    source = "# pragma version 0.4.3\nvalue: public(uint256)\n"
    contract = project / "contract.vy"
    contract.write_text(source, encoding="utf-8")

    result = compile_target_source(
        contract,
        source,
        Config(paths=(contract,), target_version="0.4.3", target_python="3.12"),
    )

    assert result.status == "failed"
    assert result.compiler_started is False
    assert result.failure_origin == "environment"
    assert result.stderr is not None
    assert "target Python pin '3.12'" in result.stderr
    assert "requires-python '>=3.13'" in result.stderr


@pytest.mark.parametrize(
    "ignored_declaration",
    [
        """dependencies = ["vyper==0.4.1; sys_platform == 'win32'"]""",
        """dependencies = []

[tool.poetry.dependencies]
python = ">=3.11"
vyper = "0.4.1"
""",
    ],
    ids=["inactive-marker", "poetry-only"],
)
def test_real_compiler_overlay_ignores_inactive_uv_and_poetry_declarations(
    tmp_path: Path,
    ignored_declaration: str,
) -> None:
    if "sys_platform" in ignored_declaration and sys.platform == "win32":
        pytest.skip("the reviewer's marker is active on Windows")
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        f"""[project]
name = "inactive-authority"
version = "0.1.0"
requires-python = ">=3.11"
{ignored_declaration}
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text("# pragma version 0.4.3\nvalue: public(uint256)\n", encoding="utf-8")

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3"),
        "0.4.3",
    )

    assert result.status == "passed"
    assert result.compiler_authority == "source-exact"
    assert result.resolved_compiler == "0.4.3"
    assert {declaration.kind for declaration in result.compiler_declarations} == {"source-pragma"}


def test_compiler_runner_writes_evidence_without_site_packages(tmp_path: Path) -> None:
    site, bin_dir = _write_managed_vyper_environment(
        tmp_path,
        """def _parse_args(_arguments):
    print("[]")
    print("{}")
    print("{}")
    print('{"ast_type": "Module", "body": []}')
""",
    )
    result_path = tmp_path / "result.json"
    runner = Path(__file__).parents[1] / "src" / "vyupgrade" / "compiler_runner.py"
    coherence = json.dumps({"declaration": "0.4.3", "versions": ["0.4.3"]})

    process = subprocess.run(
        [
            sys.executable,
            "-S",
            str(runner),
            str(result_path),
            "5",
            "managed",
            coherence,
            "vyper",
            "-f",
            "abi,method_identifiers,layout,ast",
            "contract.vy",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(site),
        },
    )

    assert process.returncode == 0, process.stderr
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["state"] == "complete"
    assert payload["compiler_started"] is True
    assert payload["failure_origin"] is None
    assert payload["resolved_compiler"] == "0.4.3"


def test_managed_compiler_timeout_does_not_require_posix_alarms(tmp_path: Path) -> None:
    site, _bin_dir = _write_managed_vyper_environment(
        tmp_path,
        """import time

def _parse_args(_arguments):
    time.sleep(5)
""",
    )
    process = _run_managed_compiler_harness(
        site,
        """import signal
import subprocess
from vyupgrade.compiler_runner import _run_managed_compiler

for name in ("SIGALRM", "ITIMER_REAL", "setitimer"):
    if hasattr(signal, name):
        delattr(signal, name)
try:
    _run_managed_compiler(["vyper"], 0.05)
except subprocess.TimeoutExpired:
    raise SystemExit(0)
raise SystemExit("managed compiler did not time out")
""",
    )

    assert process.returncode == 0, process.stderr


def test_managed_compiler_worker_crash_is_internal(tmp_path: Path) -> None:
    site, _bin_dir = _write_managed_vyper_environment(
        tmp_path,
        """import os

def _parse_args(_arguments):
    os._exit(17)
""",
    )

    process = _run_managed_compiler_harness(
        site,
        """from vyupgrade.compiler_runner import _run_managed_compiler

result = _run_managed_compiler(["vyper"], 5)
assert result["failure_origin"] == "compiler-internal", result
assert result["returncode"] == 17, result
""",
    )

    assert process.returncode == 0, process.stderr


def test_real_ranged_pragma_prefers_declared_project_compiler(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "range-pin"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["vyper==0.4.1"]
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text(
        """# pragma version >=0.4.0

value: public(uint256)
""",
        encoding="utf-8",
    )
    report = tmp_path / "range-report.json"

    assert (
        main(
            [
                "--target-version",
                "0.4.3",
                "--report-json",
                str(report),
                str(contract),
            ]
        )
        == 0
    )

    validation = json.loads(report.read_text(encoding="utf-8"))["files"][0]["validation"]
    attestation = validation["source_attestation"]
    declarations = attestation["declared_spec"]["compiler_declarations"]
    assert {declaration["value"] for declaration in declarations} == {
        "vyper==0.4.1",
        ">=0.4.0",
    }
    assert attestation["resolved_compiler"]["version"] == "0.4.1"
    assert validation["source_compiler"] == "0.4.1"
    assert attestation["authority_rule"] == "project-manifest"
    assert attestation["compiler_started"] is True
    assert attestation["failure_origin"] is None


def test_real_conflicting_project_and_source_declarations_are_environment_origin(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "conflict"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["vyper==0.4.3"]
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text("# pragma version 0.4.1\nvalue: public(uint256)\n", encoding="utf-8")
    report = tmp_path / "conflict-report.json"

    assert (
        main(
            [
                str(contract),
                "--target-version",
                "0.4.3",
                "--report-json",
                str(report),
            ]
        )
        != 0
    )

    validation = json.loads(report.read_text())["files"][0]["validation"]
    attestation = validation["source_attestation"]
    assert validation["source_compile"] == "failed"
    assert attestation["authority_rule"] == "project-manifest"
    assert attestation["resolved_compiler"]["version"] == "0.4.3"
    assert attestation["compiler_started"] is False
    assert attestation["completion_status"] == "not-started"
    assert attestation["exit_status"] == {"code": None, "signal": None}
    assert attestation["validated_source_set"] == []
    assert attestation["failure_origin"] == "environment"
    assert attestation["compiler_output"] is None


def test_real_declared_dependency_rejection_is_compiler_origin(tmp_path: Path) -> None:
    project = tmp_path / "project"
    dependency = project / "dependency"
    package = dependency / "declared_dependency"
    package.mkdir(parents=True)
    (dependency / "pyproject.toml").write_text(
        """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "declared-dependency"
version = "0.1.0"

[tool.hatch.build.targets.wheel]
packages = ["declared_dependency"]
""",
        encoding="utf-8",
    )
    (package / "broken.vy").write_text(
        """# pragma version 0.4.1
VALUE: constant(uint256) = "not a uint"
""",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        """[project]
name = "dependency-rejection"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["vyper==0.4.1", "declared-dependency"]

[tool.uv.sources]
declared-dependency = { path = "dependency" }
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text(
        """# pragma version 0.4.1
from declared_dependency import broken
""",
        encoding="utf-8",
    )

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3"),
        "0.4.1",
    )

    assert result.status == "failed"
    assert result.resolved_compiler == "0.4.1"
    assert result.compiler_started is True
    assert result.failure_origin == "compiler"
    assert result.compiler_output is not None
    assert "declared_dependency" in result.compiler_output.stderr


def test_real_uv_resolution_failure_is_environment_origin(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "broken-environment"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["vyper==0.4.1", "missing-local-dependency"]

[tool.uv.sources]
missing-local-dependency = { path = "does-not-exist" }
""",
        encoding="utf-8",
    )
    contract = project / "contract.vy"
    contract.write_text("# pragma version 0.4.1\n", encoding="utf-8")

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3"),
        "0.4.1",
    )

    assert result.status == "failed"
    assert result.compiler_started is False
    assert result.failure_origin == "environment"
    assert result.compiler_output is None


def test_real_missing_compiler_is_launch_origin(tmp_path: Path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# pragma version 0.4.1\n", encoding="utf-8")

    result = compile_source_file(
        contract,
        Config(
            paths=(contract,),
            target_version="0.4.3",
            source_vyper=str(tmp_path / "missing-vyper"),
        ),
        "0.4.1",
    )

    assert result.status == "failed"
    assert result.compiler_started is False
    assert result.failure_origin == "launch"
    assert result.compiler_output is None


def test_real_compiler_timeout_is_timeout_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vyupgrade import compiler

    executable = _write_test_compiler(
        tmp_path,
        """import time
time.sleep(5)
""",
    )
    contract = tmp_path / "contract.vy"
    contract.write_text("# pragma version 0.4.1\n", encoding="utf-8")
    monkeypatch.setattr(compiler, "COMPILE_TIMEOUT_SECONDS", 0.05)

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3", source_vyper=str(executable)),
        "0.4.1",
    )

    assert result.status == "failed"
    assert result.resolved_compiler == "0.4.1"
    assert result.compiler_started is True
    assert result.failure_origin == "timeout"


def test_real_signaled_compiler_is_compiler_internal_origin(tmp_path: Path) -> None:
    executable = _write_test_compiler(
        tmp_path,
        """import os
import signal

os.kill(os.getpid(), signal.SIGTERM)
""",
    )
    contract = tmp_path / "contract.vy"
    contract.write_text("# pragma version 0.4.1\n", encoding="utf-8")

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3", source_vyper=str(executable)),
        "0.4.1",
    )

    assert result.status == "failed"
    assert result.compiler_started is True
    assert result.failure_origin == "compiler-internal"
    assert result.completion_status == "signaled"
    assert result.exit_status.code == -signal.SIGTERM
    assert result.exit_status.signal == signal.SIGTERM


def test_real_unhandled_explicit_compiler_failure_is_not_rejection(tmp_path: Path) -> None:
    executable = _write_test_compiler(tmp_path, 'raise RuntimeError("compiler crashed")\n')
    contract = tmp_path / "contract.vy"
    contract.write_text("# pragma version 0.4.1\n", encoding="utf-8")

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3", source_vyper=str(executable)),
        "0.4.1",
    )

    assert result.status == "failed"
    assert result.compiler_started is True
    assert result.failure_origin == "adapter"
    assert result.completion_status == "completed"
    assert result.exit_status.code == 1
    assert result.compiler_output is not None
    assert "RuntimeError: compiler crashed" in result.compiler_output.stderr


def test_real_source_nonrejection_has_one_attested_attempt(tmp_path: Path) -> None:
    attempt_log = tmp_path / "attempts.log"
    executable = _write_test_compiler(
        tmp_path,
        f"""with open({str(attempt_log)!r}, "a", encoding="utf-8") as log:
    log.write("compile\\n")
print("ValueError: Unsupported format type 'ast'", file=sys.stderr)
raise SystemExit(7)
""",
    )
    contract = tmp_path / "contract.vy"
    contract.write_text("# pragma version 0.4.1\n", encoding="utf-8")
    report = tmp_path / "report.json"

    assert (
        main(
            [
                str(contract),
                "--target-version",
                "0.4.3",
                "--source-vyper",
                str(executable),
                "--report-json",
                str(report),
            ]
        )
        != 0
    )

    assert attempt_log.read_text(encoding="utf-8").splitlines() == ["compile"]
    attestation = json.loads(report.read_text(encoding="utf-8"))["files"][0]["validation"][
        "source_attestation"
    ]
    source_identity = {
        "path": str(contract.resolve()),
        "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
    }
    assert attestation["failure_origin"] == "adapter"
    assert attestation["completion_status"] == "completed"
    assert attestation["exit_status"] == {"code": 7, "signal": None}
    assert attestation["validated_source_set"] == [source_identity]
    assert attestation["attempt_sequence"] == [
        {
            "sequence": 1,
            "source": source_identity,
            "compiler_started": True,
            "completion_status": "completed",
            "exit_status": {"code": 7, "signal": None},
            "failure_origin": "adapter",
        }
    ]


def test_real_invalid_compiler_output_is_adapter_origin(tmp_path: Path) -> None:
    executable = _write_test_compiler(tmp_path, 'print("not-json")\n')
    contract = tmp_path / "contract.vy"
    contract.write_text("# pragma version 0.4.1\n", encoding="utf-8")

    result = compile_source_file(
        contract,
        Config(paths=(contract,), target_version="0.4.3", source_vyper=str(executable)),
        "0.4.1",
    )

    assert result.status == "failed"
    assert result.resolved_compiler == "0.4.1"
    assert result.compiler_started is True
    assert result.failure_origin == "adapter"
    assert result.compiler_output is not None
    assert result.compiler_output.stdout == "not-json\n"


def _run_managed_compiler_harness(
    site: Path,
    script: str,
) -> subprocess.CompletedProcess[str]:
    source_root = Path(__file__).parents[1] / "src"
    python_path = os.pathsep.join(
        part for part in (str(site), str(source_root), os.environ.get("PYTHONPATH")) if part
    )
    return subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": python_path},
    )


def _write_managed_vyper_environment(
    tmp_path: Path,
    compiler_module: str,
) -> tuple[Path, Path]:
    site = tmp_path / "site"
    package = site / "vyper"
    cli = package / "cli"
    dist_info = site / "vyper-0.4.3.dist-info"
    bin_dir = tmp_path / "bin"
    cli.mkdir(parents=True)
    dist_info.mkdir(parents=True)
    bin_dir.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "exceptions.py").write_text(
        "class VyperException(Exception):\n    pass\n",
        encoding="utf-8",
    )
    (cli / "__init__.py").write_text("", encoding="utf-8")
    (cli / "vyper_compile.py").write_text(compiler_module, encoding="utf-8")
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: vyper\nVersion: 0.4.3\n",
        encoding="utf-8",
    )
    (dist_info / "RECORD").write_text(
        "vyper-0.4.3.dist-info/METADATA,,\n",
        encoding="utf-8",
    )
    executable = bin_dir / "vyper"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return site, bin_dir


def _write_test_compiler(
    tmp_path: Path,
    compile_body: str,
    *,
    version: str = "0.4.1",
) -> Path:
    executable = tmp_path / "test-vyper"
    executable.write_text(
        f"""#!/usr/bin/env python3
import sys

if "--version" in sys.argv:
    print({version!r})
    raise SystemExit

{compile_body}
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable
