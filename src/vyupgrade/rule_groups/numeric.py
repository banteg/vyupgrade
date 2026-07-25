from __future__ import annotations

import re

from ..analysis import (
    SourceFacts,
    infer_expr_type,
    is_integer_type,
    normalize_type,
    parse_source_facts,
)
from ..models import Diagnostic, Fix
from ..rule_helpers import (
    find_matching_open as _find_matching_open,
    innermost_non_overlapping as _innermost_non_overlapping,
    insert_import as _insert_import,
    line_match_starts_outside_string as _line_match_starts_outside_string,
)
from ..rule_registry import Rule, RuleContext, crossing
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    line_number,
    split_top_level_args,
    span_is_code,
)
from .numeric_constant_helpers import integer_constant_values
from .numeric_scope import vars_for_argument as _vars_for_argument
from .numeric_types import (
    is_signed_integer_type as _is_signed_integer_type,
    is_unsigned_integer_type as _is_unsigned_integer_type,
)


def _integer_division(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    for match in re.finditer(r"(?<!/)/(?!/)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        line_start = source.rfind("\n", 0, match.start()) + 1
        line_end = source.find("\n", match.end())
        if line_end == -1:
            line_end = len(source)
        line = source[line_start:line_end]
        if re.match(r"\s*(?:from|import)\b", line):
            continue
        left = _read_left_operand(source, match.start())
        right = _read_right_operand(source, match.end())
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        left_type = infer_expr_type(left, vars_for_line, facts)
        right_type = infer_expr_type(right, vars_for_line, facts)
        slash_col = match.start() - line_start
        left_is_integer = is_integer_type(left_type) or _integerish_expression(
            left, vars_for_line, facts
        )
        right_is_integer = is_integer_type(right_type) or _integerish_expression(
            right, vars_for_line, facts
        )
        if (
            (left_is_integer and right_is_integer)
            or (
                _integerish_expression(line[:slash_col], vars_for_line, facts)
                and _integerish_expression(line[slash_col + 1 :], vars_for_line, facts)
            )
            or (
                _integerish_expression(line[slash_col + 1 :], vars_for_line, facts)
                and _multiline_integer_division_context(source, line_start)
            )
            or _multiline_integer_division_assignment_context(source, line_start, vars_for_line)
            or (
                _integerish_expression(line[slash_col + 1 :], vars_for_line, facts)
                and line.lstrip().startswith("assert ")
                and "decimal" not in line
            )
        ):
            if not rule_context.is_enabled("VY050"):
                continue
            edits.append(TextEdit(match.start(), match.end(), "//"))
            fixes.append(
                Fix(
                    "VY050",
                    line_number(source, match.start()),
                    "changed integer division to //",
                    "/",
                    "//",
                )
            )
        else:
            if rule_context.is_enabled("VYD004"):
                diagnostics.append(
                    Diagnostic(
                        "VYD004",
                        line_number(source, match.start()),
                        "cannot prove / operands are integer typed",
                    )
                )
    return apply_edits(source, edits), fixes, diagnostics


def _sqrt(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    return _math_builtin(rule_context, "sqrt", "VY100")


def _isqrt(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    return _math_builtin(rule_context, "isqrt", "VY101")


def _math_builtin(
    rule_context: RuleContext, name: str, rule: str
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    if _name_is_user_defined(facts, name) or _name_is_imported(source, name):
        return source, [], []
    mask = rule_context.code_mask
    math_binding = _absolute_math_import_binding(source)
    import_line: str | None = None
    if math_binding is None:
        math_binding = _fresh_math_binding(source)
        import_line = (
            "import math\n" if math_binding == "math" else f"import math as {math_binding}\n"
        )
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for match in re.finditer(rf"(?<!\.)\b{re.escape(name)}\s*\(", source):
        line_start = source.rfind("\n", 0, match.start()) + 1
        if re.search(r"\bdef\s*$", source[line_start : match.start()]):
            continue
        if not span_is_code(mask, match.start(), match.end()):
            continue
        after = f"{math_binding}.{name}"
        edits.append(TextEdit(match.start(), match.start() + len(name), after))
        fixes.append(
            Fix(
                rule,
                line_number(source, match.start()),
                f"moved {name} to math module",
                name,
                after,
            )
        )
    next_source = apply_edits(source, edits)
    if edits and import_line is not None:
        next_source = _insert_import(next_source, import_line)
        fixes.append(Fix(rule, 1, "added math import", "", import_line.strip()))
    return next_source, fixes, []


def _absolute_math_import_binding(source: str) -> str | None:
    mask = code_mask(source)
    pattern = re.compile(
        r"^[ \t]*import[ \t]+math"
        r"(?:[ \t]+as[ \t]+(?P<alias>[A-Za-z_][A-Za-z0-9_]*))?"
        r"[ \t]*(?:#[^\n]*)?$",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if _line_match_starts_outside_string(source, mask, match.start()):
            return match.group("alias") or "math"
    return None


def _fresh_math_binding(source: str) -> str:
    identifiers = code_identifiers(source)
    if "math" not in identifiers:
        return "math"
    binding = "builtin_math"
    suffix = 2
    while binding in identifiers:
        binding = f"builtin_math_{suffix}"
        suffix += 1
    return binding


def _bitwise(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    current = source
    if rule_context.is_enabled("VY110"):
        for name, operator, unary in [
            ("bitwise_and", "&", False),
            ("bitwise_or", "|", False),
            ("bitwise_xor", "^", False),
            ("bitwise_not", "~", True),
        ]:
            while True:
                current, new_fixes = _replace_builtin_call(current, name, operator, unary, "VY110")
                if not new_fixes:
                    break
                fixes.extend(new_fixes)
    if rule_context.any_enabled({"VY111", "VYD012"}):
        current_context = rule_context.with_source(current)
        current, new_fixes, new_diagnostics = _replace_shift_builtin(current_context)
        fixes.extend(new_fixes)
        diagnostics.extend(new_diagnostics)
    return current, fixes, diagnostics


def _read_left_operand(source: str, index: int) -> str:
    i = index - 1
    while i >= 0 and source[i].isspace():
        i -= 1
    if i >= 0 and source[i] == ")":
        open_index = _find_matching_open(source, i)
        if open_index is not None:
            start = _read_indexed_expression_start(source, open_index)
            if start < open_index and source[start] == ".":
                target = _read_left_operand(source, start)
                return f"{target}{source[start : i + 1]}"
            if start < open_index:
                return source[start : i + 1]
            return source[open_index : i + 1]
    if i >= 0 and source[i] == "]":
        open_index = _find_matching_open(source, i, open_char="[", close_char="]")
        if open_index is not None:
            start = _read_indexed_expression_start(source, open_index)
            return source[start : i + 1]
    end = i + 1
    while i >= 0 and re.match(r"[A-Za-z0-9_.$]", source[i]):
        i -= 1
    return source[i + 1 : end]


def _read_indexed_expression_start(source: str, open_index: int) -> int:
    i = open_index - 1
    while i >= 0 and re.match(r"[A-Za-z0-9_.$]", source[i]):
        i -= 1
    return i + 1


def _read_right_operand(source: str, index: int) -> str:
    i = index
    while i < len(source) and source[i].isspace():
        i += 1
    start = i
    if source.startswith(("staticcall ", "extcall "), i):
        i = source.find(" ", i) + 1
        while i < len(source) and source[i].isspace():
            i += 1
    i = _read_chained_expression_end(source, i)
    return source[start:i]


def _read_chained_expression_end(source: str, index: int) -> int:
    i = index
    while i < len(source):
        start = i
        continue_chain = False
        while i < len(source) and re.match(r"[A-Za-z0-9_.$]", source[i]):
            i += 1
        while i < len(source) and source[i] in "([":
            close = (
                find_matching(source, i)
                if source[i] == "("
                else find_matching(source, i, open_char="[", close_char="]")
            )
            if close is None:
                return i
            i = close + 1
            if i < len(source) and source[i] == ".":
                i += 1
                continue_chain = True
                break
        if continue_chain:
            continue
        if i == start or (i < len(source) and source[i] not in "([."):
            return i
    return i


def _integerish_expression(expr: str, vars_for_line: dict[str, str], facts=None) -> bool:
    expr = expr.split("#", 1)[0]
    if facts is not None:
        expr = _replace_integerish_subexpressions(expr, vars_for_line, facts)
    expr = re.sub(r"\bself\.balance\b", "1", expr)
    expr = expr.replace("self.", "")
    expr = re.sub(
        r"\b(?:block\.(?:timestamp|number|difficulty|basefee|prevhash)|chain\.id|msg\.value)\b",
        "1",
        expr,
    )
    expr = re.sub(r"^\s*(?:return|assert)\s+", "", expr)
    if "=" in expr:
        expr = expr.rsplit("=", 1)[-1]
    if re.search(r"\bdecimal\b|\d+\.\d+", expr):
        return False
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr)
    if not tokens:
        return bool(re.search(r"\d", expr))
    typed = False
    for token in tokens:
        if re.fullmatch(r"u?int(?:\d+)?", token):
            typed = True
            continue
        if token in {
            "convert",
            "as_wei_value",
            "isqrt",
            "len",
            "max",
            "min",
            "pow_mod256",
            "unsafe_add",
            "unsafe_div",
            "unsafe_mul",
            "unsafe_sub",
        }:
            typed = True
            continue
        if facts is not None and is_integer_type(facts.function_return_names.get(token)):
            typed = True
            continue
        token_type = vars_for_line.get(token)
        if token_type is None:
            if token.isupper():
                typed = True
                continue
            return False
        if not is_integer_type(token_type):
            return False
        typed = True
    return typed


def _replace_integerish_subexpressions(expr: str, vars_for_line: dict[str, str], facts) -> str:
    edits: list[TextEdit] = []
    for pattern in [
        r"\bisqrt\s*\(",
        r"\blen\s*\(",
        r"(?:staticcall|extcall)\s+(?:[A-Za-z_][A-Za-z0-9_]*\s*\([^()\n]*(?:\([^()\n]*\)[^()\n]*)*\)|(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*)*)\.[A-Za-z_][A-Za-z0-9_]*\s*\(",
        r"(?<!\.)\b(?:self\.)?[A-Za-z_][A-Za-z0-9_]*\s*\(",
        r"(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])+(?:\.[A-Za-z_][A-Za-z0-9_]*)?",
        r"(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*)+",
    ]:
        for match in re.finditer(pattern, expr):
            end = match.end()
            if expr[end - 1] == "(":
                close = find_matching(expr, end - 1)
                if close is None:
                    continue
                end = close + 1
            while end < len(expr) and expr[end] == "[":
                close = find_matching(expr, end, open_char="[", close_char="]")
                if close is None:
                    break
                end = close + 1
            candidate = expr[match.start() : end]
            if is_integer_type(infer_expr_type(candidate, vars_for_line, facts)):
                edits.append(TextEdit(match.start(), end, "1"))
    return apply_edits(expr, _outermost_non_overlapping_edits(edits))


def _outermost_non_overlapping_edits(edits: list[TextEdit]) -> list[TextEdit]:
    selected: list[TextEdit] = []
    for edit in sorted(edits, key=lambda item: (item.start, -(item.end - item.start))):
        if any(edit.start >= kept.start and edit.end <= kept.end for kept in selected):
            continue
        if any(edit.start < kept.end and kept.start < edit.end for kept in selected):
            continue
        selected.append(edit)
    return sorted(selected, key=lambda item: item.start)


def _multiline_integer_division_context(source: str, line_start: int) -> bool:
    prefix = source[:line_start].splitlines()[-8:]
    block = "\n".join(prefix)
    if re.search(r"\bdecimal\b|\d+\.\d+", block):
        return False
    return bool(re.search(r"return\s*\($|:\s*u?int(?:\d+)?\s*=\s*\($", block, re.MULTILINE))


def _multiline_integer_division_assignment_context(
    source: str, line_start: int, vars_for_line: dict[str, str]
) -> bool:
    line_end = source.find("\n", line_start)
    if line_end == -1:
        line_end = len(source)
    if source[line_start:line_end].strip() != "/":
        return False
    prefix = source[:line_start].splitlines()[-8:]
    block = "\n".join(prefix)
    if re.search(r"\bdecimal\b|\d+\.\d+", block):
        return False
    for line in reversed(prefix):
        match = re.match(
            r"\s*((?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\s*=\s*\(\s*$", line
        )
        if match is None:
            continue
        target_type = infer_expr_type(match.group(1), vars_for_line)
        return is_integer_type(target_type)
    return False


def _replace_builtin_call(
    source: str, name: str, operator: str, unary: bool, rule: str
) -> tuple[str, list[Fix]]:
    mask = code_mask(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match in re.finditer(rf"\b{re.escape(name)}\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None:
            continue
        if unary and len(args) == 1:
            replacement = f"(~{args[0]})"
        elif not unary and len(args) == 2:
            replacement = f"({args[0]} {operator} {args[1]})"
        else:
            continue
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                rule,
                line_number(source, match.start()),
                f"replaced {name} builtin",
                source[match.start() : close + 1],
                replacement,
            )
        )
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes


def _replace_shift_builtin(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    current = rule_context.source
    all_fixes: list[Fix] = []
    all_diagnostics: list[Diagnostic] = []
    while True:
        mask = code_mask(current)
        constant_values = integer_constant_values(current, rule_context.config.source_ast)
        facts = parse_source_facts(current)
        fixes: list[Fix] = []
        diagnostics: list[Diagnostic] = []
        edits: list[TextEdit] = []
        for match in re.finditer(r"\bshift\s*\(", current):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            close = find_matching(current, current.find("(", match.start()))
            if close is None:
                continue
            args = split_top_level_args(current[match.end() : close])
            if args is None or len(args) != 2:
                continue
            value = args[0].strip()
            shift_by = args[1].strip()
            negative = re.fullmatch(r"-\s*((?:\d|_)+)", shift_by)
            negative_constant = re.fullmatch(r"-\s*([A-Za-z_][A-Za-z0-9_]*)", shift_by)
            negative_expr = re.fullmatch(r"-\s*(.+)", shift_by)
            positive = re.fullmatch(r"\+?\s*((?:\d|_)+)", shift_by)
            positive_constant = re.fullmatch(r"\+?\s*([A-Za-z_][A-Za-z0-9_]*)", shift_by)
            convert_constant = re.fullmatch(
                r"convert\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*u?int(?:\d+)?\s*\)",
                shift_by,
            )
            positive_convert = re.fullmatch(r"convert\s*\((.+),\s*int128\s*\)", shift_by)
            if negative is not None:
                replacement = f"({value} >> {negative.group(1)})"
            elif negative_constant is not None and negative_constant.group(1) in constant_values:
                amount = -constant_values[negative_constant.group(1)]
                operator = "<<" if amount >= 0 else ">>"
                replacement = f"({value} {operator} {abs(amount)})"
            elif negative_expr is not None:
                vars_for_line = facts.vars_at_line(line_number(current, match.start()))
                negative_amount = negative_expr.group(1).strip()
                if _local_name_assigned_negative_integer(
                    current, match.start(), negative_amount, vars_for_line
                ):
                    replacement = f"({value} << convert(-{negative_amount}, uint256))"
                else:
                    replacement = (
                        f"({value} >> "
                        f"({_unsigned_shift_amount_expr(negative_amount, vars_for_line, constant_values)}))"
                    )
            elif positive is not None:
                replacement = f"({value} << {positive.group(1)})"
            elif positive_constant is not None and positive_constant.group(1) in constant_values:
                amount = constant_values[positive_constant.group(1)]
                operator = "<<" if amount >= 0 else ">>"
                replacement = f"({value} {operator} {abs(amount)})"
            elif _local_name_assigned_negative_integer(
                current,
                match.start(),
                shift_by,
                facts.vars_at_line(line_number(current, match.start())),
            ):
                replacement = f"({value} >> convert(-{shift_by}, uint256))"
            elif convert_constant is not None and convert_constant.group(1) in constant_values:
                amount = constant_values[convert_constant.group(1)]
                operator = "<<" if amount >= 0 else ">>"
                replacement = f"({value} {operator} {abs(amount)})"
            elif positive_convert is not None and not positive_convert.group(1).lstrip().startswith(
                "-"
            ):
                vars_for_line = facts.vars_at_line(line_number(current, match.start()))
                amount = _unsigned_shift_convert_replacement(
                    positive_convert.group(1).strip(), vars_for_line
                )
                replacement = f"({value} << {amount})"
            else:
                if rule_context.is_enabled("VYD012"):
                    diagnostics.append(
                        Diagnostic(
                            "VYD012",
                            line_number(current, match.start()),
                            "shift() with non-literal amount needs manual << or >> review",
                        )
                    )
                continue
            if not rule_context.is_enabled("VY111"):
                continue
            edits.append(TextEdit(match.start(), close + 1, replacement))
            fixes.append(
                Fix(
                    "VY111",
                    line_number(current, match.start()),
                    "replaced shift builtin",
                    current[match.start() : close + 1],
                    replacement,
                )
            )
        all_diagnostics.extend(diagnostics)
        if not edits:
            return current, all_fixes, all_diagnostics
        selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
        all_fixes.extend(selected_fixes)
        current = apply_edits(current, selected_edits)


def _unsigned_shift_amount_expr(
    expr: str, vars_for_line: dict[str, str], constant_values: dict[str, int]
) -> str:
    nonnegative_constants = [
        name
        for name, value in sorted(
            constant_values.items(), key=lambda item: len(item[0]), reverse=True
        )
        if value >= 0
    ]
    if nonnegative_constants:
        constant_re = re.compile(
            rf"\b({'|'.join(re.escape(name) for name in nonnegative_constants)})\b"
        )
        expr = constant_re.sub(lambda match: str(constant_values[match.group(1)]), expr)
    return re.sub(
        r"\bconvert\s*\(([^,\n]+),\s*int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?\s*\)",
        lambda match: _unsigned_shift_convert_replacement(match.group(1).strip(), vars_for_line),
        expr,
    )


def _local_name_assigned_negative_integer(
    source: str, index: int, name: str, vars_for_line: dict[str, str]
) -> bool:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return False
    if not _is_signed_integer_type(vars_for_line.get(name)):
        return False
    line_start = source.rfind("\n", 0, index) + 1
    current_line = source[
        line_start : source.find("\n", line_start)
        if source.find("\n", line_start) != -1
        else len(source)
    ]
    current_indent = len(current_line) - len(current_line.lstrip(" \t"))
    for line in reversed(source[:line_start].splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if indent < current_indent and re.match(r"(?:@|def\s+)", stripped):
            return False
        if indent > current_indent:
            continue
        if re.match(rf"{re.escape(name)}\s*(?::\s*int\d+)?\s*=\s*-", stripped):
            return True
        if re.match(rf"{re.escape(name)}\s*(?::[^=]+)?\s*=", stripped):
            return False
    return False


def _unsigned_shift_convert_replacement(expr: str, vars_for_line: dict[str, str]) -> str:
    if _unsigned_integer_expression(expr, vars_for_line):
        return f"({expr})" if re.search(r"[-+*/%<>=|&]", expr) else expr
    return f"convert({expr}, uint256)"


def _unsigned_integer_expression(expr: str, vars_for_line: dict[str, str]) -> bool:
    identifiers = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr)
    if not identifiers:
        return _integerish_expression(expr, vars_for_line)
    return all(
        _is_unsigned_integer_type(infer_expr_type(identifier, vars_for_line))
        for identifier in identifiers
    )


def _name_is_user_defined(facts: SourceFacts, name: str) -> bool:
    return (
        name in facts.global_vars
        or name in facts.function_return_names
        or any(name in vars_for_func for vars_for_func in facts.function_vars.values())
    )


def _name_is_imported(source: str, name: str) -> bool:
    mask = code_mask(source)
    for match in re.finditer(
        r"^[ \t]*from[ \t]+[A-Za-z0-9_.]+[ \t]+import[ \t]+(.+)$", source, re.MULTILINE
    ):
        if not _line_match_starts_outside_string(source, mask, match.start()):
            continue
        for part in match.group(1).split(","):
            imported = part.split("#", 1)[0].strip()
            imported_name, _sep, alias = imported.partition(" as ")
            bound_name = alias.strip() if alias else imported_name.strip()
            if bound_name == name:
                return True
    for match in re.finditer(r"^[ \t]*import[ \t]+(.+)$", source, re.MULTILINE):
        if not _line_match_starts_outside_string(source, mask, match.start()):
            continue
        for part in match.group(1).split(","):
            imported = part.split("#", 1)[0].strip()
            module, _sep, alias = imported.partition(" as ")
            bound_name = alias.strip() if alias else module.split(".", 1)[0].strip()
            if bound_name == name:
                return True
    return False


def _redundant_integer_convert(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    facts = rule_context.facts
    mask = rule_context.code_mask
    for match in re.finditer(r"\bconvert\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = source.find("(", match.start())
        close = find_matching(source, open_index)
        if close is None:
            continue
        args = split_top_level_args(source[open_index + 1 : close])
        if args is None or len(args) != 2:
            continue
        expr, target = args[0].strip(), args[1].strip()
        vars_for_line = _vars_for_argument(
            source,
            open_index + 1 + source[open_index + 1 : close].find(args[0]),
            expr,
            facts.vars_at_line(line_number(source, match.start())),
        )
        if (
            is_integer_type(target)
            and _inside_constant_declaration_line(source, match.start())
            and _integerish_expression(expr, vars_for_line)
            and not _expression_has_signed_integer(expr, vars_for_line)
        ):
            edits.append(TextEdit(match.start(), close + 1, expr))
            fixes.append(
                Fix(
                    "VY051",
                    line_number(source, match.start()),
                    "removed convert from constant initializer",
                    source[match.start() : close + 1],
                    expr,
                )
            )
            continue
        expr_type = infer_expr_type(expr, vars_for_line, facts)
        if (
            (is_integer_type(target) or normalize_type(target) == "bytes32")
            and normalize_type(expr_type or "") == normalize_type(target)
            and _simple_nonliteral_expr(expr)
            and not _target_semantic_convert(expr, target)
        ):
            replacement = _redundant_convert_replacement(expr)
            edits.append(TextEdit(match.start(), close + 1, replacement))
            fixes.append(
                Fix(
                    "VY051",
                    line_number(source, match.start()),
                    "removed redundant integer convert to the same type",
                    source[match.start() : close + 1],
                    replacement,
                )
            )
            continue
        if target != "uint256" or expr.lstrip().startswith("-") or not re.search(r"[-+*/%]", expr):
            continue
        if (
            _integerish_expression(expr, vars_for_line)
            and not _expression_has_signed_integer(expr, vars_for_line)
            and not _expression_has_narrow_unsigned_integer(expr, vars_for_line)
            and not _target_semantic_convert(expr, target)
        ):
            replacement = f"({expr})"
            edits.append(TextEdit(match.start(), close + 1, replacement))
            fixes.append(
                Fix(
                    "VY051",
                    line_number(source, match.start()),
                    "removed redundant uint256 convert around integer expression",
                    source[match.start() : close + 1],
                    replacement,
                )
            )
    return apply_edits(source, edits), fixes, []


def _target_semantic_convert(expr: str, target: str) -> bool:
    target = normalize_type(target)
    if expr == "block.prevhash" and target == "uint256":
        return True
    return (
        target == "uint256"
        and re.search(
            r"\bconvert\s*\([^()\n]+,\s*int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?\s*\)",
            expr,
        )
        is not None
    )


def _redundant_convert_replacement(expr: str) -> str:
    return f"({expr})" if re.search(r"[-+*/%<>=|&]", expr) else expr


def _inside_constant_declaration_line(source: str, start: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    return bool(re.search(r":\s*constant\s*\(", source[line_start:start]))


def _simple_nonliteral_expr(expr: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", expr
        )
    )


def _expression_has_signed_integer(expr: str, vars_for_line: dict[str, str]) -> bool:
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
        if _is_signed_integer_type(vars_for_line.get(token)):
            return True
    return False


def _expression_has_narrow_unsigned_integer(expr: str, vars_for_line: dict[str, str]) -> bool:
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
        type_name = normalize_type(vars_for_line.get(token) or "")
        match = re.fullmatch(r"uint(\d+)", type_name)
        if match is not None and int(match.group(1)) < 256:
            return True
    return False


INTEGER_DIVISION_RULES = (
    Rule(
        "integer_division",
        runner=_integer_division,
        changes=(
            crossing("VY050", (0, 4, 0)),
            crossing("VYD004", (0, 4, 0)),
        ),
    ),
)


REDUNDANT_CONVERT_RULES = (
    Rule(
        "redundant_integer_convert",
        runner=_redundant_integer_convert,
        changes=(crossing("VY051", (0, 4, 0)),),
    ),
)

LATE_RULES = (
    Rule("sqrt", runner=_sqrt, changes=(crossing("VY100", (0, 4, 2)),)),
    Rule("isqrt", runner=_isqrt, changes=(crossing("VY101", "0.5.0a1"),)),
    Rule(
        "bitwise",
        runner=_bitwise,
        changes=(
            crossing("VY110", (0, 4, 2)),
            crossing("VY111", (0, 4, 2)),
            crossing("VYD012", (0, 4, 2)),
        ),
    ),
    Rule(
        "integer_division_after_bitwise",
        runner=_integer_division,
        changes=(
            crossing("VY050", (0, 4, 0)),
            crossing("VYD004", (0, 4, 0)),
        ),
    ),
)
