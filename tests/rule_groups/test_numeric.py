from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_isqrt_moves_to_math_for_0_5_alpha_target(config) -> None:
    source = """#pragma version 0.4.3
@external
def f(x: uint256) -> uint256:
    return isqrt(x)
"""

    result = apply_rules(source, config(target_version="0.5.0a1"))

    assert "import math\n" in result.source
    assert "return math.isqrt(x)" in result.source
    assert any(fix.rule == "VY101" for fix in result.fixes)


def test_isqrt_math_import_follows_module_docstring(config) -> None:
    source = '''#pragma version 0.4.3
"""
@title Example
import math
"""

@external
def f(x: uint256) -> uint256:
    return isqrt(x)
'''

    result = apply_rules(source, config(target_version="0.5.0a1"))

    assert (
        result.source
        == '''#pragma version 0.5.0a1
"""
@title Example
import math
"""

import math
@external
def f(x: uint256) -> uint256:
    return math.isqrt(x)
'''
    )


def test_isqrt_uses_fresh_math_alias_when_math_is_already_bound(config) -> None:
    source = '''#pragma version 0.4.3
"""
@title Example
"""

from . import dep_math as math

@external
def f(x: uint256) -> uint256:
    return isqrt(x) + math._identity(0)
'''

    first = apply_rules(source, config(target_version="0.5.0a1"))
    second = apply_rules(first.source, config(target_version="0.5.0a1"))

    assert "import math as builtin_math\n" in first.source
    assert "return builtin_math.isqrt(x) + math._identity(0)" in first.source
    assert first.source == second.source


def test_isqrt_reuses_existing_aliased_builtin_math_import(config) -> None:
    source = """#pragma version 0.4.3
import math as target_math

@external
def f(x: uint256) -> uint256:
    return isqrt(x)
"""

    result = apply_rules(source, config(target_version="0.5.0a1"))

    assert result.source.count("import math as target_math") == 1
    assert "return target_math.isqrt(x)" in result.source


def test_isqrt_math_alias_suffix_is_deterministic(config) -> None:
    source = """#pragma version 0.4.3
from . import dep_math as math
builtin_math: constant(uint256) = 1

@external
def f(x: uint256) -> uint256:
    return isqrt(x) + math._identity(builtin_math)
"""

    result = apply_rules(source, config(target_version="0.5.0a1"))

    assert "import math as builtin_math_2\n" in result.source
    assert "return builtin_math_2.isqrt(x) + math._identity(builtin_math)" in result.source


def test_isqrt_does_not_rewrite_before_0_5_alpha_target(config) -> None:
    source = """#pragma version 0.4.3
@external
def f(x: uint256) -> uint256:
    return isqrt(x)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "return isqrt(x)" in result.source
    assert not any(fix.rule == "VY101" for fix in result.fixes)


def test_integer_expression_division(config) -> None:
    source = """# @version 0.3.10
MAX_BPS: constant(uint256) = 10_000

@external
def f(total_fees: uint256, protocol_fee_bps: uint16) -> uint256:
    return total_fees * convert(protocol_fee_bps, uint256) / MAX_BPS
"""

    result = apply_rules(source, config())

    assert "convert(protocol_fee_bps, uint256) // MAX_BPS" in result.source


def test_compound_assignment_integer_division(config) -> None:
    source = """# @version 0.3.7
MAX_BPS: constant(uint256) = 10_000

struct Fee:
    performance_fee: uint16

@external
def f(gain: uint256, fee: Fee) -> uint256:
    total_fees: uint256 = 0
    total_fees += (gain * fee.performance_fee) / MAX_BPS
    return total_fees
"""

    result = apply_rules(source, config())

    assert "total_fees += (gain * fee.performance_fee) // MAX_BPS" in result.source


def test_indexed_storage_compound_assignment_integer_division(config) -> None:
    source = """# @version 0.3.7
tokens_per_week: public(HashMap[uint256, uint256])

@external
def f(this_week: uint256, to_distribute: uint256, t: uint256, since_last: uint256):
    self.tokens_per_week[this_week] += to_distribute * (block.timestamp - t) / since_last
"""

    result = apply_rules(source, config())

    assert (
        "self.tokens_per_week[this_week] += to_distribute * (block.timestamp - t) // since_last"
        in result.source
    )


def test_parenthesized_hashmap_storage_integer_division(config) -> None:
    source = """# @version 0.2.16
_balances: (HashMap[address, uint256])

@external
def f() -> uint256:
    return self._balances[self] / 2
"""

    result = apply_rules(source, config())

    assert "return self._balances[self] // 2" in result.source


def test_integer_division_inside_storage_subscript(config) -> None:
    source = """# @version 0.3.10
packed_factory_versions: HashMap[uint256, uint256]

@internal
@view
def _enabled(_version: uint256) -> bool:
    return self.packed_factory_versions[_version / 256] & (1 << (_version % 256)) > 0
"""

    result = apply_rules(source, config())

    assert "self.packed_factory_versions[_version // 256]" in result.source


def test_multiline_function_scope_integer_division_assignment(config) -> None:
    source = """# @version 0.3.7
rates: public(uint256[3])

@external
def exchange(
    i: uint256,
    j: uint256,
    amount: uint256,
) -> uint256:
    rates: uint256[3] = self.rates
    dy: uint256 = amount
    dy = dy * 10**18 / rates[j]
    return dy
"""

    result = apply_rules(source, config())

    assert "dy = dy * 10**18 // rates[j]" in result.source


def test_struct_attribute_integer_division(config) -> None:
    source = """# @version 0.3.7
struct Loan:
    initial_debt: uint256
    rate_mul: uint256

@external
def f(loan: Loan, rate_mul: uint256) -> (uint256, uint256):
    return (loan.initial_debt * rate_mul / loan.rate_mul, rate_mul)
"""

    result = apply_rules(source, config())

    assert "return (loan.initial_debt * rate_mul // loan.rate_mul, rate_mul)" in result.source


def test_nested_struct_attribute_integer_division(config) -> None:
    source = """# @version 0.3.7
struct Rental:
    amount: uint256
    protocol_fee: uint256

struct Context:
    active_rental: Rental

@external
def f(context: Context) -> uint256:
    rewards: uint256 = 0
    rewards += context.active_rental.amount * (10000 - context.active_rental.protocol_fee) / 10000
    return rewards
"""

    result = apply_rules(source, config())

    assert (
        "rewards += context.active_rental.amount * "
        "(10000 - context.active_rental.protocol_fee) // 10000"
    ) in result.source


def test_external_call_integer_division_operand(config) -> None:
    source = """# @version 0.3.7
interface Pool:
    def virtual_balance(asset: uint256) -> uint256: view
    def rate(asset: uint256) -> uint256: view

@external
def f(pool: Pool, asset: uint256, rate: uint256) -> uint256:
    return pool.virtual_balance(asset) * rate / pool.rate(asset)
"""

    result = apply_rules(source, config())

    assert (
        "return staticcall pool.virtual_balance(asset) * rate // staticcall pool.rate(asset)"
        in result.source
    )


def test_indexed_external_call_target_integer_division_operand(config) -> None:
    source = """# @version 0.3.10
interface Pool:
    def totalSupply() -> uint256: view
    def virtual_price() -> uint256: view

@external
def f(pools: Pool[2], i: uint256) -> uint256:
    return pools[i].totalSupply() * pools[i].virtual_price() / 10**18
"""

    result = apply_rules(source, config())

    assert (
        "return staticcall pools[i].totalSupply() * staticcall pools[i].virtual_price() // 10**18"
        in result.source
    )


def test_casted_external_call_target_integer_division_operand(config) -> None:
    source = """# @version 0.2.16
interface Bundle:
    def getBalance(token: address) -> uint256: view
    def totalSupply() -> uint256: view

bstable: address
busd: address
precision: uint256

@external
def f(_amountOut: uint256) -> uint256:
    return (_amountOut * self.precision // Bundle(self.bstable).getBalance(self.busd)) * Bundle(self.bstable).totalSupply() / self.precision
"""

    result = apply_rules(source, config())

    assert (
        "return (_amountOut * self.precision // staticcall Bundle(self.bstable).getBalance(self.busd)) "
        "* staticcall Bundle(self.bstable).totalSupply() // self.precision"
    ) in result.source


def test_struct_attribute_external_call_integer_division_operand(config) -> None:
    source = """# @version 0.3.7
interface Pool:
    def price_oracle(asset: uint256) -> uint256: view

struct SwapData:
    pool: Pool
    j: int128

ONE: constant(uint256) = 10 ** 18

@external
def f(swap_data: SwapData, price: uint256) -> uint256:
    return price * swap_data.pool.price_oracle(convert(swap_data.j, uint256) - 1) / ONE
"""

    result = apply_rules(source, config())

    assert (
        "return price * staticcall swap_data.pool.price_oracle("
        "convert(swap_data.j, uint256) - 1) // ONE"
    ) in result.source


def test_indexed_right_operand_integer_division_inside_external_call(config) -> None:
    source = """# @version 0.3.1
interface Curve:
    def calc_withdraw_one_coin(amount: uint256, i: int128) -> uint256: view

N_COINS: constant(uint256) = 2
PRECISION: constant(uint256) = 10 ** 18

@external
def f(pool: address, dy: uint256, rates: uint256[N_COINS]) -> uint256:
    return Curve(pool).calc_withdraw_one_coin(dy * PRECISION / rates[1], 0)
"""

    result = apply_rules(source, config())

    assert (
        "staticcall Curve(pool).calc_withdraw_one_coin("
        "dy * PRECISION // rates[1], convert(0, int128))" in result.source
    )


def test_min_internal_call_integer_division_operand(config) -> None:
    source = """# @version 0.3.10
MAX_EXP: constant(uint256) = 10 ** 18
rate0: public(uint256)

@internal
@view
def exp(power: int256) -> uint256:
    return MAX_EXP

@external
@view
def f(power: int256) -> uint256:
    return self.rate0 * min(self.exp(power), MAX_EXP) / 10**18
"""

    result = apply_rules(source, config())

    assert "return self.rate0 * min(self.exp(power), MAX_EXP) // 10**18" in result.source


def test_cast_external_call_integer_division_operand(config) -> None:
    source = """# @version 0.3.10
interface CurveToken:
    def totalSupply() -> uint256: view

D: public(uint256)

@internal
@view
def get_xcp(d: uint256) -> uint256:
    return d

@external
@view
def f(token: address) -> uint256:
    return 10**18 * self.get_xcp(self.D) / CurveToken(token).totalSupply()
"""

    result = apply_rules(source, config())

    assert (
        "return 10**18 * self.get_xcp(self.D) // staticcall CurveToken(token).totalSupply()"
        in result.source
    )


def test_self_balance_integer_division_operand(config) -> None:
    source = """# @version 0.3.10
@external
def f(amount: uint256, total_liquidity: uint256) -> uint256:
    return amount * self.balance / total_liquidity
"""

    result = apply_rules(source, config())

    assert "return amount * self.balance // total_liquidity" in result.source


def test_parenthesized_internal_call_integer_division_operand(config) -> None:
    source = """# @version 0.3.10
balances: HashMap[address, uint256]
user_reward_per_token_paid: HashMap[address, uint256]

@internal
@view
def reward_per_token() -> uint256:
    return 10**18

@external
@view
def f(account: address) -> uint256:
    return self.balances[account] * (self.reward_per_token() - self.user_reward_per_token_paid[account]) / 10**18
"""

    result = apply_rules(source, config())

    assert (
        "return self.balances[account] * "
        "(self.reward_per_token() - self.user_reward_per_token_paid[account]) // 10**18"
    ) in result.source


def test_internal_call_integer_division_operand(config) -> None:
    source = """# @version 0.2.11
scale: uint256

@internal
@view
def getBalance(wallet: address) -> uint256:
    return 10**18

@external
@view
def balanceOf(wallet: address) -> uint256:
    return self.getBalance(wallet) / self.scale
"""

    result = apply_rules(source, config())

    assert "return self.getBalance(wallet) // self.scale" in result.source


def test_integer_division_after_bitwise_rewrite(config) -> None:
    source = """# @version 0.3.3
@external
def f(price: uint256, packed: uint256) -> uint256:
    return (price + bitwise_and(packed, 2 ** 128 - 1)) / 2
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "return (price + (packed & 2 ** 128 - 1)) // 2" in result.source


def test_isqrt_integer_division_operand(config) -> None:
    source = """# @version 0.3.10
virtual_price: uint256

@internal
@view
def price_oracle() -> uint256:
    return 10**18

@external
@view
def lp_price() -> uint256:
    return 2 * self.virtual_price * isqrt(self.price_oracle() * 10**18) / 10**18
"""

    result = apply_rules(source, config())

    assert (
        "return 2 * self.virtual_price * isqrt(self.price_oracle() * 10**18) // 10**18"
        in result.source
    )


def test_multiline_return_internal_call_integer_division(config) -> None:
    source = """# @version 0.2.12
totalSupply: public(uint256)

@internal
def _totalAssets() -> uint256:
    return 1

@external
def f(amount: uint256) -> uint256:
    return (
        amount
        * self.totalSupply
        / self._totalAssets()
    )
"""

    result = apply_rules(source, config())

    assert "* self.totalSupply\n        // self._totalAssets()" in result.source


def test_multiline_parenthesized_assignment_integer_division(config) -> None:
    source = """# @version 0.3.1
struct StrategyParams:
    performanceFee: uint256

strategies: HashMap[address, StrategyParams]

@internal
def f(strategy: address, gain: uint256) -> uint256:
    strategist_fee: uint256 = 0
    strategist_fee = (
        gain * self.strategies[strategy].performanceFee
    ) / 10_000
    return strategist_fee
"""

    result = apply_rules(source, config())

    assert ") // 10_000" in result.source


def test_return_integer_division_uses_function_return_type(config) -> None:
    source = """# @version 0.3.7
votes_used: HashMap[address, uint256]
voted: uint256

@external
def claimable(user: address, amount: uint256) -> uint256:
    return amount * self.votes_used[user] / self.voted
"""

    result = apply_rules(source, config())

    assert "return amount * self.votes_used[user] // self.voted" in result.source


def test_len_integer_division_operand(config) -> None:
    source = """# @version 0.2.16
@external
def f(values: DynArray[address, 10], threshold: uint256) -> bool:
    return threshold >= (len(values) / 2) + 1
"""

    result = apply_rules(source, config())

    assert "return threshold >= (len(values) // 2) + 1" in result.source


def test_tuple_return_index_integer_division_operand(config) -> None:
    source = """# @version 0.2.16
E18: constant(int256) = 10 ** 18

@internal
@view
def _scaling_factor() -> (int256, bool):
    return 1, False

@internal
@view
def _exp(x: int256) -> int256:
    return x

@external
@view
def f(x: int256) -> int256:
    return self._exp(47 * (self._scaling_factor()[0] * x / E18 - E18) / 10)
"""

    result = apply_rules(source, config())

    assert "self._scaling_factor()[0] * x // E18" in result.source
    assert "47 * (self._scaling_factor()[0] * x // E18 - E18) // 10" in result.source


def test_as_wei_value_integer_division_operand(config) -> None:
    source = """# @version 0.2.16
reward_per_token_stored: public(uint256)
last_update_time: public(uint256)
reward_rate: public(uint256)
total_supply: public(uint256)

@internal
@view
def _get_last_time_reward_applicable() -> uint256:
    return block.timestamp

@external
@view
def f() -> uint256:
    return self.reward_per_token_stored + (((self._get_last_time_reward_applicable() - self.last_update_time) * self.reward_rate * as_wei_value(1, "ether")) / self.total_supply)
"""

    result = apply_rules(source, config())

    assert '* as_wei_value(1, "ether")) // self.total_supply' in result.source


def test_tab_indented_return_integer_division_uses_function_return_type(config) -> None:
    source = """# @version 0.3.7
@external
def f(x: int128) -> int128:
\treturn 1 / x
"""

    result = apply_rules(source, config())

    assert "return 1 // x" in result.source


def test_multiline_reassignment_integer_division_uses_target_type(config) -> None:
    source = """# @version 0.3.10
@external
def f(x: uint256, y: uint256, z: uint256) -> uint256:
    value: uint256 = x
    value = (
        x * y
        /
        z
    )
    return value
"""

    result = apply_rules(source, config())

    assert "        //\n" in result.source


def test_integerish_call_argument_division_is_rewritten(config) -> None:
    source = """# @version 0.3.10
@external
def f(x: uint256, y: uint256) -> DynArray[uint256, 4]:
    values: DynArray[uint256, 4] = []
    values.append((10**18 * unsafe_div(x, y)) / (x + y))
    return values
"""

    result = apply_rules(source, config())

    assert "values.append((10**18 * unsafe_div(x, y)) // (x + y))" in result.source


def test_redundant_convert_uses_nearest_local_decl(config) -> None:
    source = """# @version 0.2.16
MAX_COINS: constant(int128) = 4

@external
def f(coins: address[MAX_COINS], base_coin_offset: uint256) -> address:
    coin: address = empty(address)
    for i in range(MAX_COINS):
        if i >= base_coin_offset:
            x: uint256 = convert(i, uint256) - base_coin_offset
            coin = coins[convert(x, uint256)]
    return coin
"""

    result = apply_rules(source, config())

    assert "coin = coins[x]" in result.source
    assert "coins[convert(x, uint256)]" not in result.source


def test_redundant_convert_after_integer_division(config) -> None:
    source = """# @version 0.3.3
COEFF: constant(uint256) = 10 ** 18
@external
def f() -> uint256:
    return convert(COEFF * 46 / 10 ** 6, uint256)
"""

    result = apply_rules(source, config())

    assert "convert(" not in result.source
    assert "(COEFF * 46 // 10 ** 6)" in result.source


def test_redundant_convert_keeps_narrow_unsigned_expression(config) -> None:
    source = """# @version 0.3.9
@external
def f(left: uint8, right: uint8) -> uint256:
    if left > right:
        return convert(left - right, uint256)
    return convert(right - left, uint256)
"""

    result = apply_rules(source, config())

    assert "return convert(left - right, uint256)" in result.source
    assert "return convert(right - left, uint256)" in result.source


def test_redundant_convert_keeps_narrow_unsigned_exponent(config) -> None:
    source = """# @version 0.3.10
@external
def f(decimals: uint8) -> uint256:
    return 10 ** convert(decimals // 2, uint256)
"""

    result = apply_rules(source, config())

    assert "10 ** convert(decimals // 2, uint256)" in result.source


def test_redundant_convert_keeps_signed_integer_expression(config) -> None:
    source = """# @version 0.3.10
E18: constant(int256) = 10 ** 18

@external
def f(x: int256) -> uint256:
    return convert(E18 * E18 / (E18 + 10 * x), uint256)
"""

    result = apply_rules(source, config())

    assert "return convert(E18 * E18 // (E18 + 10 * x), uint256)" in result.source


def test_redundant_convert_to_same_integer_type(config) -> None:
    source = """# @version 0.3.10
PRECISION: constant(uint256) = 10**18

@external
def f() -> uint256:
    return convert(PRECISION, uint256)
"""

    result = apply_rules(source, config())

    assert "return PRECISION" in result.source
    assert any(fix.rule == "VY051" for fix in result.fixes)


def test_redundant_prevrandao_bytes32_convert_removed(config) -> None:
    source = """# @version 0.3.6
@external
def f() -> Bytes[32]:
    return concat(convert(block.difficulty, bytes32))
"""

    result = apply_rules(source, config())

    assert "concat(block.prevrandao)" in result.source
    assert "convert(block.prevrandao, bytes32)" not in result.source


def test_redundant_bytes32_convert_keeps_uint256_storage_read(config) -> None:
    source = """# @version 0.2.16
aave_referral: uint256

@external
def f():
    aave_referral: bytes32 = convert(self.aave_referral, bytes32)
    raw_call(empty(address), concat(aave_referral))
"""

    result = apply_rules(source, config())

    assert "aave_referral: bytes32 = convert(self.aave_referral, bytes32)" in result.source


def test_prevhash_uint256_convert_kept_for_target_arithmetic(config) -> None:
    source = """# @version 0.2.8
ticket_count: uint256

@external
def choosewinner() -> uint256:
    return convert(block.prevhash, uint256) % self.ticket_count
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "convert(block.prevhash, uint256) % self.ticket_count" in result.source
    assert "block.prevhash % self.ticket_count" not in result.source


def test_signed_max_uint256_convert_kept_for_target_return(config) -> None:
    source = """# @version 0.3.10
@external
def f(value: uint256, delta: uint256) -> uint256:
    return convert(max(
        convert(value, int256),
        convert(value, int256) - convert(delta, int256)
    ), uint256)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "return convert(max(" in result.source
    assert "), uint256)" in result.source
    assert "return (max(" not in result.source


def test_literal_convert_kept_for_abi_encoding_context(config) -> None:
    source = """# @version 0.3.10
@external
def f() -> Bytes[96]:
    return abi_encode(convert(0, uint256), method_id=method_id("deposit(uint256)"))
"""

    result = apply_rules(source, config())

    assert "abi_encode(convert(0, uint256), method_id=" in result.source


def test_redundant_convert_removed_from_constant_initializers(config) -> None:
    source = """# @version 0.3.10
ZERO: constant(uint256) = convert(0, uint256)
PRECISION_MUL: constant(uint256[2]) = [convert(1, uint256), convert(10 ** 6, uint256)]
"""

    result = apply_rules(source, config())

    assert "ZERO: constant(uint256) = 0" in result.source
    assert "PRECISION_MUL: constant(uint256[2]) = [1, 10 ** 6]" in result.source


def test_multiline_integer_division(config) -> None:
    source = """# @version 0.3.10
totalSupply: public(uint256)

@internal
def f(shares: uint256) -> uint256:
    return (
        shares
        * self.totalSupply
        / self.totalSupply
    )
"""

    result = apply_rules(source, config())

    assert "// self.totalSupply" in result.source


def test_pr_2937_integer_division_to_floordiv_and_decimal_diagnostic(config) -> None:
    source = """# @version 0.3.10
@external
def f(amount: uint256, scale: decimal):
    shares: uint256 = amount / 2
    ratio: decimal = scale / 2.0
"""

    result = apply_rules(source, config())

    assert "amount // 2" in result.source
    assert "scale / 2.0" in result.source
    assert any(diag.rule == "VYD001" for diag in result.diagnostics)


def test_decimal_division_inside_integer_convert_is_not_rewritten(config) -> None:
    source = """# @version 0.3.10
@external
def f(x: decimal, y: decimal) -> uint256:
    z: uint256 = convert(x / y, uint256)
    return z
"""

    result = apply_rules(source, config())

    assert "convert(x / y, uint256)" in result.source
    assert "convert(x // y, uint256)" not in result.source
    assert any(diag.rule == "VYD004" for diag in result.diagnostics)


def test_patch_rewrites_apply_when_crossing_0_4_2(config) -> None:
    source = """# @version 0.4.1
@external
def f(a: uint256, b: uint256) -> uint256:
    return sqrt(bitwise_and(a, b))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "import math" in result.source
    assert "math.sqrt((a & b))" in result.source
    assert {fix.rule for fix in result.fixes} >= {"VY100", "VY110"}


def test_bitwise_rewrite_handles_nested_builtin_calls(config) -> None:
    source = """# @version 0.4.1
@external
def f(a: uint256, b: uint256, c: uint256) -> uint256:
    return bitwise_or(a, bitwise_or(b, c))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (a | (b | c))" in result.source
    assert [fix.rule for fix in result.fixes].count("VY110") == 2


def test_sqrt_rewrite_skips_local_function_shadowing(config) -> None:
    source = """# @version ^0.4.0
@external
@pure
def sqrt(x: uint256) -> uint256:
    return x

@external
@pure
def f(x: uint256) -> uint256:
    return sqrt(x)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "def sqrt(x: uint256)" in result.source
    assert "def math.sqrt" not in result.source
    assert "return sqrt(x)" in result.source
    assert "import math" not in result.source


def test_sqrt_rewrite_skips_import_shadowing(config) -> None:
    source = """# @version ^0.4.0
from snekmate.utils import sqrt

@external
@pure
def f(x: uint256) -> uint256:
    return sqrt(x)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return sqrt(x)" in result.source
    assert "import math" not in result.source


def test_shift_builtin_rewrites_literals_and_flags_dynamic_amounts(config) -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, n: int128) -> uint256:
    a: uint256 = shift(x, 3)
    b: uint256 = shift(x, -2)
    return shift(x, n)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "a: uint256 = (x << 3)" in result.source
    assert "b: uint256 = (x >> 2)" in result.source
    assert "return shift(x, n)" in result.source
    assert any(fix.rule == "VY111" for fix in result.fixes)
    assert any(diag.rule == "VYD012" for diag in result.diagnostics)


def test_shift_builtin_rewrites_positive_convert_amount(config) -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, i: int128) -> uint256:
    return shift(x, convert(i * 8, int128))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (x << convert(i * 8, uint256))" in result.source


def test_shift_builtin_rewrites_negative_dynamic_amount(config) -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, i: uint256) -> uint256:
    return shift(x, -8 * i)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (x >> (8 * i))" in result.source


def test_shift_builtin_casts_signed_dynamic_amount_before_rewrite(config) -> None:
    source = """# @version 0.3.10
@external
def f(x: uint256) -> uint256:
    total: uint256 = 0
    for i: int128 in range(4):
        total += shift(x, -8 * i)
    return total
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "total += (x >> (8 * convert(i, uint256)))" in result.source


def test_shift_builtin_rewrites_negative_signed_convert_amount_to_unsigned(config) -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, i: uint256) -> uint256:
    return shift(x, -128 * convert(i - 1, int256))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (x >> (128 * i - 1))" not in result.source
    assert "return (x >> (128 * (i - 1)))" in result.source


def test_shift_builtin_rewrites_local_negative_offset(config) -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, i: int128) -> uint256:
    offset: int128 = -8 * i
    return shift(shift(x, offset) % 256, -offset)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "shift(" not in result.source
    assert "(x >> convert(-offset, uint256))" in result.source
    assert "<< convert(-offset, uint256)" in result.source


def test_shift_builtin_folds_constants_inside_dynamic_amount(config) -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 3
PRICE_SIZE: constant(int128) = 256 // (N_COINS - 1)

@external
def f(x: uint256) -> uint256:
    total: uint256 = 0
    for i in range(1, N_COINS):
        total += shift(x, -PRICE_SIZE * convert(i - 1, int256))
    return total
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "total += (x >> (128 * convert(i - 1, uint256)))" in result.source
    assert "PRICE_SIZE * (i - 1)" not in result.source


def test_shift_builtin_removes_unsigned_amount_convert(config) -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(uint256) = 8
interface ERC20:
    def decimals() -> uint256: view

@external
def f(coins: address[MAX_COINS]) -> uint256:
    decimals: uint256 = 0
    for i in range(MAX_COINS):
        coin: address = coins[i]
        decimals += shift(ERC20(coin).decimals(), convert(i*8, int128))
    return decimals
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "decimals += (staticcall ERC20(coin).decimals() << (i*8))" in result.source
    assert "convert(i*8, uint256)" not in result.source


def test_shift_builtin_rewrites_signed_constant_amounts(config) -> None:
    source = """# @version 0.4.1
BAL_SHIFT: constant(int128) = -16

@external
def pack(x: uint256) -> uint256:
    return shift(x, -BAL_SHIFT)

@external
def unpack(x: uint256) -> uint256:
    return shift(x, BAL_SHIFT)

@external
def unpack_converted(x: uint256) -> uint256:
    return shift(x, convert(BAL_SHIFT, uint256))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (x << 16)" in result.source
    assert result.source.count("return (x >> 16)") == 2


def test_shift_amount_constants_are_not_cast_before_shift_rewrite(config) -> None:
    source = """# @version 0.3.10
PREVIOUS_SHIFT: constant(int128) = -120
EPOCH_SHIFT: constant(int128) = -240

@external
def pack(previous: uint256, epoch: uint256) -> uint256:
    return shift(previous, -PREVIOUS_SHIFT) | shift(epoch, -EPOCH_SHIFT)

@external
def unpack(packed: uint256, epoch: uint256) -> uint256:
    if epoch < shift(packed, EPOCH_SHIFT):
        return shift(packed, PREVIOUS_SHIFT)
    return 0
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "convert(PREVIOUS_SHIFT, uint256)" not in result.source
    assert "convert(EPOCH_SHIFT, uint256)" not in result.source
    assert "return (previous << 120) | (epoch << 240)" in result.source
    assert "if epoch < (packed >> 240):" in result.source
    assert "return (packed >> 120)" in result.source
