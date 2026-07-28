# Development

`vyupgrade` is a compiler-backed migration tool for Vyper contracts. Development
work should preserve two guarantees: rewrites are only applied when they are
locally justified, and migrated output is validated against real Vyper compilers
by comparing ABI, method identifiers, and storage layout.

## Setup

Use Python 3.11+ and `uv`.

```bash
uv sync --locked --dev
```

Run commands through `uv run --locked` so development, CI, and release builds use
the same lockfile. Only update `uv.lock` when intentionally changing
requirements in `pyproject.toml`.

## Validation

Run the main validation suite before publishing changes:

```bash
uv run --locked ruff check src tests scripts/corpus.py scripts/release_notes.py scripts/release_preflight.py
uv run --locked pytest
scripts/smoke-wheel.sh
```

Useful focused checks while iterating:

```bash
uv run --locked pytest tests/rule_groups/test_<area>.py
uv run --locked pytest tests/test_versions.py tests/test_docs.py tests/test_rule_registry.py
uv run --locked pytest tests/test_cli.py tests/test_compiler.py tests/test_storage_layout.py
```

`tests/test_cli_integration.py` uses real compiler subprocesses. Run it when
changing compiler commands, target overlays, output formats, pragma rewriting,
compiler dependency inference, or supported Vyper versions.

## Architecture

The CLI flow is:

1. `cli.main()` loads command-line and `[tool.vyupgrade]` config.
2. `project.discover_files()` finds `.vy` and `.vyi` inputs.
3. `engine.prepare_migrations()` compiles each source under the inferred or
   provided source compiler, gives each file its own source AST-backed config,
   and calls `rules.apply_rules()`.
4. `rules.apply_rules()` constructs a `MigrationContext`, then runs the ordered
   rule pipeline from `RULES`.
5. Optional interface splitting generates sibling `.vyi` files when
   `--split-interfaces` is enabled and `VY120` is active.
6. `write_plan.MigrationPlan` resolves every destination, rejects duplicate
   generated outputs and pre-existing generated-file collisions, and records
   original and candidate SHA-256 hashes.
7. `engine.validate_migrations()` builds a temporary target overlay, directly
   validates generated interfaces, compiles migrated sources under the target
   compiler, compares ABI, method identifiers, and storage layout, and returns
   the typed fail-closed decision from `validation.decide_run_validation()`.
   Rule selection and diagnostic version gating do not participate in this
   safety decision.
8. Optional formatting runs only on temporary candidate files. The exact
   formatted bytes replace the candidates and pass through target validation
   again before writes are allowed.
9. The write plan stages every destination and commits it as one rollback-aware
    transaction only when validation passes or every blocker has an explicit
    waiver. A post-write test failure is reported and exits nonzero, but does not
    roll back the write or any external side effects from the test command.

The write transaction rechecks every planned source and no-op dependency before
commit and checks each changed destination again immediately before replacement.
Portable POSIX filesystems do not expose a content compare-and-swap rename, so an
external writer that ignores coordination can still race the final check. Existing
files are copied to staging with `copy2`, permission modes are retained, and changed
hard-linked or read-only files are rejected. Platform-specific ownership, ACL, flag,
or extended-attribute behavior remains filesystem dependent. An incomplete rollback
is reported explicitly with final on-disk hashes instead of being described as clean.

Important files:

- `src/vyupgrade/engine.py` owns compiler-attempt selection, per-file AST-backed
  rewrites, coherent target-overlay validation, artifact comparisons, and typed
  validation decisions shared by the CLI and corpus smoke tool.
- `src/vyupgrade/rules.py` defines rule order and `RULE_CHANGES`.
- `src/vyupgrade/rule_registry.py` defines `Rule`, `RuleContext`, and gating.
- `src/vyupgrade/rule_groups/` contains the actual migration rules.
- `src/vyupgrade/versions.py` owns supported Vyper versions and spec resolution.
- `src/vyupgrade/compiler.py` owns compiler subprocesses, temporary overlays,
  dependency inference, and ABI and method-identifier comparisons. Each
  uv-managed compiler environment is provisioned once per process before its
  first compile, so downloads are bounded by the network timeout rather than
  the compile timeout.
- `src/vyupgrade/storage_layout.py` owns fail-closed storage artifact parsing,
  canonicalization, target-AST evidence, and typed layout comparison.
- `src/vyupgrade/write_plan.py` owns destination collision checks, candidate
  hashes, staged replacements, and rollback on partial write failure.
- `src/vyupgrade/analysis.py` extracts lightweight source facts for type-aware
  rules.
- `src/vyupgrade/ast_facts.py` extracts facts from compiler AST output.
- `docs/vyper-syntax-history.md` records source-visible upstream syntax changes.
- `docs/migration-coverage.md` records this project's behavior for each change.

Storage-layout canonicalization is fail closed: wrapper and leaf keys are
whitelisted, persistent and transient spans may not overlap, and explicit
`n_slots` values must agree with widths derivable from canonical Vyper types.
The inferred set is scalars and hash maps (one slot); fixed arrays (element
width times length); dynamic arrays (one length slot plus the maximum element
span); and bounded `Bytes`/`String` values (one length slot plus the rounded-up
byte capacity). Interfaces count as one slot only when the raw compiler type
spells `interface Name` or contains a grammar-delimited `.vyi` basename. Naming
convention and familiar built-in names are not evidence: `IFoo` and `ERC20` may
both be multi-slot structs. Canonicalization retains that interface marker.
Pairwise comparison may ignore a source marker omitted by the target compiler
only when the target compiler AST proves that the same top-level storage
variable annotation resolves that exact grammar path and name to a direct-root
`InterfaceDef`. Missing or malformed AST, imported attributes, and flattened
nested-module keys fail closed. The recursive type grammar, identifiers, slots,
and widths must also match, so structs remain distinct. No `IERC*`/`ERC*`
spelling aliases are applied. Extensionless and `.vy` paths are
preserved in full and remain width-unknown because neither suffix proves an
interface. A bare user-defined type does not carry enough information to infer
a width. Such an unknown width never matches a width supplied on only one side;
two otherwise identical legacy layouts that both omit it remain comparable,
while their occupied span beyond the compiler-reported start slot is not
independently provable.

JSON reports use the existing top-level envelope with `schema_version: 2`.
Schema 2 adds per-file roles and the top-level closure report while preserving
the schema 1 fields. Within a schema version, fields may be added but existing
fields are not renamed, removed, or type-changed. A missing version identifies
the legacy unversioned format; incompatible changes require a new schema
version.

## Rule model

A rule runner has this shape:

```python
def _some_rule(context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    ...
```

Register it with a descriptor:

```python
Rule("some_rule", runner=_some_rule, changes=(crossing("VY123", "0.4.4"),))
```

Activation helpers:

- `crossing(code, version)` runs when `source_floor < version <= target` and is
  the default for historical syntax migrations.
- `target_floor(code, version)` runs whenever the requested target is at least
  that version. Use it for legacy cleanup rules whose source syntax can still
  appear under broad, unknown, or already-modern pragmas.
- `target_update(code, version)` is currently gated like `target_floor`, but is
  used to communicate target-directed updates such as pragma changes.

Fixes and diagnostics use stable rule codes:

- `Fix("VY###", line, message, before, after)` for automated rewrites.
- `Diagnostic("VYD###", line, message, severity="warning")` for manual review or
  validation findings. Use `severity="error"` only for hard blockers.

All emitted codes must appear in a `Rule(..., changes=...)` descriptor included
in `RULES`. The docs and registry tests intentionally fail when a code is not
version-gated or not documented.

## Writing safe migrations

Prefer small, syntax-preserving edits. A migration should be idempotent and
should not change comments, strings, or docstrings unless that is the point of
the rule.

Use existing helpers:

- `context.code_mask`, `code_mask()`, and `span_is_code()` to skip comments and
  string literals.
- `TextEdit` and `apply_edits()` for multi-edit rewrites.
- `innermost_non_overlapping()` when nested matches may overlap.
- `line_number()` for fix/diagnostic locations.
- `split_top_level_args()` and `split_top_level_arg_spans()` for argument lists.
- `find_matching()` and `find_matching_open()` for balanced delimiters.
- `insert_import()` for new imports.
- `context.facts` for interfaces, structs, global variables, storage variables,
  function decorators, function return types, loop variables, and imported
  built-in interface facts.
- `config.source_ast` and `ast_facts.py` when a rule needs compiler AST spans or
  parsed constants.

Use diagnostics instead of rewrites when the safe target spelling depends on
runtime behavior, external project context, user intent, or a semantic choice.
If an intentionally behavior-changing rewrite is valuable, require
`config.aggressive` and document it clearly in migration coverage.

## Adding support for a new Vyper version

1. **Collect source-visible changes.** Read the upstream Vyper release notes and
   linked PRs. Track changes to source syntax and spelling: pragmas, decorators,
   declarations, imports, interfaces, type names, builtin names or signatures,
   call syntax, literals, and newly accepted forms. Ignore backend-only,
   optimizer-only, ABI-layout-only, EVM-default-only, and CLI-only changes unless
   source text or validation behavior must change.
2. **Update syntax history.** Add a heading to `docs/vyper-syntax-history.md`
   with short before/after examples for each source-visible change.
3. **Update version support.** In `src/vyupgrade/versions.py`, add opt-in alpha
   releases to `ALPHA_RELEASE_VERSIONS` or extend the final-release ranges in
   `KNOWN_VERSIONS` and `SUPPORTED_RELEASE_VERSIONS`. Update
   `default_evm_version()` if Vyper's default EVM changed. Update
   `_source_syntax_floor()` when broad pragmas need a newer compiler to parse a
   newly introduced syntax form.
4. **Decide the default target.** Alpha targets should remain opt-in. When a new
   final release becomes the intended default, update every default and example
   together: `Config.target_version`, `cli.py`, README, docs, tests, and
   configuration snippets.
5. **Classify coverage.** Update `docs/migration-coverage.md` for every new
   syntax-history entry. Each item should say automated rewrite, diagnostic,
   no-op, or validation-only. This document must not use tables and must not
   contain unresolved gap wording such as TODO.
6. **Implement rules.** Put the implementation in the closest existing
   `rule_groups` module or create a new focused module. Import it from
   `rules.py` and place it where earlier/later rule assumptions remain valid.
   For example, pragma and legacy syntax rules run early, interface facts should
   be normalized before external-call inference, numeric rewrites run before
   late cleanup, and validation metadata rules remain last.
7. **Add tests.** Cover successful rewrite output, diagnostics for ambiguous
   cases, comments/strings/docstrings, version gating, idempotence, and compiler
   validation where relevant. Add or update `tests/test_versions.py` for new
   version ranges, source syntax floors, and EVM defaults.
8. **Smoke real contracts.** For broad changes, run a corpus smoke through
   `scripts/corpus.py smoke` against a representative manifest and inspect the
   rule/diagnostic summary plus artifact diffs.
9. **Update user-facing docs.** Update README support statements, options or
   examples, and `CHANGELOG.md`.
10. **Run full validation.** Run lint, full pytest, and `scripts/smoke-wheel.sh`.

## Corpus tooling

`scripts/corpus.py` can build, import, dedupe, and smoke-test corpora. The
important maintainer command is the smoke runner:

```bash
uv run --locked python scripts/corpus.py smoke \
  --manifest corpus/vyper/deduped-manifest.json \
  --output corpus/vyper/smoke-results.json \
  --target-version 0.4.3 \
  --workers 4
```

Use `--limit` for a quick sample and `--path` to focus on known regressions. The
smoke command compiles source and target outputs, applies the same artifact
comparisons as the CLI, and records rule, diagnostic, compile, and diff details.
Each row records the compiler that actually won source validation as
`source_compiler`; `source_compiler_hint` preserves the compiler recorded by the
input manifest when a fallback compiler wins.
It writes an atomic checkpoint sidecar and resumes an ordered result prefix when
the manifest, selected items, target version, smoke-result schema, and runner-source
fingerprint still match the interrupted run. Each result and summary declares
`smoke_schema_version: 3`; rows from earlier schemas are never mixed into a
resumed schema 3 run. Schema 3 summaries retain the raw compiler
`status_pairs`, and also expose `normalized_status_pairs`, where a safe
`degraded` source compile is grouped with `passed`. `validation_statuses`,
`validation_blockers`, and `validation_waivers` count the typed safety outcomes
and issue codes. Failed repository, compiler, and error rankings include only
blocked validations and runner exceptions; a non-blocking degraded source
compile is not classified as a failure. Error rankings additionally exclude
stderr from compile sides that passed (or safely degraded for source), so
warnings attached to an artifact-change blocker are not mislabeled as errors.

Corpus source directories and generated outputs live under `corpus/`, which is
ignored by Git.

## Release process

Publishing uses GitHub Actions and PyPI Trusted Publishing. The publish workflow
runs on `v*` tags, builds the package, verifies the tag, project and lockfile
versions, changelog notes, and distribution metadata before publishing to PyPI,
then creates or updates the GitHub release with the preflighted notes.

Before tagging:

1. Update `pyproject.toml` version.
2. Add a matching `CHANGELOG.md` section. `scripts/release_notes.py` expects a
   heading such as `## 0.4.2 - YYYY-MM-DD` for tag `v0.4.2`.
3. Run:

   ```bash
   uv run --locked ruff check src tests scripts/corpus.py scripts/release_notes.py scripts/release_preflight.py
   uv run --locked pytest
   scripts/smoke-wheel.sh
   uv run --locked python scripts/release_preflight.py v0.4.2 --dist dist
   ```

4. Tag and push:

   ```bash
   git tag v0.4.2
   git push origin v0.4.2
   ```

The PyPI trusted publisher should be configured for repository
`vyperlang/vyupgrade`, workflow `publish.yml`, and environment `pypi`.
