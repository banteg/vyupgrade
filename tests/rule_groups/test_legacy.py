from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_docstring_only_function_body_gets_pass_for_0_4_target(config) -> None:
    source = '''#pragma version 0.3.10
@internal
def hook():
    """
    Optional override point.
    """
'''

    result = apply_rules(source, config(target_version="0.4.3"))

    assert '    """\n    pass\n' in result.source
    assert any(fix.rule == "VY131" for fix in result.fixes)


def test_docstring_only_function_body_unchanged_before_0_4_target(config) -> None:
    source = '''#pragma version 0.3.10
@internal
def hook():
    """
    Optional override point.
    """
'''

    result = apply_rules(source, config(target_version="0.3.10"))

    assert "pass" not in result.source
    assert not any(fix.rule == "VY131" for fix in result.fixes)


def test_docstring_only_constructor_with_comments_gets_pass(config) -> None:
    source = '''#pragma version 0.3.0
@external
def __init__():
    """
    Contract constructor.
    """
    # self.initialized = True
'''

    result = apply_rules(source, config(target_version="0.4.3"))

    assert '    """\n    pass\n    # self.initialized = True' in result.source
    assert any(fix.rule == "VY131" for fix in result.fixes)


def test_event_logs_rewrite_to_keyword_arguments(config) -> None:
    source = """# @version 0.3.0
event Transfer:
    sender: indexed(address)
    receiver: indexed(address)
    value: uint256

@external
def f(receiver: address, value: uint256):
    log Transfer(msg.sender, receiver, value)
"""

    result = apply_rules(source, config())

    assert "log Transfer(sender=msg.sender, receiver=receiver, value=value)" in result.source


def test_event_field_collection_ignores_docstrings(config) -> None:
    source = '''# @version 0.3.10
event Transfer:
    sender: indexed(address)
    receiver: indexed(address)

@external
def f(sender: address, receiver: address):
    """
    event Transfer:
        receiver: indexed(address)
        sender: indexed(address)
    """
    log Transfer(sender, receiver)
'''
    selected = config(source_version="0.3.10", select=frozenset({"VY112"}))

    first = apply_rules(source, selected)
    second = apply_rules(first.source, selected)

    assert "log Transfer(sender=sender, receiver=receiver)" in first.source
    assert "        receiver: indexed(address)\n        sender: indexed(address)" in first.source
    assert second.source == first.source


def test_timestamp_parameter_name_is_not_rewritten_as_type(config) -> None:
    source = """# @version 0.2.8
@view
def _balanceOf(user: address, timestamp: uint256) -> uint256:
    return timestamp

@view
def expires_at(ts: timestamp) -> timestamp:
    return ts
"""

    result = apply_rules(source, config())

    assert "def _balanceOf(user: address, timestamp: uint256) -> uint256:" in result.source
    assert "def expires_at(ts: uint256) -> uint256:" in result.source
    assert "uint256: uint256" not in result.source


def test_event_logs_rewrite_multiline_arguments_with_comments(config) -> None:
    source = """# @version 0.3.10
event StrategyReported:
    strategy: indexed(address)
    gain: uint256
    loss: uint256
    protocol_fees: uint256
    total_fees: uint256

@external
def f(strategy: address, gain: uint256, loss: uint256, total_fees: uint256):
    log StrategyReported(
        strategy,
        gain,
        loss,
        total_fees / 100,  # Protocol Fees
        total_fees
    )
"""

    result = apply_rules(source, config())

    assert "protocol_fees=total_fees // 100" in result.source
    assert "total_fees=#" not in result.source
    assert "total_fees=total_fees" in result.source


def test_natspec_strictness_removes_unknown_params_and_customizes_unknown_tags(config) -> None:
    source = '''# @version 0.3.10
"""
@title Voting Escrow
@fork Curve Finance
"""

@external
def createMotion(
    targets: DynArray[address, 4],
    values: DynArray[uint256, 4],
) -> uint256:
    """
    @notice Create a motion.
    @param targets: The contracts to call
    @param values: The values to send
    @param calldatas: The calldata payloads
    @param emptyParam
    @return motionId: The id of the motion
    """
    return 1
'''

    result = apply_rules(source, config())

    assert "@custom:fork Curve Finance" in result.source
    assert "@param targets The contracts to call" in result.source
    assert "@param values The values to send" in result.source
    assert "calldatas" not in result.source
    assert "emptyParam" not in result.source
    assert any(fix.rule == "VY058" for fix in result.fixes)


def test_natspec_strictness_customizes_duplicate_singleton_fields(config) -> None:
    source = '''# @version 0.3.10
"""
@author 0xvv
@author Axxe
"""

@external
def collect_fees() -> uint256:
    """
    @notice Collect the fees charged as interest
    @notice None of this fees are collected if factory has no fee_receiver
            This is by design.
    """
    return 0
'''

    result = apply_rules(source, config())

    assert "@author 0xvv" in result.source
    assert "@custom:author Axxe" in result.source
    assert "@notice Collect the fees charged as interest" in result.source
    assert "@custom:notice None of this fees are collected" in result.source
    assert any(fix.rule == "VY058" for fix in result.fixes)


def test_natspec_strictness_uniquely_numbers_repeated_singleton_fields(config) -> None:
    source = '''# @version 0.3.10
# @dev This ordinary comment is not NatSpec.
NOTE: constant(String[32]) = "@dev neither is this string"

@external
def collect_fees() -> uint256:
    """
    @dev First detail
    @dev Second detail
    @dev Third detail
    @dev Fourth detail
    """
    return 0
'''

    first = apply_rules(source, config())
    second = apply_rules(first.source, config())

    assert "@dev First detail" in first.source
    assert "@custom:dev Second detail" in first.source
    assert "@custom:dev-2 Third detail" in first.source
    assert "@custom:dev-3 Fourth detail" in first.source
    assert "# @dev This ordinary comment is not NatSpec." in first.source
    assert '"@dev neither is this string"' in first.source
    assert second.source == first.source


def test_natspec_strictness_avoids_existing_custom_tag_names(config) -> None:
    source = '''# @version 0.3.10
@external
def collect_fees() -> uint256:
    """
    @dev First detail
    @custom:dev Existing custom detail
    @dev Second detail
    @custom:dev-3 Another existing custom detail
    @dev Third detail
    """
    return 0
'''

    result = apply_rules(source, config())

    assert "@custom:dev Existing custom detail" in result.source
    assert "@custom:dev-2 Second detail" in result.source
    assert "@custom:dev-3 Another existing custom detail" in result.source
    assert "@custom:dev-4 Third detail" in result.source


def test_pragma_rewrite_bumps_to_enabled_target_version(config) -> None:
    source = """# @version 0.3.8
@external
def f():
    pass
"""

    before = apply_rules(source, config(target_version="0.3.9"))
    after = apply_rules(source, config(target_version="0.3.10"))

    assert "# @version 0.3.8" in before.source
    assert "#pragma version 0.3.10" in after.source


def test_pragma_updates_existing_pragma_version(config) -> None:
    source = """# pragma version 0.3.10
@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "#pragma version 0.4.3" in result.source


def test_pragma_does_not_report_unchanged_fix(config) -> None:
    source = """#pragma version 0.3.10
x: uint256
"""

    result = apply_rules(source, config(target_version="0.3.10"))

    assert result.source == source
    assert not [fix for fix in result.fixes if fix.rule == "VY001"]


def test_pragma_is_added_when_missing(config) -> None:
    source = """@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3", source_version="0.3.10"))

    assert result.source.startswith("#pragma version 0.4.3\n")


def test_pragma_can_be_stripped_without_leaving_a_blank_line(config) -> None:
    source = """# @version 0.3.10
@external
def f():
    pass
"""

    result = apply_rules(source, config(strip_pragma=True))

    assert result.source == """@external
def f():
    pass
"""
    assert [(fix.rule, fix.message) for fix in result.fixes] == [
        ("VY001", "removed version pragma")
    ]
    assert apply_rules(result.source, config(strip_pragma=True)).source == result.source


def test_strip_pragma_does_not_add_a_missing_pragma_or_touch_docstrings(config) -> None:
    source = '''@external
def f():
    """
    # @version 0.3.10
    """
    pass
'''

    result = apply_rules(
        source,
        config(strip_pragma=True, source_version="0.3.10"),
    )

    assert result.source == source
    assert not [fix for fix in result.fixes if fix.rule == "VY001"]


def test_strip_pragma_removes_every_source_directive(config) -> None:
    source = """# @version 0.3.10
#pragma version >=0.3.10,<0.5.0
x: uint256
"""

    result = apply_rules(source, config(strip_pragma=True))

    assert result.source == "x: uint256\n"
    assert [fix.line for fix in result.fixes if fix.rule == "VY001"] == [1, 2]


def test_legacy_0_2_1_syntax_rewrites_are_granular(config) -> None:
    source = """# @version 0.2.1
Transfer: event({_from: indexed(address), _to: indexed(address), _value: uint256})

contract Token:
    def transfer(to: address, amount: uint256) -> bool: modifying

balances: map(address, uint256)
payload: bytes[100]
name: string[32]

@constant
@public
def f(token: Token, amount: uint256(wei), data: bytes[32]) -> uint256:
    log.Transfer(msg.sender, self, amount)
    assert_modifiable(token.transfer(msg.sender, amount))
    raw_call(msg.sender, data, outsize=32)
    return extract32(data, 0, type=uint256)

@private
def _g():
    pass
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "event Transfer:\n    _from: indexed(address)" in result.source
    assert "interface Token:" in result.source
    assert "balances: HashMap[address, uint256]" in result.source
    assert "payload: Bytes[100]" in result.source
    assert "name: String[32]" in result.source
    assert "@view\n@external" in result.source
    assert "amount: uint256," in result.source
    assert "data: Bytes[32]" in result.source
    assert "log Transfer(msg.sender, self, amount)" in result.source
    assert "assert token.transfer(msg.sender, amount)" in result.source
    assert "max_outsize=32" in result.source
    assert "output_type=uint256" in result.source
    assert "@internal\ndef _g" in result.source
    assert {fix.rule for fix in result.fixes} >= {
        "VY201",
        "VY202",
        "VY203",
        "VY204",
        "VY205",
        "VY206",
        "VY207",
        "VY208",
    }


def test_non_ascii_string_literals_are_sanitized_for_modern_target(config) -> None:
    source = '''# @version 0.3.7
minter: address

@external
def set_uri(_uri: String[100]) -> bool:
    assert msg.sender == self.minter, "Les charrettes sont libres et indépendantes !"
    return True
'''

    result = apply_rules(source, config())

    assert '"Les charrettes sont libres et ind?pendantes !"' in result.source
    assert any(fix.rule == "VY224" for fix in result.fixes)


def test_legacy_public_fixed_array_getter_preserves_int128_selector(config) -> None:
    source = """# @version 0.1.0b17
coins: public(address[2])
balances: public(uint256[N_COINS])
N_COINS: constant(uint256) = 2

@public
@constant
def first() -> address:
    return self.coins[0]
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "__coins: address[2]" in result.source
    assert "__balances: uint256[N_COINS]" in result.source
    assert "coins: public(address[2])" not in result.source
    assert "balances: public(uint256[N_COINS])" not in result.source
    assert "def coins(i: int128) -> address:" in result.source
    assert "return self.__coins[convert(i, uint256)]" in result.source
    assert "def balances(i: int128) -> uint256:" in result.source
    assert "return self.__balances[convert(i, uint256)]" in result.source
    assert "return self.__coins[0]" in result.source
    assert any(fix.rule == "VY223" for fix in result.fixes)


def test_delegate_raw_call_value_kwarg_removed(config) -> None:
    source = """# pragma version ^0.3.10
@external
@payable
def f(target: address, data: Bytes[32]) -> Bytes[32]:
    success: bool = False
    raw_data: Bytes[32] = b""
    success, raw_data = raw_call(target, data, max_outsize=32,\\
                                 value=msg.value, is_delegate_call=True, revert_on_failure=False)
    assert success
    return raw_data
"""

    result = apply_rules(source, config())

    assert "value=msg.value" not in result.source
    assert "is_delegate_call=True" in result.source
    assert "revert_on_failure=False" in result.source
    assert any(fix.rule == "VY208" for fix in result.fixes)


def test_legacy_event_after_blank_line_does_not_add_blank_field_lines(config) -> None:
    source = """# @version 0.2.1

Transfer: event({_from: indexed(address), _to: indexed(address), _value: uint256})
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert (
        "event Transfer:\n"
        "    _from: indexed(address)\n"
        "    _to: indexed(address)\n"
        "    _value: uint256\n"
    ) in result.source
    assert "event Transfer:\n\n" not in result.source


def test_legacy_timestamp_type_rewrites_in_type_positions(config) -> None:
    source = """event CommitNewAdmin:
    deadline: indexed(timestamp)

event Start:
    timestamp: uint256

struct Ramp:
    initial_time: timestamp

period_timestamp: public(HashMap[int128, timestamp])
last_epoch_time: public(timestamp)

@external
def f() -> timestamp:
    log Start(timestamp=block.timestamp)
    self.point_history[0] = Point({ts: block.timestamp})
    return block.timestamp
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "deadline: indexed(uint256)" in result.source
    assert "timestamp: uint256" in result.source
    assert "log Start(timestamp=block.timestamp)" in result.source
    assert "Point({ts: block.timestamp})" in result.source
    assert "initial_time: uint256" in result.source
    assert "period_timestamp: public(HashMap[int128, uint256])" in result.source
    assert "last_epoch_time: public(uint256)" in result.source
    assert "def f() -> uint256:" in result.source
    assert "return block.timestamp" in result.source


def test_legacy_as_unitless_number_is_unwrapped(config) -> None:
    source = """# @version 0.2.1
@external
def f(start: uint256) -> uint256:
    return as_unitless_number(block.timestamp - start)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "return block.timestamp - start" in result.source
    assert "as_unitless_number" not in result.source


def test_reserved_value_parameter_is_renamed_with_function_references(config) -> None:
    source = """# @version 0.2.1
@internal
def _deposit_for(addr: address, value: uint256):
    self.balance += value
    log Deposit(addr, value=value)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "def _deposit_for(addr: address, _value: uint256):" in result.source
    assert "self.balance += _value" in result.source
    assert "log Deposit(addr, value=_value)" in result.source
    assert any(fix.rule == "VY212" for fix in result.fixes)


def test_builtin_colliding_max_value_local_is_renamed(config) -> None:
    source = """# @version 0.3.10
allowance: HashMap[address, HashMap[address, uint256]]

@external
def f(_from: address, _value: uint256):
    _allowance: uint256 = self.allowance[_from][msg.sender]
    max_value:uint256 = 115792089237316195423570985008687907853269984665640564039457584007913129639935
    # if _allowance != max_value(uint256):
    if _allowance != max_value:
        self.allowance[_from][msg.sender] = _allowance - _value
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "_max_value:uint256 =" in result.source
    assert "if _allowance != _max_value:" in result.source
    assert "# if _allowance != max_value(uint256):" in result.source
    assert any(fix.rule == "VY222" for fix in result.fixes)


def test_builtin_colliding_min_and_max_value_locals_are_renamed(config) -> None:
    source = """# @version 0.3.10
user_point_epoch: HashMap[address, uint256]

@external
def f(addr: address, ts: uint256) -> uint256:
    min_value: uint256 = 0
    max_value: uint256 = self.user_point_epoch[addr]
    for i in range(128):
        if min_value >= max_value:
            break
        mid: uint256 = (min_value + max_value + 1) / 2
        if mid <= ts:
            min_value = mid
        else:
            max_value = mid - 1
    return min_value
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "_min_value: uint256 = 0" in result.source
    assert "_max_value: uint256 = self.user_point_epoch[addr]" in result.source
    assert "if _min_value >= _max_value:" in result.source
    assert "mid: uint256 = (_min_value + _max_value + 1) // 2" in result.source
    assert "return _min_value" in result.source
    assert sum(1 for fix in result.fixes if fix.rule == "VY222") == 2


def test_early_beta_syntax_cleanup_rewrites_safe_forms(config) -> None:
    source = """# @version 0.1.0b4
payload: bytes <= 32
amounts: num[3]
counter: num256
signed: signed256

@public
def f(data: bytes <= 32, amount: num128, target: address) -> uint256:
    x: uint256 = convert(amount, "uint256")
    digest: bytes32 = sha3(data)
    reset(self.counter)
    del self.signed
    raw_call(target, data, outsize=32)
    sliced: bytes <= 4 = slice(data, start=0, len=4)
    return as_wei_value(1, wei)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "payload: Bytes[32]" in result.source
    assert "amounts: int128[3]" in result.source
    assert "counter: uint256" in result.source
    assert "signed: int256" in result.source
    assert "data: Bytes[32]" in result.source
    assert "amount: int128" in result.source
    assert 'convert(amount, "uint256")' not in result.source
    assert "convert(amount, uint256)" in result.source
    assert "keccak256(data)" in result.source
    assert "clear(self.counter)" in result.source
    assert "clear(self.signed)" in result.source
    assert "max_outsize=32" in result.source
    assert "slice(data, 0, 4)" in result.source
    assert 'as_wei_value(1, "wei")' in result.source
    assert {fix.rule for fix in result.fixes} >= {"VY216", "VY217", "VY218", "VY219", "VY221"}


def test_early_beta_cleanup_skips_comments_strings_and_identifiers(config) -> None:
    source = """# @version 0.1.0b4
note: String[64] = "sha3 convert(x, \\"uint256\\") reset bytes <= 32"
# sha3(convert(x, "uint256"))

@public
def f(num: uint256) -> uint256:
    return num
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert '"sha3 convert(x, \\"uint256\\") reset bytes <= 32"' in result.source
    assert '# sha3(convert(x, "uint256"))' in result.source
    assert "def f(num: uint256)" in result.source
    assert "return num" in result.source
    assert not ({"VY216", "VY217", "VY218", "VY219", "VY221"} & {fix.rule for fix in result.fixes})
