# Changelog

## Unreleased

- Made source validation use the nearest declared project environment and
  removed source-validation import guessing and retry behavior.
- Replaced JSON report schema v3 with v4, which records producer identity and
  distinct source and target attestations: the declared snapshot and compiler
  declarations, compiler authority and executable/artifact identity, manifest,
  lock, resolved packages and sources, process completion and exit status,
  validated sources, exact compile attempt, typed failure origin, and compiler
  output on failure.

## 0.6.0 - 2026-07-16

- Reject source version specifications that match no compiler supported by the
  current release instead of attempting an unvalidated target migration.
- Made `sqrt`/`isqrt` math imports module-docstring-safe and collision-free by
  reusing existing builtin imports or choosing a deterministic fresh alias.
- Added a public import-closure primitive (resolve_import_closure) and a reusable overlay materializer, single-sourcing the validation dependency traversal.
- Added opt-in closure-mode overlay materialization: the full import closure (including external search-path dependencies) can be materialized import-root-relative for dependency-closure upgrades.
- Added opt-in `--include-dependencies` closure upgrading: the full import closure (including installed search-path dependencies) is rewritten and cross-validated in a closure-mode target overlay; JSON schema v2 adds file roles and a closure block; in-place dependency writes are structurally impossible.
- Added `--closure-output DIR`: materializes the validated closure — including installed search-path dependencies — as a standalone import-root-relative tree, gated on the validation decision, with a structural guarantee against in-place writes.
- Added `--closure-archive OUT.vyz`: compiles the validated closure — including upgraded search-path dependencies — to a self-contained Vyper archive with the real target compiler, gated on archive-capable targets (>= 0.4.0).

## 0.5.1 - 2026-07-10

- Extracted fail-closed storage-layout parsing, canonicalization, AST evidence,
  and comparison into a dedicated typed module without changing validation
  behavior or compiler-facing APIs.

## 0.5.0 - 2026-07-10

- Made compiler-backed validation fail closed with typed blockers for missing,
  malformed, or changed ABI, method identifier, and storage-layout artifacts.
- Made multi-file writes transactional, including destination-collision checks,
  generated-file validation, rollback on failure, and dry-run/report parity.
- Moved migration preparation, validation, reporting, and write planning into
  shared engines so CLI modes use one pipeline with per-file configuration.
- Hardened source rewrites against comments, strings, docstrings, overlapping
  edits, unsafe nonreentrant changes, and ambiguous numeric or interface cases.
- Hardened storage-layout comparison across legacy flat layouts, modern nested
  modules, transient and code layouts, path-qualified interface types, and
  compiler-reported slot spans while keeping unprovable widths fail closed.
- Added resumable, fingerprinted corpus runs with truthful source-compiler
  provenance, schema-versioned results, normalized status summaries, and
  compiler fallback ordering that respects historical manifest metadata.
- Added release preflight checks for tags, changelog notes, lock metadata, and
  built distributions, and expanded corpus-backed regression coverage for
  NatSpec cleanup, interface getters, numeric casts, and composed storage types.

## 0.4.3 - 2026-07-08

- Fixed source-version inference so version specs are matched with the
  compiler's own PEP 440 pragma semantics; prerelease targets such as
  `0.5.0a3` are no longer selected as the source validation compiler for
  stable pragmas like `^0.4.2`.

## 0.4.2 - 2026-07-07

- Added opt-in support for targeting Vyper `0.5.0a3`; custom error syntax is
  treated as newly accepted source and broad alpha pragmas compile with `a3`
  when needed.
- Added a range diagnostic for leading loop sentinel `if`/`break` blocks, with
  an aggressive-mode migration to collapse them into `range(..., bound=...)`
  when the bounded stop is inferable.

## 0.4.1 - 2026-06-06

- Fixed standard-json corpus imports so only declared compilation targets are
  smoked while helper package modules remain available for dependency
  resolution.
- Fixed target validation for multiline function NatSpec docstrings,
  package-local helper imports, and decimal-enabled math imports.
- Added a migration rewrite for non-ASCII string literal characters that modern
  target compilers reject.
- Kept confirmed decimal selector changes surfaced as method identifier diffs.

## 0.4.0 - 2026-06-02

- Added corpus import tooling and targeted corpus smoke selection for repeatable large-corpus regression checks.
- Fixed broad migration failures found in large-corpus validation, including overlapping bitwise rewrites, integer division detection, signedness casts, external-call inference, struct literal ordering, NatSpec validation cleanup, and dependency import resolution.
- Improved source and target compiler validation for standard-json packages, overlay import trees, broad version pragmas, old compiler span crashes, and common dependency hints.
- Normalized noisy ABI, method identifier, and storage layout metadata so corpus reports focus on meaningful behavior changes.
- Preserved legacy public fixed-array getter selectors and legacy storage/interface type surfaces when comparing upgraded artifacts.
- Kept storage layout movements surfaced by default, including nonreentrant transient-storage moves, instead of inserting compatibility gaps automatically.

## 0.3.1 - 2026-05-31

- Fixed `--format mamushi` write runs so missing, timed out, or failing formatter executions are captured in report output instead of crashing.

## 0.3.0 - 2026-05-30

- Added opt-in support for targeting Vyper `0.5.0a1` and `0.5.0a2`.
- Added rewrites for `isqrt`, duplicate or repeated `implements` declarations, concrete interface defaults, and docstring-only function bodies when migrating to the alpha targets.
- Added an error diagnostic when the inferred source version is newer than the requested target.
- Moved rule gating into descriptors and split rule implementations and tests by rule group for easier maintenance.
- Isolated compiler-backed CLI tests from fast CLI tests to keep the default suite quick while preserving real compiler coverage.

## 0.2.0 - 2026-05-29

- Added support for validating and migrating older Vyper beta-era contracts.
- Added optional interface splitting into generated `.vyi` files.
- Added colored CLI diffs and more detailed ABI, method identifier, storage layout, and EVM default diagnostics.
- Improved target validation coverage across Curve, Yearn, and Yield Basis corpora.
- Fixed additional rewrites for external calls, shifted `method_id()` values, signed hashmap keys, pure/view interactions, blueprint offsets, legacy ERC interface facts, and older syntax forms.
- Hardened reporting, compiler command preparation, corpus tooling, and rule-code coverage checks.

## 0.1.0 - 2026-05-29

- Initial public release of `vyupgrade`.
- Supports compiler-backed Vyper migrations from `0.2.1` through `0.4.3`.
- Validates rewritten contracts by compiling source and target outputs and comparing ABI, method identifiers, and storage layout.
- Includes focused rewrites for legacy syntax, external call keywords, integer division, typed loops, nonreentrant locks, built-in interface imports, and manual-review diagnostics.
