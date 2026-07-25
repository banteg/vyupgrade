from __future__ import annotations

import re

from ..analysis import SourceFacts
from ..models import Diagnostic, Fix
from .legacy_call_helpers import iter_calls, replace_identifier_call
from ..rule_helpers import (
    function_body_span as _function_body_span,
    function_start_at_line as _function_start_at_line,
    strip_arg_comments as _strip_arg_comments,
    is_attribute_name as _is_attribute_name,
    is_keyword_argument_name as _is_keyword_argument_name,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    pre_021_context as _pre_021_context,
    remove_constructor_decorators as _remove_constructor_decorators,
)
from ..rule_registry import (
    Rule,
    RuleContext,
    crossing,
    target_floor,
    target_update,
)
from ..source import (
    TextEdit,
    apply_edits,
    code_mask,
    code_identifiers,
    find_matching,
    line_number,
    split_top_level_args,
    span_is_code,
)
from ..versions import VyperVersion


def _pragma(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    config = rule_context.config
    if config.strip_pragma:
        return _strip_version_pragmas(rule_context)

    fixes: list[Fix] = []
    mask = rule_context.code_mask
    pattern = re.compile(
        r"^([ \t]*)#[ \t]*(?:@version|pragma[ \t]+version)[ \t]+(.+?)[ \t]*$", re.MULTILINE
    )
    matched = False

    def repl(match: re.Match[str]) -> str:
        nonlocal matched
        if not _line_match_starts_outside_string(source, mask, match.start()):
            return match.group(0)
        matched = True
        before = match.group(0)
        after = f"{match.group(1)}#pragma version {config.target_version}"
        if before != after:
            fixes.append(
                Fix(
                    "VY001",
                    line_number(source, match.start()),
                    "modernized version pragma",
                    before,
                    after,
                )
            )
        return after

    rewritten = pattern.sub(repl, source)
    if matched:
        return rewritten, fixes, []
    pragma = f"#pragma version {config.target_version}\n"
    fixes.append(Fix("VY001", 1, "added version pragma", "", pragma.rstrip()))
    return pragma + rewritten, fixes, []


def _strip_version_pragmas(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    mask = rule_context.code_mask
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    pattern = re.compile(
        r"^[ \t]*#[ \t]*(?:@version|pragma[ \t]+version)[ \t]+[^\r\n]*"
        r"(?:\r\n|\n|\r|$)",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if not _line_match_starts_outside_string(source, mask, match.start()):
            continue
        before = match.group(0).rstrip("\r\n")
        edits.append(TextEdit(match.start(), match.end(), ""))
        fixes.append(
            Fix(
                "VY001",
                line_number(source, match.start()),
                "removed version pragma",
                before,
                "",
            )
        )
    return apply_edits(source, edits), fixes, []


def _legacy_decorators(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    replacements = {
        "public": "external",
        "private": "internal",
        "constant": "view",
    }
    mask = rule_context.code_mask
    pattern = re.compile(r"^([ \t]*)@(public|private|constant)([ \t]*(?:#.*)?$)", re.MULTILINE)

    def repl(match: re.Match[str]) -> str:
        if not _line_match_starts_outside_string(source, mask, match.start()):
            return match.group(0)
        before = match.group(0)
        after = f"{match.group(1)}@{replacements[match.group(2)]}{match.group(3)}"
        fixes.append(
            Fix(
                "VY201",
                line_number(source, match.start()),
                "renamed legacy decorator",
                before,
                after,
            )
        )
        return after

    return pattern.sub(repl, source), fixes, []


def _legacy_type_units(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    type_re = re.compile(
        r"\b(u?int(?:8|16|32|64|128|256)?|decimal)\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)"
    )
    for match in type_re.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        before = match.group(0)
        after = match.group(1)
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY202",
                line_number(source, match.start()),
                "removed legacy type unit",
                before,
                after,
            )
        )
    for edit in _legacy_timestamp_type_edits(source, mask):
        edits.append(edit)
        fixes.append(
            Fix(
                "VY202",
                line_number(source, edit.start),
                "replaced legacy timestamp type",
                "timestamp",
                "uint256",
            )
        )
    return apply_edits(source, edits), fixes, []


def _ascii_string_literals(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char == "#":
            newline = source.find("\n", index)
            index = len(source) if newline == -1 else newline + 1
            continue
        if char not in {"'", '"'}:
            index += 1
            continue
        if index > 0 and source[index - 1] in {"b", "B"}:
            index += 1
            continue
        quote = char
        if source.startswith(quote * 3, index):
            close = source.find(quote * 3, index + 3)
            index = len(source) if close == -1 else close + 3
            continue
        close = _string_literal_end(source, index)
        if close is None:
            break
        content = source[index + 1 : close]
        if any(ord(item) > 127 for item in content):
            replacement = "".join(item if ord(item) <= 127 else "?" for item in content)
            edits.append(TextEdit(index + 1, close, replacement))
            fixes.append(
                Fix(
                    "VY224",
                    line_number(source, index),
                    "replaced non-ASCII string literal characters",
                    source[index : close + 1],
                    f"{quote}{replacement}{quote}",
                )
            )
        index = close + 1
    return apply_edits(source, edits), fixes, []


def _string_literal_end(source: str, start: int) -> int | None:
    quote = source[start]
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index
        if char == "\n":
            return None
        index += 1
    return None


def _legacy_timestamp_type_edits(source: str, mask: list[bool]) -> list[TextEdit]:
    edits: list[TextEdit] = []
    offset = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        code = line.split("#", 1)[0]
        if "timestamp" not in code:
            offset += len(raw_line)
            continue
        spans: list[tuple[int, int]] = []
        if re.match(r"\s*def\b", code):
            spans.append((0, len(code)))
        else:
            colon = code.find(":")
            if colon != -1:
                assignment = code.find("=", colon + 1)
                end = assignment if assignment != -1 else len(code)
                spans.append((colon + 1, end))
        for start, end in spans:
            for match in re.finditer(r"\btimestamp\b", code[start:end]):
                if re.match(r"\s*def\b", code) and not _timestamp_in_def_type_position(
                    code, start + match.start(), start + match.end()
                ):
                    continue
                absolute_start = offset + start + match.start()
                absolute_end = offset + start + match.end()
                if absolute_start > 0 and source[absolute_start - 1] == ".":
                    continue
                if span_is_code(mask, absolute_start, absolute_end):
                    edits.append(TextEdit(absolute_start, absolute_end, "uint256"))
        offset += len(raw_line)
    return edits


def _timestamp_in_def_type_position(code: str, start: int, end: int) -> bool:
    prev_index = start - 1
    while prev_index >= 0 and code[prev_index].isspace():
        prev_index -= 1
    if prev_index >= 0 and code[prev_index] in {":", ">"}:
        return True
    next_index = end
    while next_index < len(code) and code[next_index].isspace():
        next_index += 1
    return not (next_index < len(code) and code[next_index] == ":")


def _legacy_events(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    current = source
    if rule_context.is_enabled("VY203"):
        current, event_fixes = _rewrite_legacy_event_declarations(current)
        fixes.extend(event_fixes)
    if rule_context.is_enabled("VY204"):
        pattern = re.compile(r"\blog\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        edits: list[TextEdit] = []
        mask = code_mask(current)
        for match in pattern.finditer(current):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            replacement = f"log {match.group(1)}("
            edits.append(TextEdit(match.start(), match.end(), replacement))
            fixes.append(
                Fix(
                    "VY204",
                    line_number(current, match.start()),
                    "changed legacy log call to statement",
                    match.group(0),
                    replacement,
                )
            )
        current = apply_edits(current, edits)
    return current, fixes, []


def _event_kwargs(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    event_fields = _collect_event_fields(source)
    if not event_fields:
        return source, [], []

    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    for match in re.finditer(r"\blog\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        event_name = match.group(1)
        fields = event_fields.get(event_name)
        if fields is None:
            continue
        open_index = source.find("(", match.start(), match.end())
        close = find_matching(source, open_index)
        if close is None:
            continue
        raw_args = source[open_index + 1 : close]
        args = split_top_level_args(_strip_arg_comments(raw_args))
        if args is None or len(args) != len(fields) or any("=" in arg for arg in args):
            continue
        kwargs = [f"{field}={arg}" for field, arg in zip(fields, args, strict=True)]
        if "\n" in raw_args:
            indent = source[source.rfind("\n", 0, match.start()) + 1 : match.start()]
            child_indent = indent + "    "
            joined = ",\n".join(f"{child_indent}{kwarg}" for kwarg in kwargs)
            replacement = f"log {event_name}(\n{joined}\n{indent})"
        else:
            replacement = f"log {event_name}({', '.join(kwargs)})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY112",
                line_number(source, match.start()),
                "changed positional event log to keyword arguments",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _legacy_public_fixed_array_getters(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _pre_021_context(rule_context.migration):
        return rule_context.source, [], []
    source = rule_context.source
    mask = rule_context.code_mask
    declarations = list(_legacy_public_fixed_array_declarations(source, mask))
    if not declarations:
        return source, [], []

    used = code_identifiers(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    getters: list[str] = []
    renames: dict[str, str] = {}
    for declaration in declarations:
        name = declaration.group("name")
        if re.search(rf"^[ \t]*def[ \t]+{re.escape(name)}[ \t]*\(", source, re.MULTILINE):
            continue
        backing_name = _legacy_public_getter_backing_name(name, used)
        used.add(backing_name)
        value_type = declaration.group("type").strip()
        bound = declaration.group("bound").strip()
        replacement = (
            f"{declaration.group('indent')}{backing_name}: {value_type}[{bound}]"
            f"{declaration.group('comment')}"
        )
        edits.append(TextEdit(declaration.start(), declaration.end(), replacement))
        fixes.append(
            Fix(
                "VY223",
                line_number(source, declaration.start()),
                "renamed legacy public fixed array backing storage",
                declaration.group(0),
                replacement,
            )
        )
        renames[name] = backing_name
        getters.append(
            "\n"
            "@view\n"
            "@external\n"
            f"def {name}(i: int128) -> {value_type}:\n"
            f"    return self.{backing_name}[convert(i, uint256)]\n"
        )

    if not renames:
        return source, [], []

    for name, backing_name in renames.items():
        for match in re.finditer(rf"\bself\.{re.escape(name)}\b", source):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            edits.append(TextEdit(match.start(), match.end(), f"self.{backing_name}"))
            fixes.append(
                Fix(
                    "VY223",
                    line_number(source, match.start()),
                    "renamed legacy public fixed array storage reference",
                    f"self.{name}",
                    f"self.{backing_name}",
                )
            )

    insert_at = len(source)
    prefix = "" if source.endswith("\n") else "\n"
    getter_source = prefix + "\n".join(getters).lstrip("\n") + "\n"
    edits.append(TextEdit(insert_at, insert_at, getter_source))
    fixes.append(
        Fix(
            "VY223",
            line_number(source, insert_at),
            "added legacy int128 public fixed array getter",
            "",
            getter_source.rstrip(),
        )
    )
    return apply_edits(source, edits), fixes, []


def _legacy_public_fixed_array_declarations(source: str, mask: list[bool]):
    pattern = re.compile(
        r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*:[ \t]*"
        r"public[ \t]*\([ \t]*(?P<type>[^()\n\[\]]+)[ \t]*"
        r"\[[ \t]*(?P<bound>[^\]\n]+)[ \t]*\][ \t]*\)"
        r"(?P<comment>[ \t]*(?:#.*)?)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if match.group("indent") or not span_is_code(mask, match.start(), match.end()):
            continue
        value_type = match.group("type").strip()
        if value_type.startswith(("Bytes", "String", "DynArray", "HashMap")):
            continue
        yield match


def _legacy_public_getter_backing_name(name: str, used: set[str]) -> str:
    candidate = f"__{name}"
    if candidate not in used:
        return candidate
    index = 2
    while f"__{name}_{index}" in used:
        index += 1
    return f"__{name}_{index}"


def _legacy_dynamic_types(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    for match in re.finditer(r"\b(bytes|string)(\s*\[)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        after = "Bytes" if match.group(1) == "bytes" else "String"
        edits.append(TextEdit(match.start(1), match.end(1), after))
        fixes.append(
            Fix(
                "VY207",
                line_number(source, match.start()),
                f"capitalized legacy {match.group(1)} type",
                match.group(1),
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _early_beta_syntax(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    context = rule_context.migration
    rules = {"VY216", "VY217", "VY218", "VY219", "VY221"}
    if not rule_context.any_enabled(rules) or not _pre_021_context(context):
        return source, [], []
    fixes: list[Fix] = []
    current = source
    if rule_context.is_enabled("VY216"):
        current, new_fixes = _rewrite_early_beta_types(current)
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY217"):
        current, new_fixes = replace_identifier_call(current, "sha3", "keccak256", "VY217")
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY218"):
        current, new_fixes = _rewrite_string_convert_types(current)
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY219"):
        current, new_fixes = _rewrite_early_beta_clear(current)
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY221"):
        current, new_fixes = _rewrite_early_beta_call_syntax(current)
        fixes.extend(new_fixes)
    return current, fixes, []


def _rewrite_early_beta_types(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bbytes\s*<=\s*([A-Za-z_][A-Za-z0-9_]*|\d+)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = f"bytes[{match.group(1)}]"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY216",
                line_number(source, match.start()),
                "changed early beta bytes bound syntax",
                match.group(0),
                replacement,
            )
        )
    type_replacements = {
        "num128": "int128",
        "num256": "uint256",
        "signed256": "int256",
        "num": "int128",
    }
    pattern = re.compile(r"(?P<prefix>(?::|->|\[)\s*)(?P<type>num128|num256|signed256|num)\b")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = type_replacements[match.group("type")]
        edits.append(TextEdit(match.start("type"), match.end("type"), replacement))
        fixes.append(
            Fix(
                "VY216",
                line_number(source, match.start()),
                "renamed early beta numeric type",
                match.group("type"),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_string_convert_types(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in iter_calls(source, "convert"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 2:
            continue
        target = args[1].strip()
        target_match = re.fullmatch(r"""(["'])([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\1""", target)
        if target_match is None:
            continue
        replacement = f"convert({args[0].strip()}, {target_match.group(2)})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY218",
                line_number(source, match.start()),
                "changed convert string type argument",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_early_beta_clear(source: str) -> tuple[str, list[Fix]]:
    current, fixes = replace_identifier_call(source, "reset", "clear", "VY219")
    mask = code_mask(current)
    edits: list[TextEdit] = []
    for match in re.finditer(
        r"^(?P<indent>[ \t]*)del[ \t]+(?P<expr>[^#\n]+?)(?P<trailing>[ \t]*(?:#.*)?)(?=\n|$)",
        current,
        re.MULTILINE,
    ):
        if not _line_match_starts_outside_string(current, mask, match.start()):
            continue
        expr = match.group("expr").strip()
        replacement = f"{match.group('indent')}clear({expr}){match.group('trailing')}"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY219",
                line_number(current, match.start()),
                "changed early beta delete statement",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(current, edits), fixes


def _rewrite_early_beta_call_syntax(source: str) -> tuple[str, list[Fix]]:
    current, fixes = _rewrite_as_wei_value_units(source)
    current, slice_fixes = _rewrite_slice_keyword_args(current)
    fixes.extend(slice_fixes)
    return current, fixes


def _rewrite_as_wei_value_units(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in iter_calls(source, "as_wei_value"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 2:
            continue
        unit = args[1].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", unit) is None:
            continue
        replacement = f'as_wei_value({args[0].strip()}, "{unit}")'
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY221",
                line_number(source, match.start()),
                "quoted as_wei_value unit",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_slice_keyword_args(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in iter_calls(source, "slice"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 3:
            continue
        value_arg: str | None = None
        start_arg: str | None = None
        length_arg: str | None = None
        for arg in args:
            name, sep, raw_value = arg.partition("=")
            if not sep:
                if value_arg is not None:
                    value_arg = None
                    break
                value_arg = arg.strip()
                continue
            if name.strip() == "start":
                start_arg = raw_value.strip()
            elif name.strip() == "len":
                length_arg = raw_value.strip()
            else:
                value_arg = None
                break
        if value_arg is None or start_arg is None or length_arg is None:
            continue
        replacement = f"slice({value_arg}, {start_arg}, {length_arg})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY221",
                line_number(source, match.start()),
                "changed slice required keyword arguments",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _reserved_parameter_names(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    context = rule_context.migration
    if context.source_floor is not None and context.source_floor > VyperVersion("0.2.1"):
        return source, [], []
    facts = rule_context.facts
    line_offsets = rule_context.line_offsets
    mask = rule_context.code_mask
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    pattern = re.compile(r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\((?P<args>[^)]*)\)", re.MULTILINE)
    for match in pattern.finditer(source):
        args = split_top_level_args(match.group("args"))
        if args is None:
            continue
        names = {arg.split(":", 1)[0].split("=", 1)[0].strip() for arg in args}
        if "value" not in names:
            continue
        replacement = "_value" if "_value" not in names else "value_"
        args_start = match.start("args")
        args_text = match.group("args")
        for name_match in re.finditer(r"\bvalue\b(?=\s*(?::|=|,|$))", args_text):
            start = args_start + name_match.start()
            edits.append(TextEdit(start, start + len("value"), replacement))
        function_line = line_number(source, match.start())
        body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
        for name_match in re.finditer(r"\bvalue\b", source[body_start:body_end]):
            start = body_start + name_match.start()
            end = body_start + name_match.end()
            if not span_is_code(mask, start, end):
                continue
            if _is_attribute_name(source, start) or _is_keyword_argument_name(source, start, end):
                continue
            edits.append(TextEdit(start, end, replacement))
        fixes.append(
            Fix(
                "VY212",
                function_line,
                "renamed reserved function parameter value",
                "value",
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _reserved_local_names(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    line_offsets = rule_context.line_offsets
    mask = rule_context.code_mask
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    reserved_names = {"min_value", "max_value"}
    decl_pattern = re.compile(r"(?m)^[ \t]*(min_value|max_value)[ \t]*:")

    for function_line, vars_for_func in sorted(facts.function_vars.items()):
        body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
        body = source[body_start:body_end]
        declared_names: dict[str, int] = {}
        for decl_match in decl_pattern.finditer(body):
            name = decl_match.group(1)
            start = body_start + decl_match.start(1)
            end = body_start + decl_match.end(1)
            if not span_is_code(mask, start, end):
                continue
            declared_names.setdefault(name, start)
        for name, declaration_start in declared_names.items():
            taken = set(vars_for_func) | reserved_names
            replacement = f"_{name}"
            if replacement in taken:
                replacement = f"{name}_"
            for name_match in re.finditer(rf"\b{re.escape(name)}\b", source[declaration_start:body_end]):
                start = declaration_start + name_match.start()
                end = declaration_start + name_match.end()
                if not span_is_code(mask, start, end):
                    continue
                if _is_attribute_name(source, start) or _is_keyword_argument_name(source, start, end):
                    continue
                next_index = end
                while next_index < body_end and source[next_index] in " \t":
                    next_index += 1
                if next_index < body_end and source[next_index] == "(":
                    continue
                edits.append(TextEdit(start, end, replacement))
            fixes.append(
                Fix(
                    "VY222",
                    line_number(source, declaration_start),
                    "renamed local variable colliding with builtin",
                    name,
                    replacement,
                )
            )
    return apply_edits(source, edits), fixes, []


def _natspec_strictness(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    lines = source.splitlines(keepends=True)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    in_docstring = False
    quote = ""
    doc_function_start: int | None = None
    seen_singleton_natspec_fields: set[str] = set()
    reserved_natspec_tags: set[str] = set()
    offset = 0
    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\n")
        stripped = line.lstrip()
        if not in_docstring:
            if stripped.startswith(('"""', "'''")):
                quote = stripped[:3]
                in_docstring = True
                doc_function_start = _function_start_at_line(facts, line_no)
                seen_singleton_natspec_fields = set()
                reserved_natspec_tags = _natspec_tags_until_close(lines, line_no, quote)
                if stripped.count(quote) >= 2:
                    in_docstring = False
                    doc_function_start = None
                    seen_singleton_natspec_fields = set()
                    reserved_natspec_tags = set()
            offset += len(raw_line)
            continue

        if stripped.startswith(quote):
            in_docstring = False
            doc_function_start = None
            seen_singleton_natspec_fields = set()
            reserved_natspec_tags = set()
            offset += len(raw_line)
            continue

        singleton_tag = _natspec_singleton_tag(line)
        if singleton_tag is not None and singleton_tag in seen_singleton_natspec_fields:
            replacement = _duplicate_natspec_line_replacement(
                line,
                singleton_tag,
                reserved_natspec_tags,
            )
            if replacement is None:
                edits.append(TextEdit(offset, offset + len(raw_line), ""))
                fixes.append(
                    Fix(
                        "VY058",
                        line_no,
                        "removed duplicate NatSpec field",
                        line,
                        "",
                    )
                )
            else:
                edits.append(TextEdit(offset, offset + len(line), replacement))
                fixes.append(
                    Fix(
                        "VY058",
                        line_no,
                        "rewrote duplicate NatSpec field as custom tag",
                        line,
                        replacement,
                    )
                )
            offset += len(raw_line)
            continue
        if singleton_tag is not None:
            seen_singleton_natspec_fields.add(singleton_tag)

        params = _function_param_names_at_start(facts, doc_function_start)
        replacement = _natspec_line_replacement(line, params)
        if replacement is None:
            edits.append(TextEdit(offset, offset + len(raw_line), ""))
            fixes.append(
                Fix(
                    "VY058",
                    line_no,
                    "removed NatSpec line for unknown function parameter",
                    line,
                    "",
                )
            )
        elif replacement != line:
            edits.append(TextEdit(offset, offset + len(line), replacement))
            fixes.append(Fix("VY058", line_no, "updated NatSpec tag syntax", line, replacement))
        offset += len(raw_line)
    return apply_edits(source, edits), fixes, []


_SINGLETON_NATSPEC_FIELDS = frozenset({"author", "title", "notice", "dev", "license"})


def _natspec_singleton_tag(line: str) -> str | None:
    match = re.match(r"^\s*@([A-Za-z_][A-Za-z0-9_-]*)\b", line)
    if match is None:
        return None
    tag = match.group(1)
    return tag if tag in _SINGLETON_NATSPEC_FIELDS else None


def _natspec_tags_until_close(lines: list[str], start: int, quote: str) -> set[str]:
    tags: set[str] = set()
    for raw_line in lines[start:]:
        stripped = raw_line.lstrip()
        if stripped.startswith(quote):
            break
        match = re.match(r"^\s*@(\S+)", raw_line)
        if match is not None:
            tags.add(match.group(1))
    return tags


def _next_custom_natspec_tag(tag: str, reserved: set[str]) -> str:
    candidate = f"custom:{tag}"
    suffix = 2
    while candidate in reserved:
        candidate = f"custom:{tag}-{suffix}"
        suffix += 1
    reserved.add(candidate)
    return candidate


def _duplicate_natspec_line_replacement(
    line: str, tag: str, reserved: set[str]
) -> str | None:
    match = re.match(r"^(\s*)@[A-Za-z_][A-Za-z0-9_-]*(\s+.*)?$", line)
    if match is None:
        return line
    body = match.group(2)
    if body is None or not body.strip():
        return None
    custom_tag = _next_custom_natspec_tag(tag, reserved)
    return f"{match.group(1)}@{custom_tag}{body}"


def _function_param_names_at_start(facts: SourceFacts, start: int | None) -> set[str] | None:
    if start is None:
        return None
    name = facts.function_names.get(start)
    if name is None:
        return None
    return set(facts.function_params.get(name, {}))


def _natspec_line_replacement(line: str, params: set[str] | None) -> str | None:
    param_match = re.match(r"^(\s*)@param\s+([A-Za-z_][A-Za-z0-9_]*)(:)?(\s+.*)?$", line)
    if param_match is not None and params is not None:
        name = param_match.group(2)
        if name not in params or not (param_match.group(4) or "").strip():
            return None
        if param_match.group(3):
            return f"{param_match.group(1)}@param {name}{param_match.group(4) or ''}"
        return line
    fork_match = re.match(r"^(\s*)@fork(\s+.*)?$", line)
    if fork_match is not None:
        return f"{fork_match.group(1)}@custom:fork{fork_match.group(2) or ''}"
    return line


def _docstring_only_bodies(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    mask = rule_context.code_mask
    edits: list[TextEdit] = []
    fixes: list[Fix] = []

    for index, line in enumerate(lines):
        match = re.match(
            r"(?P<indent>[ \t]*)def\s+[A-Za-z_][A-Za-z0-9_]*\(.*\):\s*(?:#.*)?$",
            line,
        )
        if not match or not _line_match_starts_outside_string(source, mask, offsets[index]):
            continue
        def_indent = len(match.group("indent"))
        doc_index = _next_body_line(lines, index + 1, def_indent)
        if doc_index is None:
            continue
        doc_line = lines[doc_index]
        doc_stripped = doc_line.strip()
        quote = doc_stripped[:3]
        if quote not in {'"""', "'''"}:
            continue
        close_index = _docstring_close_line(lines, doc_index, quote)
        if close_index is None:
            continue
        next_statement = _next_body_line(lines, close_index + 1, def_indent)
        if next_statement is not None:
            continue
        insert_at = offsets[close_index] + len(lines[close_index])
        body_indent = re.match(r"[ \t]*", doc_line).group(0)
        insertion = (
            f"{body_indent}pass\n"
            if lines[close_index].endswith("\n")
            else f"\n{body_indent}pass"
        )
        edits.append(TextEdit(insert_at, insert_at, insertion))
        fixes.append(
            Fix(
                "VY131",
                close_index + 1,
                "added pass after docstring-only function body",
                "",
                "pass",
            )
        )
    return apply_edits(source, edits), fixes, []


def _next_body_line(lines: list[str], start: int, def_indent: int) -> int | None:
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[index]) - len(lines[index].lstrip(" \t"))
        if indent <= def_indent:
            return None
        return index
    return None


def _docstring_close_line(lines: list[str], start: int, quote: str) -> int | None:
    first = lines[start].strip()
    if first.count(quote) >= 2:
        return start
    for index in range(start + 1, len(lines)):
        if quote in lines[index]:
            return index
    return None


def _legacy_constructor_locks(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    current, fixes, insertions = _remove_constructor_decorators(
        source,
        {"@nonreentrant"},
        "VY210",
        "removed nonreentrant constructor decorator",
    )
    fixes.extend(insertions)
    return current, fixes, []


def _rewrite_legacy_event_declarations(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*event\s*\(\s*\{", re.MULTILINE)
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_brace = source.rfind("{", match.start(), match.end())
        close_brace = find_matching(source, open_brace, "{", "}")
        if close_brace is None:
            continue
        close_paren = source.find(")", close_brace)
        if close_paren == -1:
            continue
        fields = split_top_level_args(source[open_brace + 1 : close_brace])
        if fields is None:
            continue
        lines = [f"{match.group(1)}event {match.group(2)}:"]
        child_indent = f"{match.group(1)}    "
        ok = True
        for field in fields:
            if ":" not in field:
                ok = False
                break
            name, typ = field.split(":", 1)
            lines.append(f"{child_indent}{name.strip()}: {typ.strip()}")
        if not ok:
            continue
        replacement = "\n".join(lines)
        edits.append(TextEdit(match.start(), close_paren + 1, replacement))
        fixes.append(
            Fix(
                "VY203",
                line_number(source, match.start()),
                "changed legacy event declaration",
                source[match.start() : close_paren + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _collect_event_fields(source: str) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {}
    lines = source.splitlines(keepends=True)
    mask = code_mask(source)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(
            r"^(?P<indent>\s*)event\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:#.*)?$", line
        )
        if match is None or not _line_match_starts_outside_string(
            source, mask, offsets[index]
        ):
            index += 1
            continue
        indent = len(match.group("indent"))
        fields: list[str] = []
        index += 1
        while index < len(lines):
            child = lines[index]
            if not child.strip() or child.lstrip().startswith("#"):
                index += 1
                continue
            child_indent = len(child) - len(child.lstrip())
            if child_indent <= indent:
                break
            field = re.match(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:", child)
            if field is not None and _line_match_starts_outside_string(
                source, mask, offsets[index]
            ):
                fields.append(field.group("name"))
            index += 1
        if fields:
            events[match.group("name")] = fields
    return events




EARLY_RULES = (
    Rule("pragma", runner=_pragma, changes=(target_update("VY001", (0, 3, 10)),)),
    Rule("legacy_decorators", runner=_legacy_decorators, changes=(target_floor("VY201", (0, 2, 1)),)),
    Rule("legacy_type_units", runner=_legacy_type_units, changes=(target_floor("VY202", (0, 2, 1)),)),
    Rule("ascii_string_literals", runner=_ascii_string_literals, changes=(crossing("VY224", (0, 4, 0)),)),
    Rule(
        "legacy_events",
        runner=_legacy_events,
        changes=(
            target_floor("VY203", (0, 2, 1)),
            target_floor("VY204", (0, 2, 1)),
        ),
    ),
    Rule(
        "legacy_public_fixed_array_getters",
        runner=_legacy_public_fixed_array_getters,
        changes=(target_floor("VY223", (0, 2, 1)),),
    ),
    Rule("event_kwargs", runner=_event_kwargs, changes=(crossing("VY112", (0, 4, 1)),)),
)

POST_INTERFACE_RULES = (
    Rule(
        "early_beta_syntax",
        runner=_early_beta_syntax,
        changes=(
            target_floor("VY216", (0, 2, 1)),
            target_floor("VY217", (0, 2, 1)),
            target_floor("VY218", (0, 2, 1)),
            target_floor("VY219", (0, 2, 1)),
            target_floor("VY221", (0, 2, 1)),
        ),
    ),
    Rule("legacy_dynamic_types", runner=_legacy_dynamic_types, changes=(target_floor("VY207", (0, 2, 1)),)),
    Rule("reserved_parameter_names", runner=_reserved_parameter_names, changes=(target_floor("VY212", (0, 2, 1)),),),
    Rule("reserved_local_names", runner=_reserved_local_names, changes=(target_floor("VY222", (0, 4, 0)),),),
)

POST_DIAGNOSTIC_RULES = (
    Rule("natspec_strictness", runner=_natspec_strictness, changes=(crossing("VY058", (0, 4, 0)),)),
    Rule(
        "docstring_only_bodies",
        runner=_docstring_only_bodies,
        changes=(crossing("VY131", (0, 4, 0)),),
    ),
)

POST_COMPARISON_RULES = (
    Rule("legacy_constructor_locks", runner=_legacy_constructor_locks, changes=(crossing("VY210", (0, 2, 16)),)),
)
