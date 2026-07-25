from __future__ import annotations

import importlib.util
import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vyupgrade.compiler import CompileResult


def _load_corpus_module():
    script = Path(__file__).parents[1] / "scripts" / "corpus.py"
    spec = importlib.util.spec_from_file_location("vyupgrade_corpus_script", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_schema_version_is_three() -> None:
    corpus = _load_corpus_module()

    assert corpus.SMOKE_RESULT_SCHEMA_VERSION == 3


def test_import_old_vyper_bug_uses_csv_versions_for_missing_pragmas(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    source = tmp_path / "old-vyper-bug"
    contracts = source / "contracts" / "ethereum"
    exports = source / "etherscan-export"
    contracts.mkdir(parents=True)
    exports.mkdir(parents=True)
    (contracts / "0xabc.vy").write_text("# @version 0.2.16\nx: uint256\n", encoding="utf-8")
    (contracts / "0xdef.vy").write_text("x: uint256\n", encoding="utf-8")
    (exports / "ethereum.csv").write_text("0xabc,0.2.16\n0xdef,0.3.0\n", encoding="utf-8")

    manifest = corpus.import_old_vyper_bug(source, tmp_path / "corpus" / "vyper")

    assert manifest["counts"]["applicable"] == 2
    by_address = {item["address"]: item for item in manifest["items"]}
    assert by_address["0xabc"]["pragma"] == "0.2.16"
    assert by_address["0xabc"]["source_pragma"] == "0.2.16"
    assert by_address["0xdef"]["pragma"] == "0.3.0"
    assert by_address["0xdef"]["source_pragma"] is None
    assert Path(by_address["0xdef"]["corpus_path"]).read_text(encoding="utf-8") == "x: uint256\n"


def test_import_smart_contract_fiesta_uses_metadata_compiler(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    source = tmp_path / "smart-contract-fiesta"
    contract = source / "organized_contracts" / "ab" / "abcdef"
    contract.mkdir(parents=True)
    (contract / "metadata.json").write_text(
        '{"ContractName":"Vyper_contract","CompilerVersion":"vyper:0.3.7","BytecodeHash":"abcdef"}',
        encoding="utf-8",
    )
    (contract / "main.vy").write_text("x: uint256\n", encoding="utf-8")

    manifest = corpus.import_smart_contract_fiesta(source, tmp_path / "corpus" / "vyper")

    assert manifest["counts"]["applicable"] == 1
    item = manifest["items"][0]
    assert item["pragma"] == "0.3.7"
    assert item["source_pragma"] is None
    assert item["bytecode_hash"] == "abcdef"
    assert Path(item["corpus_path"]).read_text(encoding="utf-8") == "x: uint256\n"


def test_dedupe_manifests_keeps_one_item_per_source_hash(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    stale = tmp_path / "stale.json"
    first_source = tmp_path / "corpus" / "a.vy"
    second_source = tmp_path / "corpus" / "b.vy"
    first_source.parent.mkdir()
    source = "x: uint256\n"
    digest = corpus._source_hash(source)
    first_source.write_text(source, encoding="utf-8")
    second_source.write_text(source, encoding="utf-8")
    base_item = {
        "source_path": str(first_source),
        "repo": "first",
        "relpath": "a.vy",
        "corpus_path": str(first_source),
        "corpus_repo_root": str(first_source.parent),
        "pragma": "0.3.7",
        "source_compiler": "0.3.7",
        "sha256": digest,
    }
    first.write_text(
        json.dumps({"items": [base_item]}),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps(
            {
                "items": [
                    {
                        **base_item,
                        "source_path": str(second_source),
                        "repo": "second",
                        "relpath": "b.vy",
                        "corpus_path": str(second_source),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stale.write_text(
        json.dumps({"items": [{**base_item, "corpus_path": str(tmp_path / "missing.vy")}]}),
        encoding="utf-8",
    )

    manifest = corpus.dedupe_manifests([first, second, stale], tmp_path / "deduped.json")

    assert manifest["counts"]["items_seen"] == 3
    assert manifest["counts"]["deduped"] == 1
    assert manifest["counts"]["duplicates"] == 1
    assert manifest["counts"]["missing_corpus_path"] == 1
    assert len(manifest["items"]) == 1
    assert manifest["items"][0]["duplicate_count"] == 1
    assert manifest["items"][0]["duplicate_sources"][0]["repo"] == "second"


def test_default_dedupe_uses_every_registered_manifest(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    expected = {
        "build": "manifest.json",
        "codeslaw": "codeslaw-manifest.json",
        "codeslaw-buckets": "codeslaw-buckets-manifest.json",
        "old-vyper-bug": "old-vyper-bug-manifest.json",
        "smart-contract-fiesta": "smart-contract-fiesta-manifest.json",
        "vyper-2026": "vyper-2026-manifest.json",
        "chainsecurity": "chainsecurity-manifest.json",
    }
    assert expected == corpus.CORPUS_MANIFESTS

    for filename in corpus.CORPUS_MANIFESTS.values():
        (tmp_path / filename).write_text('{"items": []}', encoding="utf-8")

    manifest = corpus.dedupe_manifests(None, tmp_path / "deduped-manifest.json")

    assert manifest["roots"] == [str(tmp_path / filename) for filename in expected.values()]
    assert str(tmp_path / "chainsecurity-manifest.json") in manifest["roots"]


def test_build_corpus_uses_hash_suffixed_path_for_path_collisions(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    first = tmp_path / "dev" / "yearn" / "yearn-token"
    second = tmp_path / "yearn" / "yearn-token"
    for root in (first, second):
        (root / ".git").mkdir(parents=True)
        (root / "contracts").mkdir()
    first_source = "# @version 0.2.15\nx: uint256\n"
    second_source = "# @version 0.2.8\nx: uint256\n"
    (first / "contracts" / "Token.vy").write_text(first_source, encoding="utf-8")
    (second / "contracts" / "Token.vy").write_text(second_source, encoding="utf-8")

    manifest = corpus.build_corpus(
        (tmp_path / "dev", tmp_path / "yearn"), tmp_path / "corpus" / "vyper"
    )

    paths = [Path(item["corpus_path"]) for item in manifest["items"]]
    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert manifest["counts"]["corpus_path_collisions"] == 1
    assert sorted(path.read_text(encoding="utf-8") for path in paths) == sorted(
        [first_source, second_source]
    )


def test_dedupe_repairs_manifest_path_collisions_from_source_path(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    first_source = tmp_path / "first" / "Token.vy"
    second_source = tmp_path / "second" / "Token.vy"
    stale_corpus_path = tmp_path / "corpus" / "contracts" / "yearn__token" / "Token.vy"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    stale_corpus_path.parent.mkdir(parents=True)
    first_text = "# @version 0.2.15\nx: uint256\n"
    second_text = "# @version 0.2.8\nx: uint256\n"
    first_source.write_text(first_text, encoding="utf-8")
    second_source.write_text(second_text, encoding="utf-8")
    stale_corpus_path.write_text(second_text, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json_dumps(
            {
                "items": [
                    {
                        "source_path": str(first_source),
                        "repo": "yearn__token",
                        "relpath": "Token.vy",
                        "corpus_path": str(stale_corpus_path),
                        "corpus_repo_root": str(stale_corpus_path.parent),
                        "pragma": "0.2.15",
                        "source_compiler": "0.2.15",
                        "sha256": corpus._source_hash(first_text),
                    },
                    {
                        "source_path": str(second_source),
                        "repo": "yearn__token",
                        "relpath": "Token.vy",
                        "corpus_path": str(stale_corpus_path),
                        "corpus_repo_root": str(stale_corpus_path.parent),
                        "pragma": "0.2.8",
                        "source_compiler": "0.2.8",
                        "sha256": corpus._source_hash(second_text),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    manifest = corpus.dedupe_manifests([manifest_path], tmp_path / "deduped.json")

    paths = [Path(item["corpus_path"]) for item in manifest["items"]]
    assert len(set(paths)) == 2
    assert manifest["counts"]["corpus_path_hash_mismatch"] == 1
    assert manifest["counts"]["corpus_path_collisions"] == 1
    for item in manifest["items"]:
        assert corpus._file_hash(Path(item["corpus_path"])) == item["sha256"]


def test_import_vyper_2026_keeps_metadata_when_source_is_not_local(tmp_path: Path) -> None:
    import polars as pl

    corpus = _load_corpus_module()
    source = tmp_path / "vyper-2026"
    data = source / "data"
    data.mkdir(parents=True)
    pl.DataFrame(
        [
            {
                "chain": "ethereum",
                "address": "0xabc",
                "source_available": True,
                "source_origin": "etherscan_getsourcecode",
                "contract_name": "Example",
                "compiler_version": "vyper:0.3.7",
                "normalized_vyper_version": "0.3.7",
                "source_len": 12,
                "source_sha256": "abc",
            }
        ]
    ).write_parquet(data / "etherscan_source_metadata.parquet")

    manifest = corpus.import_vyper_2026(source, tmp_path / "corpus" / "vyper")

    assert manifest["counts"]["etherscan_source_metadata.parquet:rows_seen"] == 1
    assert manifest["counts"]["etherscan_source_metadata.parquet:missing_source_path"] == 1
    assert manifest["items"] == []
    assert manifest["metadata_items"][0]["source_compiler"] == "0.3.7"
    assert manifest["by_metadata_compiler"] == [("0.3.7", 1)]


def test_import_vyper_2026_source_enrichment_inventory(tmp_path: Path) -> None:
    import json

    import polars as pl

    corpus = _load_corpus_module()
    source = tmp_path / "vyper-2026"
    inventory = source / "data" / "source_enrichment" / "vyper_inventory_20260605"
    flat = inventory / "source_store" / "vy" / "flat.vy"
    standard_json = inventory / "source_store" / "standard_json" / "package.json"
    flat.parent.mkdir(parents=True)
    standard_json.parent.mkdir(parents=True)
    flat.write_text("# @version 0.3.10\nx: uint256\n", encoding="utf-8")
    standard_json.write_text(
        json.dumps(
            {
                "compiler_version": "vyper:0.3.10",
                "language": "Vyper",
                "settings": {"compilationTarget": {"contracts/main.vy": "main"}},
                "sources": {
                    "contracts/main.vy": {
                        "content": "# @version 0.3.10\nimport contracts.lib as lib\ny: uint256\n"
                    },
                    "contracts/lib.vy": {"content": "# @version 0.3.10\nz: uint256\n"},
                },
            }
        ),
        encoding="utf-8",
    )
    pl.DataFrame(
        [
            {
                "source_id": "sha256:flat",
                "source_format": "vy",
                "source_sha256": "flat",
                "source_len": flat.stat().st_size,
                "catalog_path": "data/source_enrichment/vyper_inventory_20260605/source_store/vy/flat.vy",
                "representative_provider": "test",
                "representative_source_path": str(flat),
                "duplicate_artifact_count": 1,
                "providers": ["test"],
                "match_kinds": ["address"],
            },
            {
                "source_id": "sha256:json",
                "source_format": "standard_json",
                "source_sha256": "json",
                "source_len": standard_json.stat().st_size,
                "catalog_path": "data/source_enrichment/vyper_inventory_20260605/source_store/standard_json/package.json",
                "representative_provider": "test",
                "representative_source_path": str(standard_json),
                "duplicate_artifact_count": 1,
                "providers": ["test"],
                "match_kinds": ["address"],
            },
        ]
    ).write_parquet(inventory / "source_catalog.parquet")
    pl.DataFrame(
        [
            {
                "provider": "test",
                "match_kind": "address",
                "source_format": "vy",
                "source_path": str(flat),
                "has_source_file": True,
                "source_len": flat.stat().st_size,
                "source_sha256": "flat",
                "priority": 1,
                "address": "0xabc",
                "runtime_code_hash": "0x1",
                "compiler_version": "vyper:0.3.10",
                "source_compiler": "0.3.10",
                "contract_name": "Flat",
                "note": None,
                "source_id": "sha256:flat",
                "catalog_path": "data/source_enrichment/vyper_inventory_20260605/source_store/vy/flat.vy",
            },
            {
                "provider": "test",
                "match_kind": "address",
                "source_format": "standard_json",
                "source_path": str(standard_json),
                "has_source_file": True,
                "source_len": standard_json.stat().st_size,
                "source_sha256": "json",
                "priority": 1,
                "address": "0xdef",
                "runtime_code_hash": "0x2",
                "compiler_version": "vyper:0.3.10",
                "source_compiler": "0.3.10",
                "contract_name": "Json",
                "note": None,
                "source_id": "sha256:json",
                "catalog_path": "data/source_enrichment/vyper_inventory_20260605/source_store/standard_json/package.json",
            },
        ]
    ).write_parquet(inventory / "source_artifacts.parquet")

    manifest = corpus.import_vyper_2026(
        source / "data" / "source_enrichment", tmp_path / "corpus" / "vyper"
    )

    assert manifest["counts"]["source_catalog.parquet:rows_seen"] == 2
    assert len(manifest["items"]) == 2
    assert {item["address"] for item in manifest["items"]} == {"0xabc", "0xdef"}
    assert all(Path(item["corpus_path"]).exists() for item in manifest["items"])
    assert any(item.get("standard_json") for item in manifest["items"])
    json_items = [item for item in manifest["items"] if item.get("standard_json")]
    assert len(json_items) == 1
    assert json_items[0]["relpath"] == "standard_json/json/contracts/main.vy"
    assert (
        tmp_path
        / "corpus"
        / "vyper"
        / "contracts"
        / "vyper_2026"
        / "standard_json"
        / "json"
        / "contracts"
        / "lib.vy"
    ).exists()


def test_import_chainsecurity_preserves_standard_json_source_tree(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    source = tmp_path / "vyper-contracts"
    export = source / "export"
    export.mkdir(parents=True)
    metadata = {
        "language": "Vyper",
        "sources": {
            "interfaces/Foo.vyi": {"content": "# @version 0.4.1\n@external\ndef f(): ...\n"},
            "contracts/Main.vy": {
                "content": "# @version ^0.4.0\nfrom interfaces import Foo\nx: uint256\n"
            },
        },
        "settings": {
            "outputSelection": {"contracts/Main.vy": ["abi"]},
            "search_paths": [".", "0", "interfaces"],
        },
        "compiler_version": "v0.4.1+commit.8a93dd27",
    }
    (export / "1_0x0000000000000000000000000000000000000001.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )

    manifest = corpus.import_chainsecurity(source, tmp_path / "corpus" / "vyper")

    assert manifest["counts"]["json_sources_written"] == 2
    assert manifest["counts"]["applicable"] == 1
    item = manifest["items"][0]
    assert item["repo"] == "chainsecurity"
    assert item["chain"] == "1"
    assert item["address"] == "0x0000000000000000000000000000000000000001"
    assert item["pragma"] == "0.4.1"
    assert item["source_pragma"] == "^0.4.0"
    assert item["source_compiler"] == "0.4.1"
    assert item["compiler_search_paths"] == [
        str(Path(item["corpus_repo_root"])),
        str(Path(item["corpus_repo_root"], "interfaces")),
    ]
    assert Path(item["corpus_repo_root"], "interfaces", "Foo.vyi").exists()


def test_import_chainsecurity_uses_flat_sources_with_pragmas(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    source = tmp_path / "vyper-contracts"
    export = source / "export"
    export.mkdir(parents=True)
    (export / "10_0x0000000000000000000000000000000000000002.vy").write_text(
        "# @version 0.3.10\nx: uint256\n",
        encoding="utf-8",
    )

    manifest = corpus.import_chainsecurity(source, tmp_path / "corpus" / "vyper")

    assert manifest["counts"]["flat_seen"] == 1
    assert manifest["counts"]["applicable"] == 1
    item = manifest["items"][0]
    assert item["repo"] == "chainsecurity_flat"
    assert item["source_compiler"] == "0.3.10"
    assert item["chain"] == "10"


def test_smoke_summary_groups_failures_and_rules(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    summary = corpus._smoke_summary(
        [
            {
                "repo": "chainsecurity",
                "source_compiler": "0.3.10",
                "source_compile": "passed",
                "target_compile": "failed",
                "target_error": "target failed",
                "validation_status": "blocked",
                "validation_blockers": [{"code": "target_compile_failed"}],
                "validation_waivers": [],
                "fixes": ["VY001"],
                "diagnostics": ["VYD001"],
            },
            {
                "repo": "chainsecurity",
                "source_compiler": "0.2.16",
                "source_compile": "failed",
                "target_compile": "failed",
                "source_error": "source failed",
                "target_error": "target failed",
                "validation_status": "blocked",
                "validation_blockers": [
                    {"code": "source_compile_failed"},
                    {"code": "target_compile_failed"},
                ],
                "validation_waivers": [],
                "fixes": [],
                "diagnostics": [],
            },
            {
                "repo": "safe-degraded",
                "source_compiler": "0.2.15",
                "source_compile": "degraded",
                "target_compile": "passed",
                "source_error": "unsupported optional source artifact",
                "validation_status": "passed",
                "validation_blockers": [],
                "validation_waivers": [],
                "fixes": [],
                "diagnostics": [],
            },
            {
                "repo": "safe-waived",
                "source_compiler": "0.2.15",
                "source_compile": "degraded",
                "target_compile": "passed",
                "source_error": "waived source artifact gap",
                "validation_status": "waived",
                "validation_blockers": [],
                "validation_waivers": [{"code": "source_artifacts_unavailable"}],
                "fixes": [],
                "diagnostics": [],
            },
            {
                "repo": "crashed",
                "source_compiler": "0.1.0",
                "source_compile": "exception",
                "target_compile": "exception",
                "source_error": "runner exploded",
                "target_error": "traceback",
                "fixes": [],
                "diagnostics": [],
            },
            {
                "repo": "artifact-change",
                "source_compiler": "0.4.0",
                "source_compile": "passed",
                "target_compile": "passed",
                "source_error": "harmless source warning",
                "target_error": "harmless target warning",
                "validation_status": "blocked",
                "validation_blockers": [{"code": "storage_layout_changed"}],
                "validation_waivers": [],
                "fixes": [],
                "diagnostics": [],
            },
        ],
        tmp_path / "manifest.json",
        tmp_path / "results.json",
        1.2,
    )

    assert summary["failed_compilers"] == [
        ("0.3.10", 1),
        ("0.2.16", 1),
        ("0.1.0", 1),
        ("0.4.0", 1),
    ]
    assert summary["top_target_errors"] == [("target failed", 2), ("traceback", 1)]
    assert summary["top_source_errors"] == [("source failed", 1), ("runner exploded", 1)]
    assert summary["top_fixes"] == [("VY001", 1)]
    assert summary["top_diagnostics"] == [("VYD001", 1)]
    assert summary["status_pairs"] == {
        "degraded->passed": 2,
        "passed->failed": 1,
        "failed->failed": 1,
        "exception->exception": 1,
        "passed->passed": 1,
    }
    assert summary["normalized_status_pairs"] == {
        "passed->passed": 3,
        "passed->failed": 1,
        "failed->failed": 1,
        "exception->exception": 1,
    }
    assert summary["validation_statuses"] == {
        "blocked": 3,
        "passed": 1,
        "waived": 1,
        "exception": 1,
    }
    assert summary["validation_blockers"] == {
        "target_compile_failed": 2,
        "source_compile_failed": 1,
        "storage_layout_changed": 1,
    }
    assert summary["validation_waivers"] == {"source_artifacts_unavailable": 1}
    assert summary["failed_repos"] == [
        ("chainsecurity", 2),
        ("crashed", 1),
        ("artifact-change", 1),
    ]


def test_smoke_items_filters_by_corpus_path_when_paths_are_given(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    first = tmp_path / "contracts" / "a.vy"
    second = tmp_path / "contracts" / "b.vy"
    first.parent.mkdir(parents=True)
    first.write_text("x: uint256\n", encoding="utf-8")
    second.write_text("y: uint256\n", encoding="utf-8")
    items = [
        {"corpus_path": str(first), "repo": "a"},
        {"corpus_path": str(second), "repo": "b"},
    ]

    selected = corpus._smoke_items(items, [first], 0)

    assert selected == [items[0]]


def test_smoke_items_keeps_limit_for_unfiltered_runs() -> None:
    corpus = _load_corpus_module()
    items = [
        {"corpus_path": "a.vy", "repo": "a"},
        {"corpus_path": "b.vy", "repo": "b"},
    ]

    assert corpus._smoke_items(items, None, 1) == [items[0]]


def test_smoke_checkpoints_in_manifest_order_after_creating_parent(
    monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    items = [{"index": index, "repo": "test"} for index in range(51)]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    output_path = tmp_path / "nested" / "smoke-results.json"

    def fake_smoke_one(item, target_version):
        time.sleep((len(items) - item["index"]) * 0.0005)
        return {
            **item,
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": target_version,
        }

    monkeypatch.setattr(corpus, "_smoke_one", fake_smoke_one)

    summary = corpus.smoke_corpus(manifest_path, output_path, "0.4.3", 8, 0)

    results = json.loads(output_path.read_text(encoding="utf-8"))
    assert [result["index"] for result in results] == list(range(51))
    assert summary["total"] == 51


def test_smoke_resumes_an_ordered_prefix_and_only_submits_remaining_items(
    monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    items = [{"index": index, "repo": "test"} for index in range(3)]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    output_path = tmp_path / "smoke-results.json"
    target_version = "0.4.3"
    identity = corpus._smoke_run_identity(manifest_path, target_version, items)
    completed = [
        {
            **item,
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": target_version,
        }
        for item in items[:2]
    ]
    corpus._write_smoke_checkpoint(output_path, completed, identity)
    submitted: list[int] = []

    def fake_smoke_one(item, requested_target):
        submitted.append(item["index"])
        return {
            **item,
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": requested_target,
        }

    monkeypatch.setattr(corpus, "_smoke_one", fake_smoke_one)

    summary = corpus.smoke_corpus(manifest_path, output_path, target_version, 2, 0)

    assert submitted == [2]
    results = json.loads(output_path.read_text(encoding="utf-8"))
    assert [result["index"] for result in results] == [0, 1, 2]
    assert summary["total"] == 3


def test_smoke_resumes_when_selected_compiler_differs_from_manifest_hint(
    monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    items = [{"index": index, "repo": "test", "source_compiler": "0.3.3"} for index in range(2)]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    output_path = tmp_path / "smoke-results.json"
    target_version = "0.4.3"
    identity = corpus._smoke_run_identity(manifest_path, target_version, items)
    corpus._write_smoke_checkpoint(
        output_path,
        [
            {
                **items[0],
                "source_compiler_hint": "0.3.3",
                "source_compiler": "0.3.4",
                "source_compile": "passed",
                "target_compile": "passed",
                "target_version": target_version,
            }
        ],
        identity,
    )
    submitted: list[int] = []

    def fake_smoke_one(item, requested_target):
        submitted.append(item["index"])
        return {
            **item,
            "source_compiler_hint": item["source_compiler"],
            "source_compiler": "0.3.4",
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": requested_target,
        }

    monkeypatch.setattr(corpus, "_smoke_one", fake_smoke_one)

    summary = corpus.smoke_corpus(manifest_path, output_path, target_version, 1, 0)

    assert submitted == [1]
    results = json.loads(output_path.read_text(encoding="utf-8"))
    assert [result["index"] for result in results] == [0, 1]
    assert results[0]["source_compiler_hint"] == "0.3.3"
    assert results[0]["source_compiler"] == "0.3.4"
    assert summary["total"] == 2


@pytest.mark.parametrize("old_schema", [None, 2])
def test_smoke_does_not_resume_older_schema_result_rows(
    old_schema: int | None, monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    items = [{"index": index, "repo": "test"} for index in range(2)]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    output_path = tmp_path / "smoke-results.json"
    target_version = "0.4.3"
    identity = corpus._smoke_run_identity(manifest_path, target_version, items)
    legacy_results = [
        {
            **items[0],
            **({"smoke_schema_version": old_schema} if old_schema is not None else {}),
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": target_version,
        }
    ]
    corpus._atomic_write_json(output_path, legacy_results)
    corpus._atomic_write_json(
        corpus._checkpoint_path(output_path),
        {
            "checkpoint_version": 2,
            "identity": identity,
            "completed": 1,
            "results_sha256": corpus._json_digest(legacy_results),
        },
    )
    submitted: list[int] = []

    def fake_smoke_one(item, requested_target):
        submitted.append(item["index"])
        return {
            **item,
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": requested_target,
        }

    monkeypatch.setattr(corpus, "_smoke_one", fake_smoke_one)

    corpus.smoke_corpus(manifest_path, output_path, target_version, 2, 0)

    assert sorted(submitted) == [0, 1]
    results = json.loads(output_path.read_text(encoding="utf-8"))
    assert all(
        result["smoke_schema_version"] == corpus.SMOKE_RESULT_SCHEMA_VERSION for result in results
    )


def test_smoke_does_not_resume_when_run_identity_changes(monkeypatch, tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    items = [{"index": index, "repo": "test"} for index in range(2)]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    output_path = tmp_path / "smoke-results.json"
    old_target = "0.4.2"
    identity = corpus._smoke_run_identity(manifest_path, old_target, items)
    corpus._write_smoke_checkpoint(
        output_path,
        [
            {
                **items[0],
                "source_compile": "passed",
                "target_compile": "passed",
                "target_version": old_target,
            }
        ],
        identity,
    )
    submitted: list[int] = []

    def fake_smoke_one(item, requested_target):
        submitted.append(item["index"])
        return {
            **item,
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": requested_target,
        }

    monkeypatch.setattr(corpus, "_smoke_one", fake_smoke_one)

    corpus.smoke_corpus(manifest_path, output_path, "0.4.3", 2, 0)

    assert sorted(submitted) == [0, 1]
    results = json.loads(output_path.read_text(encoding="utf-8"))
    assert {result["target_version"] for result in results} == {"0.4.3"}


def test_smoke_does_not_resume_when_runner_source_changes(monkeypatch, tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    items = [{"index": index, "repo": "test"} for index in range(2)]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    output_path = tmp_path / "smoke-results.json"
    target_version = "0.4.3"
    monkeypatch.setattr(corpus, "_smoke_runner_digest", lambda: "old-runner")
    identity = corpus._smoke_run_identity(manifest_path, target_version, items)
    corpus._write_smoke_checkpoint(
        output_path,
        [
            {
                **items[0],
                "source_compile": "passed",
                "target_compile": "passed",
                "target_version": target_version,
            }
        ],
        identity,
    )
    monkeypatch.setattr(corpus, "_smoke_runner_digest", lambda: "new-runner")
    submitted: list[int] = []

    def fake_smoke_one(item, requested_target):
        submitted.append(item["index"])
        return {
            **item,
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": requested_target,
        }

    monkeypatch.setattr(corpus, "_smoke_one", fake_smoke_one)

    corpus.smoke_corpus(manifest_path, output_path, target_version, 2, 0)

    assert sorted(submitted) == [0, 1]
    checkpoint = json.loads(corpus._checkpoint_path(output_path).read_text(encoding="utf-8"))
    assert checkpoint["identity"]["runner_sha256"] == "new-runner"


def test_smoke_does_not_resume_results_that_are_not_an_ordered_prefix(
    monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    items = [{"index": index, "repo": "test"} for index in range(2)]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    output_path = tmp_path / "smoke-results.json"
    target_version = "0.4.3"
    identity = corpus._smoke_run_identity(manifest_path, target_version, items)
    corpus._write_smoke_checkpoint(
        output_path,
        [
            {
                **items[1],
                "source_compile": "passed",
                "target_compile": "passed",
            }
        ],
        identity,
    )
    submitted: list[int] = []

    def fake_smoke_one(item, requested_target):
        submitted.append(item["index"])
        return {
            **item,
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": requested_target,
        }

    monkeypatch.setattr(corpus, "_smoke_one", fake_smoke_one)

    corpus.smoke_corpus(manifest_path, output_path, target_version, 2, 0)

    assert sorted(submitted) == [0, 1]
    results = json.loads(output_path.read_text(encoding="utf-8"))
    assert [result["index"] for result in results] == [0, 1]


@pytest.mark.parametrize("malformed", ["checkpoint", "output"])
def test_smoke_malformed_resume_state_falls_back_safely(
    malformed: str, monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    items = [{"index": 0, "repo": "test"}]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    output_path = tmp_path / "smoke-results.json"
    target_version = "0.4.3"
    identity = corpus._smoke_run_identity(manifest_path, target_version, items)
    corpus._write_smoke_checkpoint(
        output_path,
        [
            {
                **items[0],
                "source_compile": "passed",
                "target_compile": "passed",
                "target_version": target_version,
            }
        ],
        identity,
    )
    malformed_path = (
        corpus._checkpoint_path(output_path) if malformed == "checkpoint" else output_path
    )
    malformed_path.write_text("{", encoding="utf-8")
    submitted: list[int] = []

    def fake_smoke_one(item, requested_target):
        submitted.append(item["index"])
        return {
            **item,
            "source_compile": "passed",
            "target_compile": "passed",
            "target_version": requested_target,
        }

    monkeypatch.setattr(corpus, "_smoke_one", fake_smoke_one)

    corpus.smoke_corpus(manifest_path, output_path, target_version, 1, 0)

    assert submitted == [0]
    assert json.loads(output_path.read_text(encoding="utf-8"))[0]["index"] == 0
    assert (
        json.loads(corpus._checkpoint_path(output_path).read_text(encoding="utf-8"))["completed"]
        == 1
    )


def test_atomic_json_write_keeps_existing_destination_valid_on_replace_failure(
    monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    destination = tmp_path / "results.json"
    destination.write_text('{"generation": "old"}', encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(corpus.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        corpus._atomic_write_json(destination, {"generation": "new"})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"generation": "old"}
    assert list(tmp_path.iterdir()) == [destination]


def test_atomic_json_write_preserves_existing_destination_mode(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    destination = tmp_path / "results.json"
    destination.write_text('{"generation": "old"}', encoding="utf-8")
    destination.chmod(0o640)

    corpus._atomic_write_json(destination, {"generation": "new"})

    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert json.loads(destination.read_text(encoding="utf-8")) == {"generation": "new"}


def test_atomic_json_write_new_file_uses_normal_umask_mode(tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    destination = tmp_path / "results.json"
    reference = tmp_path / "reference.json"
    previous_umask = corpus.os.umask(0o027)
    try:
        reference.write_text("{}", encoding="utf-8")
        corpus._atomic_write_json(destination, {"generation": "new"})
    finally:
        corpus.os.umask(previous_umask)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert stat.S_IMODE(destination.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)


def test_smoke_uses_manifest_pragma_as_source_version(monkeypatch, tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "old_vyper_bug" / "ethereum" / "0xdef.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("x: uint256\n", encoding="utf-8")
    item = {
        "repo": "old_vyper_bug",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parents[1]),
        "pragma": "0.3.0",
    }
    seen: dict[str, str | None] = {}

    def fake_apply_rules(source, config, path):
        seen["source_version"] = config.source_version
        return SimpleNamespace(source=source, fixes=[], diagnostics=[])

    def fake_compile_source_file(path, config, source_version):
        return CompileResult(status="passed", artifacts={}, stderr=None)

    monkeypatch.setattr(corpus.engine, "apply_rules", fake_apply_rules)
    monkeypatch.setattr(corpus.engine, "compile_source_file", fake_compile_source_file)
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert seen["source_version"] == "0.3.0"
    assert result["source_compile"] == "passed"
    assert result["target_compile"] == "passed"
    assert result["validation_status"] == "blocked"
    assert result["validation_blockers"][0]["code"] == ("target_artifacts_unavailable")
    assert result["validation_waivers"] == []


def test_smoke_records_artifact_diff_details_for_mismatches(monkeypatch, tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity_flat" / "0xabc.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("x: uint256\n", encoding="utf-8")
    item = {
        "repo": "chainsecurity_flat",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
        "pragma": "0.3.10",
    }

    monkeypatch.setattr(
        corpus.engine,
        "compile_source_file",
        lambda path, config, source_version: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )
    monkeypatch.setattr(
        corpus.engine,
        "apply_rules",
        lambda source, config, path: SimpleNamespace(source=source, fixes=[], diagnostics=[]),
    )
    monkeypatch.setattr(
        corpus.engine,
        "compare_artifacts",
        lambda source, target: (False, True, False),
    )
    monkeypatch.setattr(
        corpus.engine,
        "compare_artifact_details",
        lambda source, target: (
            ["changed ABI entry: f(): stateMutability 'view' -> 'nonpayable'"],
            ["changed selector: f() 0x11111111 -> 0x22222222"],
            ["changed storage: x slot 0 uint256 -> 1 uint256"],
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert result["abi_diff"] == ["changed ABI entry: f(): stateMutability 'view' -> 'nonpayable'"]
    assert "method_id_diff" not in result
    assert result["storage_layout_diff"] == ["changed storage: x slot 0 uint256 -> 1 uint256"]


def test_smoke_tries_lowest_broad_pragma_compiler_before_syntax_hint(
    monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity_flat" / "0xabc.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        "#pragma version ^0.3.0\ndecimals: public(uint8)\n",
        encoding="utf-8",
    )
    item = {
        "repo": "chainsecurity_flat",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
        "pragma": "^0.3.0",
    }
    seen: dict[str, object] = {"compile_versions": []}

    def fake_apply_rules(source, config, path):
        seen["rewrite_version"] = config.source_version
        return SimpleNamespace(source=source, fixes=[], diagnostics=[])

    def fake_compile_source_file(path, config, source_version):
        seen["compile_versions"].append(source_version)
        if source_version == "0.3.0":
            return CompileResult(status="failed", artifacts=None, stderr="syntax unavailable")
        return CompileResult(status="passed", artifacts={}, stderr=None)

    monkeypatch.setattr(corpus.engine, "apply_rules", fake_apply_rules)
    monkeypatch.setattr(corpus.engine, "compile_source_file", fake_compile_source_file)
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert seen["compile_versions"] == ["0.3.0", "0.3.1"]
    assert seen["rewrite_version"] == "0.3.1"
    assert result["source_compile"] == "passed"
    assert result["source_compiler"] == "0.3.1"


@pytest.mark.parametrize(
    "body",
    [
        """    assert msg.sender == self._minter
    factor: uint256 = 10000000
    product: uint256 = self._balances[msg.sender] * convert(factor, uint256)
    self._balances[self._minter] = product
""",
        """    factor: uint256 = 10000000
    product: uint256 = self._balances[msg.sender] * convert(factor, uint256)
    if msg.sender == self.owner:
        self._balances[msg.sender] = product
    else:
        self._balances[self.owner] = product
""",
    ],
    ids=["minter", "owner-branch"],
)
def test_smoke_prefers_manifest_compiler_for_same_type_convert_regressions(
    body: str, monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity_flat" / "token.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        """# @version ^0.3.3
NAME: immutable(String[33])
DECIMALS: immutable(uint8)
_balances: HashMap[address, uint256]
_minter: address
owner: public(address)

@external
def multiplyHoldings() -> bool:
"""
        + body
        + "    return True\n",
        encoding="utf-8",
    )
    item = {
        "repo": "chainsecurity_flat",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
        "pragma": "^0.3.3",
        "source_compiler": "0.3.3",
    }
    seen: dict[str, object] = {"compile_versions": []}

    assert corpus._item_source_version(item) == "0.3.3"
    monkeypatch.setattr(corpus, "_item_source_version", lambda _item: "0.3.7")

    def fake_compile_source_file(path, config, source_version):
        seen["compile_versions"].append(source_version)
        status = "passed" if source_version == "0.3.3" else "failed"
        return CompileResult(status=status, artifacts={}, stderr=None)

    def fake_apply_rules(source, config, path):
        seen["rewrite_version"] = config.source_version
        return SimpleNamespace(source=source, fixes=[], diagnostics=[])

    monkeypatch.setattr(corpus.engine, "compile_source_file", fake_compile_source_file)
    monkeypatch.setattr(corpus.engine, "apply_rules", fake_apply_rules)
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert seen["compile_versions"] == ["0.3.3"]
    assert seen["rewrite_version"] == "0.3.3"
    assert result["source_compile"] == "passed"
    assert result["source_compiler_hint"] == "0.3.3"
    assert result["source_compiler"] == "0.3.3"


def test_smoke_reports_winning_source_compiler_instead_of_manifest_hint(
    monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity_flat" / "token.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("# @version ^0.3.3\nx: uint256\n", encoding="utf-8")
    item = {
        "repo": "chainsecurity_flat",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
        "pragma": "^0.3.3",
        "source_compiler": "0.3.3",
    }
    seen: dict[str, object] = {"compile_versions": []}

    monkeypatch.setattr(corpus, "_item_source_version", lambda _item: "0.3.7")

    def fake_compile_source_file(path, config, source_version):
        seen["compile_versions"].append(source_version)
        status = "passed" if source_version == "0.3.4" else "failed"
        return CompileResult(status=status, artifacts={}, stderr=None)

    def fake_apply_rules(source, config, path):
        seen["rewrite_version"] = config.source_version
        return SimpleNamespace(source=source, fixes=[], diagnostics=[])

    monkeypatch.setattr(corpus.engine, "compile_source_file", fake_compile_source_file)
    monkeypatch.setattr(corpus.engine, "apply_rules", fake_apply_rules)
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert seen["compile_versions"] == ["0.3.3", "0.3.7", "0.3.4"]
    assert seen["rewrite_version"] == "0.3.4"
    assert result["source_compile"] == "passed"
    assert result["source_compiler_hint"] == "0.3.3"
    assert result["source_compiler"] == "0.3.4"


def test_smoke_preserves_source_provenance_when_target_validation_raises(
    monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity_flat" / "token.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("# @version 0.3.3\nx: uint256\n", encoding="utf-8")
    item = {
        "repo": "chainsecurity_flat",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
        "pragma": "0.3.3",
        "source_compiler": "0.3.3",
    }

    monkeypatch.setattr(
        corpus.engine,
        "compile_source_file",
        lambda path, config, source_version: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )
    monkeypatch.setattr(
        corpus.engine,
        "apply_rules",
        lambda source, config, path: SimpleNamespace(source=source, fixes=[], diagnostics=[]),
    )

    def fail_target_validation(batch, config):
        raise RuntimeError("target validation crashed")

    monkeypatch.setattr(corpus.engine, "validate_migrations", fail_target_validation)

    result = corpus._smoke_one(item, "0.4.3")

    assert result["source_compiler_hint"] == "0.3.3"
    assert result["source_compiler"] == "0.3.3"
    assert result["source_compile"] == "passed"
    assert result["target_compile"] == "exception"
    assert result["source_error"] is None
    assert result["validation_status"] == "exception"
    assert "target validation crashed" in result["target_error"]


@pytest.mark.parametrize("pragma_case", ["missing", "null", "invalid"])
def test_smoke_uses_valid_manifest_compiler_when_pragma_is_unusable(
    pragma_case: str, monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity_flat" / "token.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("x: uint256\n", encoding="utf-8")
    item = {
        "repo": "chainsecurity_flat",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
        "source_compiler": "0.3.3",
    }
    if pragma_case == "null":
        item["pragma"] = None
    elif pragma_case == "invalid":
        item["pragma"] = "not-a-version"
    compiled: list[str | None] = []

    def fake_compile_source_file(path, config, source_version):
        compiled.append(source_version)
        return CompileResult(status="passed", artifacts={}, stderr=None)

    monkeypatch.setattr(corpus.engine, "compile_source_file", fake_compile_source_file)
    monkeypatch.setattr(
        corpus.engine,
        "apply_rules",
        lambda source, config, path: SimpleNamespace(source=source, fixes=[], diagnostics=[]),
    )
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert compiled == ["0.3.3"]
    assert result["source_compiler_hint"] == "0.3.3"
    assert result["source_compiler"] == "0.3.3"
    assert result["source_compiler"] in compiled


@pytest.mark.parametrize("pragma_case", ["missing", "null", "invalid"])
def test_smoke_fails_closed_without_usable_source_version(
    pragma_case: str, monkeypatch, tmp_path: Path
) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity_flat" / "token.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("x: uint256\n", encoding="utf-8")
    item = {
        "repo": "chainsecurity_flat",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
    }
    if pragma_case == "null":
        item["pragma"] = None
    elif pragma_case == "invalid":
        item["pragma"] = "not-a-version"

    def unexpected_source_compile(path, config, source_version):
        pytest.fail(f"unexpected source compiler: {source_version}")

    monkeypatch.setattr(corpus.engine, "compile_source_file", unexpected_source_compile)
    monkeypatch.setattr(
        corpus.engine,
        "apply_rules",
        lambda source, config, path: SimpleNamespace(source=source, fixes=[], diagnostics=[]),
    )
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert result["source_compiler_hint"] is None
    assert result["source_compiler"] is None
    assert result["source_compile"] == "skipped"
    assert result["validation_status"] == "blocked"
    assert {issue["code"] for issue in result["validation_blockers"]} >= {"source_compile_failed"}


def test_smoke_retries_broad_pragma_with_newer_source_compiler(monkeypatch, tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity_flat" / "0xabc.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("#pragma version ^0.3.0\nx: uint256\n", encoding="utf-8")
    item = {
        "repo": "chainsecurity_flat",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
        "pragma": "^0.3.0",
    }
    seen: dict[str, object] = {"compile_versions": []}

    def fake_compile_source_file(path, config, source_version):
        seen["compile_versions"].append(source_version)
        if source_version == "0.3.0":
            return CompileResult(status="failed", artifacts=None, stderr="old compiler failed")
        return CompileResult(status="passed", artifacts={}, stderr=None)

    def fake_apply_rules(source, config, path):
        seen["rewrite_version"] = config.source_version
        return SimpleNamespace(source=source, fixes=[], diagnostics=[])

    monkeypatch.setattr(corpus.engine, "compile_source_file", fake_compile_source_file)
    monkeypatch.setattr(corpus.engine, "apply_rules", fake_apply_rules)
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert seen["compile_versions"][:2] == ["0.3.0", "0.3.1"]
    assert seen["rewrite_version"] == "0.3.1"
    assert result["source_compile"] == "passed"


def test_smoke_uses_standard_json_compiler_version_for_source(monkeypatch, tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    contract = tmp_path / "contracts" / "chainsecurity" / "main.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("# @version ^0.4.0\nx: uint256\n", encoding="utf-8")
    item = {
        "repo": "chainsecurity",
        "corpus_path": str(contract),
        "corpus_repo_root": str(contract.parent),
        "pragma": "^0.4.0",
        "compiler_version": "v0.4.3+commit.bff19ea2",
        "standard_json": str(tmp_path / "source.json"),
    }
    seen: dict[str, str | None] = {}

    def fake_apply_rules(source, config, path):
        seen["rewrite_version"] = config.source_version
        return SimpleNamespace(source=source, fixes=[], diagnostics=[])

    def fake_compile_source_file(path, config, source_version):
        seen["compile_version"] = source_version
        return CompileResult(status="passed", artifacts={}, stderr=None)

    monkeypatch.setattr(corpus.engine, "apply_rules", fake_apply_rules)
    monkeypatch.setattr(corpus.engine, "compile_source_file", fake_compile_source_file)
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    assert seen["compile_version"] == "0.4.3"
    assert seen["rewrite_version"] == "0.4.3"
    assert result["source_compile"] == "passed"


def test_smoke_uses_standard_json_search_paths(monkeypatch, tmp_path: Path) -> None:
    corpus = _load_corpus_module()
    root = tmp_path / "corpus" / "chainsecurity" / "1_0xabc"
    contract = root / "main.vy"
    module_dir = root / "tests" / "integration" / "network" / "sepolia"
    module_dir.mkdir(parents=True)
    contract.write_text("import module_lib\n", encoding="utf-8")
    standard_json = tmp_path / "export" / "1_0xabc.json"
    standard_json.parent.mkdir()
    standard_json.write_text(
        json.dumps(
            {
                "settings": {
                    "search_paths": [".", "0", "tests/integration/network/sepolia", "../unsafe"]
                }
            }
        ),
        encoding="utf-8",
    )
    item = {
        "repo": "chainsecurity",
        "corpus_path": str(contract),
        "corpus_repo_root": str(root),
        "pragma": "0.4.3",
        "standard_json": str(standard_json),
    }
    seen: dict[str, tuple[Path, ...]] = {}

    def fake_apply_rules(source, config, path):
        seen["rewrite_paths"] = config.compiler_search_paths
        return SimpleNamespace(source=source, fixes=[], diagnostics=[])

    def fake_compile_source_file(path, config, source_version):
        seen["source_paths"] = config.compiler_search_paths
        return CompileResult(status="passed", artifacts={}, stderr=None)

    monkeypatch.setattr(corpus.engine, "apply_rules", fake_apply_rules)
    monkeypatch.setattr(corpus.engine, "compile_source_file", fake_compile_source_file)
    monkeypatch.setattr(
        corpus.engine,
        "compile_target_source",
        lambda path, source, config, overlay: CompileResult(
            status="passed", artifacts={}, stderr=None
        ),
    )

    result = corpus._smoke_one(item, "0.4.3")

    expected = (root, module_dir)
    assert seen["source_paths"] == expected
    assert seen["rewrite_paths"] == expected
    assert result["source_compile"] == "passed"


def test_error_excerpt_keeps_nested_compiler_messages() -> None:
    corpus = _load_corpus_module()
    stderr = """
    vyper.exceptions.VyperException: Compilation failed with the following errors:

    vyper.exceptions.CallViolation: Calls to external view functions must use the `staticcall` keyword.
      contract.vy:42
    """

    excerpt = corpus._error_excerpt(stderr)

    assert "VyperException" in excerpt
    assert "CallViolation" in excerpt


def json_dumps(value: object) -> str:
    return json.dumps(value)
