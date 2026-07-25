from __future__ import annotations

import re
from pathlib import Path

from .analysis import SourceFacts, indexed_value_type
from .models import Config, Fix
from .source import (
    TextEdit,
    apply_edits,
    code_mask,
    line_number,
    line_starts_in_code,
    span_is_code,
)
from .versions import MigrationContext, VyperVersion


def line_match_starts_outside_string(source: str, mask: list[bool], start: int) -> bool:
    return line_starts_in_code(source, mask, start)


def pre_021_context(context: MigrationContext) -> bool:
    return context.source_floor is None or context.source_floor < VyperVersion("0.2.1")


def innermost_non_overlapping(
    edits: list[TextEdit], fixes: list[Fix]
) -> tuple[list[TextEdit], list[Fix]]:
    selected: list[tuple[TextEdit, Fix]] = []
    for edit, fix in sorted(
        zip(edits, fixes, strict=True),
        key=lambda item: (item[0].end - item[0].start, item[0].start),
    ):
        if any(edit.start < kept.end and kept.start < edit.end for kept, _fix in selected):
            continue
        selected.append((edit, fix))
    selected.sort(key=lambda item: item[0].start)
    return [edit for edit, _fix in selected], [fix for _edit, fix in selected]


def line_offsets(source: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", source):
        offsets.append(match.end())
    return offsets


def strip_arg_comments(raw_args: str) -> str:
    lines: list[str] = []
    for raw_line in raw_args.splitlines():
        line = raw_line
        mask = code_mask(line)
        comment_start = next(
            (
                index
                for index, char in enumerate(line)
                if char == "#" and (index == 0 or mask[index - 1])
            ),
            None,
        )
        if comment_start is not None:
            line = line[:comment_start]
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def has_line_comment(text: str) -> bool:
    for line in text.splitlines():
        mask = code_mask(line)
        if any(char == "#" and (index == 0 or mask[index - 1]) for index, char in enumerate(line)):
            return True
    return False


def function_start_at_line(facts: SourceFacts, line_no: int) -> int | None:
    for start, end in sorted(facts.function_ends.items()):
        if start <= line_no <= end:
            return start
    return None


def function_body_span(
    source: str,
    line_offsets: list[int],
    facts: SourceFacts,
    function_line: int,
) -> tuple[int, int]:
    start = line_offsets[function_line] if function_line < len(line_offsets) else len(source)
    end_line = facts.function_ends.get(function_line, len(line_offsets))
    end = line_offsets[end_line] if end_line < len(line_offsets) else len(source)
    return start, end


def is_attribute_name(source: str, start: int) -> bool:
    i = start - 1
    while i >= 0 and source[i].isspace() and source[i] != "\n":
        i -= 1
    return i >= 0 and source[i] == "."


def is_keyword_argument_name(source: str, start: int, end: int) -> bool:
    i = end
    while i < len(source) and source[i].isspace() and source[i] != "\n":
        i += 1
    if i >= len(source) or source[i] != "=":
        return False
    j = start - 1
    while j >= 0 and source[j].isspace():
        j -= 1
    return j >= 0 and source[j] in "(,{"


def insert_import(source: str, line: str) -> str:
    lines = source.splitlines(keepends=True)
    insert_at = _import_prelude_end(source, lines)
    while insert_at < len(lines) and lines[insert_at].startswith("import "):
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].startswith("from "):
        insert_at += 1
    lines.insert(insert_at, line)
    return "".join(lines)


def _import_prelude_end(source: str, lines: list[str]) -> int:
    offsets: list[int] = []
    offset = 0
    for current in lines:
        offsets.append(offset)
        offset += len(current)

    insert_at = 0
    while insert_at < len(lines):
        stripped = lines[insert_at].strip()
        if stripped and not stripped.startswith("#"):
            break
        insert_at += 1

    if insert_at < len(lines):
        line = lines[insert_at]
        leading = len(line) - len(line.lstrip())
        start = offsets[insert_at] + leading
        if source.startswith(('"""', "'''"), start):
            mask = code_mask(source)
            end = start + 3
            while end < len(source) and not mask[end]:
                end += 1
            while insert_at < len(lines) and offsets[insert_at] <= end:
                insert_at += 1
            while insert_at < len(lines) and not lines[insert_at].strip():
                insert_at += 1

    return insert_at


def remove_constructor_decorators(
    source: str,
    decorators_to_remove: set[str],
    rule: str,
    message: str,
    add_deploy: bool = False,
) -> tuple[str, list[Fix], list[Fix]]:
    lines = source.splitlines(keepends=True)
    mask = code_mask(source)
    offsets = line_offsets(source)
    fixes: list[Fix] = []
    insertions: list[Fix] = []
    out = list(lines)
    offset = 0
    for index, line in enumerate(lines):
        if not re.match(r"\s*def\s+__init__\s*\(", line) or not line_starts_in_code(
            source, mask, offsets[index]
        ):
            continue
        start = index
        while (
            start > 0
            and re.match(
                r"\s*@[A-Za-z_][A-Za-z0-9_]*(?:\(.*\))?\s*(?:#.*)?$",
                lines[start - 1],
            )
            and line_starts_in_code(source, mask, offsets[start - 1])
        ):
            start -= 1
        decorators = [decor.strip() for decor in lines[start:index]]
        insert_at = start + offset
        remove_indices: list[int] = []
        has_deploy = any(decor.startswith("@deploy") for decor in decorators)
        for rel, decor in enumerate(decorators):
            decor_name = decor.split("(", 1)[0].split("#", 1)[0].strip()
            if decor_name in decorators_to_remove:
                remove_indices.append(start + rel)
        for original_index in sorted(remove_indices, reverse=True):
            before = out[original_index + offset].rstrip("\n")
            del out[original_index + offset]
            offset -= 1
            fixes.append(Fix(rule, original_index + 1, message, before, ""))
        if add_deploy and not has_deploy:
            indent = re.match(r"(\s*)", line).group(1)
            out.insert(insert_at, f"{indent}@deploy\n")
            offset += 1
            insertions.append(
                Fix(rule, index + 1, "added @deploy to constructor", "", f"{indent}@deploy")
            )
    return "".join(out), fixes, insertions


def nested_under_config_path(path: Path, config: Config) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in config.paths:
        try:
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return resolved.parent != root.resolve()
    return path.parent != Path(".")


def replace_identifier_expr(
    source: str,
    before: str,
    after: str,
    rule: str,
    message: str,
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(rf"(?<![\w.]){re.escape(before)}(?![\w.])", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(Fix(rule, line_number(source, match.start()), message, before, after))
    return apply_edits(source, edits), fixes


def find_matching_open(
    source: str, close_index: int, open_char: str = "(", close_char: str = ")"
) -> int | None:
    depth = 0
    for index in range(close_index, -1, -1):
        char = source[index]
        if char == close_char:
            depth += 1
        elif char == open_char:
            depth -= 1
            if depth == 0:
                return index
    return None


def literal_integer(value: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:\d|_)+\s*", value))


def lhs_declared_type(line: str) -> str | None:
    match = re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*([^=]+)=", line)
    return match.group(1).strip() if match else None


def lhs_assigned_type(line: str, vars_for_line: dict[str, str]) -> str | None:
    match = re.match(
        r"\s*(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)(\s*\[[^=]+\])?\s*(?://=|[-+*/%]?=)", line
    )
    if not match:
        return None
    type_name = vars_for_line.get(match.group(1))
    if match.group(2):
        return indexed_value_type(type_name)
    return type_name
