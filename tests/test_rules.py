from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_mechanical_rewrites_skip_comments_and_strings(config) -> None:
    source = """# @version ^0.3.10

@external
def __init__():
    x: Bytes[32] = _abi_encode(1)
    y: String[64] = "_abi_decode should stay"
    # _abi_encode should stay
"""

    result = apply_rules(source, config())

    assert "#pragma version 0.4.3" in result.source
    assert "@deploy\ndef __init__" in result.source
    assert "@external\ndef __init__" not in result.source
    assert "abi_encode(1)" in result.source
    assert '"_abi_decode should stay"' in result.source
    assert "# _abi_encode should stay" in result.source


def test_line_rewrites_skip_docstring_content(config) -> None:
    source = '''# @version 0.3.10

@external
def f():
    """
    @public
    # @version 0.2.1
    contract Foo:
    """
    pass
'''

    result = apply_rules(source, config())

    assert "#pragma version 0.4.3" in result.source
    assert "    @public" in result.source
    assert "    # @version 0.2.1" in result.source
    assert "    contract Foo:" in result.source
    assert "    @external" not in result.source
    assert "    #pragma version 0.2.1" not in result.source
    assert "    interface Foo:" not in result.source


def test_type_aware_rewrites(config) -> None:
    source = """# @version 0.3.10

from vyper.interfaces import ERC20

interface Strategy:
    def totalAssets() -> uint256: view
    def withdraw(amount: uint256) -> uint256: nonpayable

struct Position:
    shares: uint256
    assets: uint256

token: public(ERC20)

@external
def f(strategy: Strategy, amount: uint256, price: uint256):
    shares: uint256 = amount / price
    p: Position = Position({shares: shares, assets: amount})
    b: uint256 = self.token.balanceOf(msg.sender)
    extcall strategy.withdraw(amount)
    t: uint256 = strategy.totalAssets()
    for i in range(10):
        pass
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC20" in result.source
    assert "token: public(IERC20)" in result.source
    assert "amount // price" in result.source
    assert "Position(shares=shares, assets=amount)" in result.source
    assert "staticcall self.token.balanceOf(msg.sender)" in result.source
    assert "extcall strategy.withdraw(amount)" in result.source
    assert "staticcall strategy.totalAssets()" in result.source
    assert "for i: uint256 in range(10):" in result.source
    assert not [diag for diag in result.diagnostics if diag.rule == "VYD003"]


def test_erc721_import_migration_preserves_staticcall_inference(config) -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC721

@external
def f(nft: ERC721, owner: address) -> uint256:
    return nft.balanceOf(owner)
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC721" in result.source
    assert "def f(nft: IERC721, owner: address)" in result.source
    assert "return staticcall nft.balanceOf(owner)" in result.source
    assert not [diag for diag in result.diagnostics if diag.rule == "VYD003"]


def test_nested_shift_rewrites_without_overlapping_edits(config) -> None:
    source = """# @version 0.3.0
@external
def f(indexes: uint256) -> uint256:
    return shift(shift(indexes, -128), 128)
"""

    result = apply_rules(source, config())

    assert "shift(" not in result.source
    assert "return ((indexes >> 128) << 128)" in result.source


def test_casted_interface_calls_and_assigned_integer_division(config) -> None:
    source = """# @version 0.3.10
interface Token:
    def balanceOf(owner: address) -> uint256: view
    def transfer(to: address, amount: uint256) -> bool: nonpayable

@external
def f(token: address, amount: uint256):
    shares: uint256 = 0
    shares = amount / 2
    b: uint256 = Token(token).balanceOf(msg.sender)
    ok: bool = Token(token).transfer(msg.sender, amount)
"""

    result = apply_rules(source, config())

    assert "shares = amount // 2" in result.source
    assert "staticcall Token(token).balanceOf(msg.sender)" in result.source
    assert "extcall Token(token).transfer(msg.sender, amount)" in result.source


def test_diagnostics_for_ambiguous_cases(config) -> None:
    source = """# @version 0.3.10

@nonreentrant("a")
@external
def f(target: address, amount: uint256, scale: decimal):
    x: decimal = scale / 2.0
    target.foo()
    create_from_blueprint(target)

@nonreentrant("b")
@external
def g():
    pass
"""

    result = apply_rules(source, config())
    rules = {diag.rule for diag in result.diagnostics}

    assert "VYD001" in rules
    assert "VYD002" in rules
    assert "VYD003" in rules
    assert "VYD004" in rules
    assert "VY080" in rules
    assert '@nonreentrant("a")' not in result.source
    assert '@nonreentrant("b")' not in result.source
    assert result.source.count("@nonreentrant") == 2


def test_idempotent(config) -> None:
    source = """# @version 0.3.10
@external
def __init__():
    pass
"""
    once = apply_rules(source, config()).source
    twice = apply_rules(once, config()).source
    assert once == twice


def test_target_before_change_skips_later_patch_rewrites(config) -> None:
    source = """# @version 0.4.1
@external
def f(a: uint256, b: uint256) -> uint256:
    return sqrt(bitwise_and(a, b))
"""

    result = apply_rules(source, config(target_version="0.4.1"))

    assert "math.sqrt" not in result.source
    assert "bitwise_and" in result.source
    assert not [fix for fix in result.fixes if fix.rule in {"VY100", "VY110"}]


def test_source_at_change_skips_already_current_patch_rewrites(config) -> None:
    source = """# @version 0.4.2
@external
def f(a: uint256, b: uint256) -> uint256:
    return sqrt(bitwise_and(a, b))
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "math.sqrt" not in result.source
    assert "bitwise_and" in result.source
    assert not [fix for fix in result.fixes if fix.rule in {"VY100", "VY110"}]


def test_0_4_rules_apply_from_0_2_1_source(config) -> None:
    source = """# @version 0.2.1
@external
def __init__():
    pass
"""

    result = apply_rules(source, config())

    assert "@deploy\ndef __init__" in result.source
    assert any(fix.rule == "VY002" for fix in result.fixes)


def test_constructor_nonreentrant_is_removed_before_deploy_rewrite(config) -> None:
    source = """# @version 0.2.15
@nonreentrant("lock")
@external
def __init__():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "@nonreentrant" not in result.source
    assert "@deploy\ndef __init__" in result.source
    assert {fix.rule for fix in result.fixes} >= {"VY002", "VY210"}


def test_source_newer_than_target_is_error_diagnostic(config) -> None:
    source = """# pragma version >=0.5.0a1,<0.6.0

@external
def f() -> uint256:
    return 1
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert result.source == source
    assert result.fixes == []
    assert [(diag.rule, diag.severity) for diag in result.diagnostics] == [("VYD016", "error")]
    assert "newer than target 0.4.3" in result.diagnostics[0].message


def test_unsupported_source_version_is_distinct_error_diagnostic(config) -> None:
    source = """# pragma version 0.4.4

@external
def f() -> uint256:
    return 1
"""

    result = apply_rules(
        source,
        config(target_version="0.5.0a3", ignore=frozenset({"VYD016"})),
    )

    assert result.source == source
    assert result.fixes == []
    assert [(diag.rule, diag.severity) for diag in result.diagnostics] == [("VYD018", "error")]
    assert "matches no Vyper compiler" in result.diagnostics[0].message
