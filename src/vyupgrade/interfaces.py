from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import Fix, GeneratedFile
from .rule_helpers import insert_import
from .source import TextEdit, apply_edits, code_mask, find_matching, line_number, span_is_code


@dataclass(frozen=True)
class InterfaceSplitResult:
    source: str
    generated: tuple[GeneratedFile, ...]
    fixes: tuple[Fix, ...]


def split_interfaces_to_vyi(source: str, path: Path) -> InterfaceSplitResult:
    blocks = _top_level_interface_blocks(source)
    if not blocks:
        return InterfaceSplitResult(source, (), ())

    edits: list[TextEdit] = []
    generated: list[GeneratedFile] = []
    fixes: list[Fix] = []
    import_lines: list[str] = []
    existing_imports = _existing_imports(source)

    for block in blocks:
        vyi_source = _interface_body_to_vyi(block.body)
        vyi_path = path.with_name(f"{block.name}.vyi")
        fix = Fix(
            "VY120",
            line_number(source, block.start),
            f"moved interface {block.name} to {vyi_path.name}",
            source[block.start : block.end].rstrip("\n"),
            vyi_source.rstrip("\n"),
        )
        generated.append(GeneratedFile(vyi_path, vyi_source, fix))
        fixes.append(fix)
        edits.append(TextEdit(block.start, block.end, ""))
        if block.name not in existing_imports:
            import_lines.append(f"import {block.name}\n")
            existing_imports.add(block.name)

    rewritten = apply_edits(source, edits)
    if import_lines:
        rewritten = _insert_imports(rewritten, import_lines)
    rewritten = re.sub(r"\n{3,}", "\n\n", rewritten)
    return InterfaceSplitResult(rewritten, tuple(generated), tuple(fixes))


@dataclass(frozen=True)
class _InterfaceBlock:
    name: str
    start: int
    end: int
    body: str


def _top_level_interface_blocks(source: str) -> list[_InterfaceBlock]:
    mask = code_mask(source)
    offsets = _line_offsets(source)
    lines = source.splitlines(keepends=True)
    blocks: list[_InterfaceBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        start = offsets[index]
        match = re.match(
            r"interface[ \t]+([A-Za-z_][A-Za-z0-9_]*)(?:[ \t]*\([^)]*\))?[ \t]*:[ \t]*(?:#.*)?(?:\n)?$",
            line,
        )
        if match is None or not span_is_code(mask, start, start + match.end(1)):
            index += 1
            continue
        body_start_index = index + 1
        end_index = body_start_index
        while end_index < len(lines):
            candidate = lines[end_index]
            stripped = candidate.strip()
            if not stripped:
                end_index += 1
                continue
            indent = len(candidate) - len(candidate.lstrip(" \t"))
            if indent == 0:
                break
            end_index += 1
        end = offsets[end_index] if end_index < len(offsets) else len(source)
        body_start = offsets[body_start_index] if body_start_index < len(offsets) else end
        blocks.append(_InterfaceBlock(match.group(1), start, end, source[body_start:end]))
        index = end_index
    return blocks


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", source):
        offsets.append(match.end())
    return offsets


def _existing_imports(source: str) -> set[str]:
    names: set[str] = set()
    mask = code_mask(source)
    for match in re.finditer(r"^[ \t]*import[ \t]+(.+)$", source, re.MULTILINE):
        if not span_is_code(mask, match.start(), match.start(1)):
            continue
        for part in match.group(1).split(","):
            module, _sep, alias = part.split("#", 1)[0].strip().partition(" as ")
            bound_name = alias.strip() if alias else module.split(".", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bound_name):
                names.add(bound_name)
    return names


def _insert_imports(source: str, imports: list[str]) -> str:
    for line in imports:
        source = insert_import(source, line)
    return source


def _interface_body_to_vyi(body: str) -> str:
    lines = _dedent_interface_body(body).splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(line)
            index += 1
            continue
        if not stripped.startswith("def "):
            output.append(line)
            index += 1
            continue

        header_lines = [line]
        index += 1
        while index < len(lines) and not _interface_def_header_complete("".join(header_lines)):
            header_lines.append(lines[index])
            index += 1
        mutability = _interface_def_mutability("".join(header_lines))
        if (
            mutability is None
            and index < len(lines)
            and lines[index].strip()
            in {
                "view",
                "pure",
                "payable",
                "nonpayable",
            }
        ):
            mutability = lines[index].strip()
            index += 1
        stub = _interface_def_stub("".join(header_lines), mutability)
        output.extend(_interface_stub_lines(stub, mutability))
    return "".join(output).strip() + "\n"


def _dedent_interface_body(body: str) -> str:
    lines = body.splitlines(keepends=True)
    indents = [len(line) - len(line.lstrip(" \t")) for line in lines if line.strip()]
    width = min(indents) if indents else 0
    return "".join(line[width:] if len(line) >= width else line for line in lines)


def _interface_def_header_complete(header: str) -> bool:
    stripped = header.strip()
    if not stripped:
        return False
    open_index = header.find("(")
    close_index = find_matching(header, open_index) if open_index != -1 else None
    return close_index is not None and bool(
        re.search(r":[ \t]*(?:view|pure|payable|nonpayable)?[ \t]*(?:#.*)?$", stripped)
    )


def _interface_def_mutability(header: str) -> str | None:
    match = re.search(r":[ \t]*(view|pure|payable|nonpayable)[ \t]*(?:#.*)?$", header.strip())
    return match.group(1) if match else None


def _interface_def_stub(header: str, mutability: str | None) -> str:
    if mutability is not None:
        return re.sub(
            rf":[ \t]*{re.escape(mutability)}([ \t]*(?:#.*)?)$",
            r": ...\1",
            header.rstrip("\n"),
        )
    return re.sub(r":[ \t]*(?:#.*)?$", ": ...", header.rstrip("\n"))


def _interface_stub_lines(stub: str, mutability: str | None) -> list[str]:
    decorators = []
    if mutability in {"view", "pure", "payable"}:
        decorators.append(f"@{mutability}\n")
    decorators.append("@external\n")
    return [*decorators, stub + "\n"]
