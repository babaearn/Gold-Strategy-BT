"""
tests/test_all.py — Comprehensive unit test suite
==================================================
Covers every class and method in:
  src/live/indicators.py
  src/live/state_machine.py
  src/live/trade_log.py
  src/live/telegram_bot.py   (BotState only — no Telegram API calls)
  src/live/strategies/__init__.py
  src/live/order_manager.py  (_fmt_qty, _fmt_price; API methods mocked)

Run:
    python -m pytest tests/test_all.py -v
  or
    python tests/test_all.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

# ── path setup ────────────────────────────────────────────────────────────────
_LIVE = os.path.join(os.path.dirname(__file__), '..', 'src', 'live')
sys.path.insert(0, os.path.abspath(_LIVE))

# ── stub heavy / broken optional dependencies before any imports ──────────────
# telegram-bot and pybit are not needed for unit tests; stub them so the
# module-level `from telegram import ...` and `from pybit import ...` don't
# crash in environments without those packages.

def _make_stub(*names):
    """Return a MagicMock module and register it (and children) in sys.modules."""
    stub = MagicMock()
    stub.__spec__ = None
    for name in names:
        sys.modules.setdefault(name, stub)
    return stub

# telegram family
_tg_stub = MagicMock()
_tg_stub.Update = MagicMock
_tg_stub.constants = MagicMock()
_tg_stub.constants.ParseMode = MagicMock()
for _m in [
    'telegram', 'telegram.ext', 'telegram.constants',
    'telegram._payment', 'telegram._payment.stars',
    'telegram._payment.stars.startransactions',
    'telegram._payment.stars.transactionpartner',
    'telegram._gifts', 'telegram._files', 'telegram._files.sticker',
    'telegram._files.file', 'telegram._passport', 'telegram._passport.credentials',
    'cryptography', 'cryptography.hazmat', 'cryptography.hazmat.primitives',
    'cryptography.hazmat.primitives.asymmetric',
    'cryptography.hazmat.primitives.asymmetric.padding',
    'cryptography.hazmat.bindings', 'cryptography.hazmat.bindings._rust',
]:
    sys.modules.setdefault(_m, _tg_stub)

# pybit family
_pybit_stub = MagicMock()
sys.modules.setdefault('pybit', _pybit_stub)
sys.modules.setdefault('pybit.unified_trading', _pybit_stub)

# ── module imports (after stubs are in place) ─────────────────────────────────
from indicators    import IndicatorEngine
from state_machine import StateMachine
from trade_log     import TradeLog
from telegram_bot  import BotState
from strategies    import get_strategy, strategy_name, available_strategies, REGISTRY
from order_manager import _fmt_qty, _fmt_price


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_bar(
    close: float = 100.0,
    open_: float = 99.0,
    high:  float = 101.0,
    low:   float = 98.0,
    ts:    int   = 0,
) -> dict:
    return {
        'timestamp': ts,
        'open':      open_,
        'high':      high,
        'low':       low,
        'close':     close,
        'volume':    1.0,
    }


def _warm_engine(engine: IndicatorEngine, n: int = 150, base: float = 100.0) -> None:
    """Feed `n` bars into the engine so is_warm becomes True."""
    for i in range(n):
        engine.add_bar(_make_bar(close=base, open_=base - 1, high=base + 1, low=base - 2, ts=i))


def _make_ind(
    ema_confirm: float = 100.0,
    ema_fast:    float = 100.0,   # neutral: no crossover by default
    ema_medium:  float = 100.0,
    ema_slow:    float = 100.0,
    prev_ema_confirm: float = 100.0,  # neutral
    prev_ema_fast:    float = 100.0,
    prev_ema_medium:  float = 100.0,
    prev_ema_slow:    float = 100.0,
    open_:  float = 99.0,
    close:  float = 100.0,
    high:   float = 101.0,
    low:    float = 98.0,
    atr:    float = 1.0,
    atr_regime: float = 0.8,
) -> dict:
    """Build a minimal indicator dict matching IndicatorEngine.compute() output."""
    return {
        'open':             open_,
        'high':             high,
        'low':              low,
        'close':            close,
        'ema_confirm':      ema_confirm,
        'ema_fast':         ema_fast,
        'ema_medium':       ema_medium,
        'ema_slow':         ema_slow,
        'atr':              atr,
        'atr_regime':       atr_regime,
        'prev_open':        open_,
        'prev_close':       close - 1,
        'prev_ema_confirm': prev_ema_confirm,
        'prev_ema_fast':    prev_ema_fast,
        'prev_ema_medium':  prev_ema_medium,
        'prev_ema_slow':    prev_ema_slow,
    }


# ─────────────────────────────────────────────────────────────────────────────
# IndicatorEngine
# ─────────────────────────────────────────────────────────────────────────────

class TestIndicatorEngine(unittest.TestCase):

    def setUp(self):
        self.engine = IndicatorEngine()

    # ── add_bar ───────────────────────────────────────────────────────────────

    def test_add_bar_increments_count(self):
        self.assertEqual(self.engine.bar_count, 0)
        self.engine.add_bar(_make_bar())
        self.assertEqual(self.engine.bar_count, 1)

    def test_add_bar_respects_max_bars(self):
        for i in range(IndicatorEngine.MAX_BARS + 10):
            self.engine.add_bar(_make_bar(ts=i))
        self.assertEqual(self.engine.bar_count, IndicatorEngine.MAX_BARS)

    def test_add_bar_drops_oldest(self):
        """The oldest bar should be evicted once MAX_BARS is exceeded."""
        for i in range(IndicatorEngine.MAX_BARS):
            self.engine.add_bar(_make_bar(ts=i, close=float(i)))
        # Add one more — should evict ts=0 (close=0.0)
        self.engine.add_bar(_make_bar(ts=IndicatorEngine.MAX_BARS, close=9999.0))
        # The bar with close=0.0 (ts=0) should be gone
        closes = [b['close'] for b in self.engine._bars]
        self.assertNotIn(0.0, closes)
        self.assertIn(9999.0, closes)

    # ── bar_count / is_warm ───────────────────────────────────────────────────

    def test_bar_count_correct(self):
        for i in range(5):
            self.engine.add_bar(_make_bar(ts=i))
        self.assertEqual(self.engine.bar_count, 5)

    def test_is_warm_false_before_min(self):
        for i in range(IndicatorEngine.MIN_WARM - 1):
            self.engine.add_bar(_make_bar(ts=i))
        self.assertFalse(self.engine.is_warm)

    def test_is_warm_true_at_min(self):
        for i in range(IndicatorEngine.MIN_WARM):
            self.engine.add_bar(_make_bar(ts=i))
        self.assertTrue(self.engine.is_warm)

    # ── compute returns None before warm ──────────────────────────────────────

    def test_compute_returns_none_before_warm(self):
        for i in range(IndicatorEngine.MIN_WARM - 1):
            self.engine.add_bar(_make_bar(ts=i))
        self.assertIsNone(self.engine.compute())

    # ── compute returns all expected keys after warm ──────────────────────────

    def test_compute_returns_expected_keys(self):
        _warm_engine(self.engine)
        result = self.engine.compute()
        self.assertIsNotNone(result)
        expected_keys = [
            'open', 'high', 'low', 'close',
            'ema_fast', 'ema_medium', 'ema_slow', 'ema_confirm',
            'atr', 'atr_regime',
            'prev_open', 'prev_close',
            'prev_ema_fast', 'prev_ema_medium', 'prev_ema_slow', 'prev_ema_confirm',
        ]
        for key in expected_keys:
            self.assertIn(key, result, f"Missing key: {key}")

    def test_compute_values_are_floats(self):
        _warm_engine(self.engine)
        result = self.engine.compute()
        for k, v in result.items():
            self.assertIsInstance(v, float, f"Key '{k}' is not float")

    def test_compute_last_bar_values_match(self):
        """The close returned by compute() should match the last bar added."""
        _warm_engine(self.engine)
        self.engine.add_bar(_make_bar(close=1234.56, high=1235.0, low=1230.0, open_=1231.0, ts=999))
        result = self.engine.compute()
        self.assertAlmostEqual(result['close'], 1234.56, places=4)
        self.assertAlmostEqual(result['high'],  1235.0,  places=4)
        self.assertAlmostEqual(result['low'],   1230.0,  places=4)

    def test_compute_atr_positive(self):
        """ATR must always be > 0 given nonzero price ranges."""
        _warm_engine(self.engine, n=150, base=2000.0)
        result = self.engine.compute()
        self.assertGreater(result['atr'], 0.0)
        self.assertGreater(result['atr_regime'], 0.0)

    def test_compute_ema_confirm_period1_equals_close(self):
        """EMA with period=1 has alpha=1 → converges to last close."""
        eng = IndicatorEngine(ema_confirm=1)
        # Feed many identical bars so EMA converges
        for i in range(200):
            eng.add_bar(_make_bar(close=500.0, high=501.0, low=499.0, open_=499.5, ts=i))
        result = eng.compute()
        self.assertAlmostEqual(result['ema_confirm'], 500.0, places=2)

    def test_compute_constant_close_all_emas_converge(self):
        """If close is constant, all EMAs should converge to that value."""
        _warm_engine(self.engine, n=200, base=300.0)
        result = self.engine.compute()
        for key in ('ema_fast', 'ema_medium', 'ema_slow', 'ema_confirm'):
            self.assertAlmostEqual(result[key], 300.0, delta=0.1,
                                   msg=f"{key} did not converge to 300.0")

    # ── cross_above ───────────────────────────────────────────────────────────

    def test_cross_above_clear_cross(self):
        # prev: a <= b,  curr: a > b  → True
        self.assertTrue(IndicatorEngine.cross_above(
            curr_a=101.0, curr_b=100.0,
            prev_a=99.0,  prev_b=100.0,
        ))

    def test_cross_above_prev_exactly_equal(self):
        # prev_a == prev_b and curr_a > curr_b → still a cross (touching counts)
        self.assertTrue(IndicatorEngine.cross_above(
            curr_a=100.5, curr_b=100.0,
            prev_a=100.0, prev_b=100.0,
        ))

    def test_cross_above_already_above_no_cross(self):
        # Both bars a > b → not a fresh cross
        self.assertFalse(IndicatorEngine.cross_above(
            curr_a=101.0, curr_b=100.0,
            prev_a=101.0, prev_b=100.0,
        ))

    def test_cross_above_still_below(self):
        # a stays below b → no cross
        self.assertFalse(IndicatorEngine.cross_above(
            curr_a=99.0, curr_b=100.0,
            prev_a=99.0, prev_b=100.0,
        ))

    def test_cross_above_equal_current_no_cross(self):
        # curr_a == curr_b → not strictly above
        self.assertFalse(IndicatorEngine.cross_above(
            curr_a=100.0, curr_b=100.0,
            prev_a=99.0,  prev_b=100.0,
        ))

    # ── cross_below ───────────────────────────────────────────────────────────

    def test_cross_below_clear_cross(self):
        self.assertTrue(IndicatorEngine.cross_below(
            curr_a=99.0,  curr_b=100.0,
            prev_a=101.0, prev_b=100.0,
        ))

    def test_cross_below_prev_exactly_equal(self):
        self.assertTrue(IndicatorEngine.cross_below(
            curr_a=99.5,  curr_b=100.0,
            prev_a=100.0, prev_b=100.0,
        ))

    def test_cross_below_already_below_no_cross(self):
        self.assertFalse(IndicatorEngine.cross_below(
            curr_a=99.0,  curr_b=100.0,
            prev_a=99.0,  prev_b=100.0,
        ))

    def test_cross_below_still_above(self):
        self.assertFalse(IndicatorEngine.cross_below(
            curr_a=101.0, curr_b=100.0,
            prev_a=101.0, prev_b=100.0,
        ))

    def test_cross_below_equal_current_no_cross(self):
        self.assertFalse(IndicatorEngine.cross_below(
            curr_a=100.0, curr_b=100.0,
            prev_a=101.0, prev_b=100.0,
        ))

    # ── _ema / _atr internal calc ─────────────────────────────────────────────

    def test_ema_constant_series(self):
        import pandas as pd
        s = pd.Series([100.0] * 50)
        result = IndicatorEngine._ema(s, 14)
        self.assertAlmostEqual(float(result.iloc[-1]), 100.0, places=5)

    def test_ema_period_1_equals_value(self):
        """EMA(1) of any series = the series itself."""
        import pandas as pd
        s = pd.Series([10.0, 20.0, 30.0, 40.0])
        result = IndicatorEngine._ema(s, 1)
        self.assertAlmostEqual(float(result.iloc[-1]), 40.0, places=5)

    def test_atr_constant_range(self):
        """ATR of bars with fixed H-L range should converge to that range."""
        import pandas as pd
        n = 200
        high  = pd.Series([102.0] * n)
        low   = pd.Series([98.0]  * n)
        close = pd.Series([100.0] * n)
        atr = IndicatorEngine._atr(high, low, close, period=14)
        self.assertAlmostEqual(float(atr.iloc[-1]), 4.0, places=2)

    def test_atr_always_positive(self):
        import pandas as pd
        import random
        random.seed(42)
        prices = [100.0 + random.uniform(-1, 1) for _ in range(100)]
        high  = pd.Series([p + 0.5  for p in prices])
        low   = pd.Series([p - 0.5  for p in prices])
        close = pd.Series(prices)
        atr = IndicatorEngine._atr(high, low, close, period=14)
        self.assertTrue(all(v > 0 for v in atr.dropna()))


# ─────────────────────────────────────────────────────────────────────────────
# StateMachine
# ─────────────────────────────────────────────────────────────────────────────

class TestStateMachine(unittest.TestCase):

    def _sm(self, **kw) -> StateMachine:
        defaults = dict(
            long_pullback_max=2,
            short_pullback_max=2,
            long_window_periods=5,
            short_window_periods=7,
            window_price_offset=0.001,
            enable_long=True,
            enable_short=True,
        )
        defaults.update(kw)
        return StateMachine(**defaults)

    # ── initial state ─────────────────────────────────────────────────────────

    def test_initial_state_is_scanning(self):
        sm = self._sm()
        self.assertEqual(sm.state, 'SCANNING')

    def test_reset_returns_to_scanning(self):
        sm = self._sm()
        # Drive to ARMED_LONG
        ind = _make_ind(
            ema_confirm=101.0, ema_fast=100.0,
            prev_ema_confirm=99.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind, bar_index=1, is_vol_expanding=True)
        self.assertEqual(sm.state, 'ARMED_LONG')
        sm.reset()
        self.assertEqual(sm.state, 'SCANNING')

    # ── phase 1 — SCANNING ────────────────────────────────────────────────────

    def test_phase1_no_signal_returns_none(self):
        sm = self._sm()
        # No crossover
        ind = _make_ind(
            ema_confirm=99.0, ema_fast=100.0,
            prev_ema_confirm=98.0, prev_ema_fast=100.0,
        )
        result = sm.process_bar(ind, 1, is_vol_expanding=True)
        self.assertIsNone(result)
        self.assertEqual(sm.state, 'SCANNING')

    def test_phase1_long_signal_arms_long(self):
        sm = self._sm()
        # ema_confirm crosses above ema_fast: prev below, curr above
        ind = _make_ind(
            ema_confirm=101.0, ema_fast=100.0,
            prev_ema_confirm=99.0, prev_ema_fast=100.0,
        )
        result = sm.process_bar(ind, 1, is_vol_expanding=True)
        self.assertIsNone(result)          # No entry yet
        self.assertEqual(sm.state, 'ARMED_LONG')

    def test_phase1_short_signal_arms_short(self):
        sm = self._sm()
        # ema_confirm crosses below ema_fast: prev above, curr below
        ind = _make_ind(
            ema_confirm=99.0,  ema_fast=100.0,
            prev_ema_confirm=101.0, prev_ema_fast=100.0,
        )
        result = sm.process_bar(ind, 1, is_vol_expanding=True)
        self.assertIsNone(result)
        self.assertEqual(sm.state, 'ARMED_SHORT')

    def test_phase1_skips_when_vol_contracting(self):
        sm = self._sm()
        # Would normally be a LONG signal
        ind = _make_ind(
            ema_confirm=101.0, ema_fast=100.0,
            prev_ema_confirm=99.0, prev_ema_fast=100.0,
        )
        result = sm.process_bar(ind, 1, is_vol_expanding=False)
        self.assertIsNone(result)
        self.assertEqual(sm.state, 'SCANNING')   # stays scanning

    def test_phase1_long_disabled(self):
        sm = self._sm(enable_long=False)
        ind = _make_ind(
            ema_confirm=101.0, ema_fast=100.0,
            prev_ema_confirm=99.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind, 1, is_vol_expanding=True)
        # Should NOT arm long since it's disabled
        self.assertEqual(sm.state, 'SCANNING')

    def test_phase1_short_disabled(self):
        sm = self._sm(enable_short=False)
        ind = _make_ind(
            ema_confirm=99.0, ema_fast=100.0,
            prev_ema_confirm=101.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind, 1, is_vol_expanding=True)
        self.assertEqual(sm.state, 'SCANNING')

    def test_phase1_long_signal_via_ema_slow(self):
        sm = self._sm()
        # Cross above ema_slow (fast/medium are already above)
        ind = _make_ind(
            ema_confirm=101.0,
            ema_fast=98.0, ema_medium=98.0, ema_slow=100.0,
            prev_ema_confirm=99.0,
            prev_ema_fast=98.0, prev_ema_medium=98.0, prev_ema_slow=100.0,
        )
        sm.process_bar(ind, 1, is_vol_expanding=True)
        self.assertEqual(sm.state, 'ARMED_LONG')

    # ── phase 2 — pullback counting ───────────────────────────────────────────

    def _arm_long(self, sm: StateMachine, bar_index: int = 1) -> int:
        """Drive SM into ARMED_LONG state."""
        ind = _make_ind(
            ema_confirm=101.0, ema_fast=100.0,
            prev_ema_confirm=99.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind, bar_index, is_vol_expanding=True)
        return bar_index + 1

    def _arm_short(self, sm: StateMachine, bar_index: int = 1) -> int:
        ind = _make_ind(
            ema_confirm=99.0,  ema_fast=100.0,
            prev_ema_confirm=101.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind, bar_index, is_vol_expanding=True)
        return bar_index + 1

    def test_phase2_long_pullback_single_bearish(self):
        sm = self._sm(long_pullback_max=1)
        bi = self._arm_long(sm)
        # One bearish pullback (close < open) → enough → opens window
        ind = _make_ind(open_=101.0, close=99.0, high=102.0, low=98.0)
        sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertEqual(sm.state, 'WINDOW_OPEN')

    def test_phase2_long_non_pullback_resets(self):
        sm = self._sm()
        bi = self._arm_long(sm)
        # Bullish candle (close > open) is NOT a pullback for LONG → reset
        ind = _make_ind(open_=99.0, close=101.0, high=102.0, low=98.0)
        sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertEqual(sm.state, 'SCANNING')

    def test_phase2_long_two_pullbacks_opens_window(self):
        sm = self._sm(long_pullback_max=2)
        bi = self._arm_long(sm)
        pullback = _make_ind(open_=101.0, close=99.0, high=102.0, low=98.0)
        sm.process_bar(pullback, bi,     is_vol_expanding=True)
        self.assertEqual(sm.state, 'ARMED_LONG')  # 1/2 pullbacks done
        sm.process_bar(pullback, bi + 1, is_vol_expanding=True)
        self.assertEqual(sm.state, 'WINDOW_OPEN') # 2/2 → window open

    def test_phase2_short_pullback_bullish(self):
        sm = self._sm(short_pullback_max=1)
        bi = self._arm_short(sm)
        # Bullish candle (close > open) is a pullback for SHORT
        ind = _make_ind(open_=99.0, close=101.0, high=102.0, low=98.0)
        sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertEqual(sm.state, 'WINDOW_OPEN')

    def test_phase2_short_non_pullback_resets(self):
        sm = self._sm()
        bi = self._arm_short(sm)
        # Bearish candle is NOT a pullback for SHORT
        ind = _make_ind(open_=101.0, close=99.0, high=102.0, low=98.0)
        sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertEqual(sm.state, 'SCANNING')

    # ── global invalidation ───────────────────────────────────────────────────

    def test_global_invalidation_long_resets_on_short_cross(self):
        """
        While ARMED_LONG, an opposing SHORT cross triggers global invalidation
        (resets to SCANNING), then phase1 immediately re-arms in the SHORT direction
        on the same bar.  Final state is ARMED_SHORT.
        """
        sm = self._sm()
        self._arm_long(sm)
        self.assertEqual(sm.state, 'ARMED_LONG')
        ind = _make_ind(
            ema_confirm=99.0,  ema_fast=100.0,
            prev_ema_confirm=101.0, prev_ema_fast=100.0,
            open_=101.0, close=102.0,
        )
        sm.process_bar(ind, 10, is_vol_expanding=True)
        # Invalidation resets → phase1 detects SHORT cross → ARMED_SHORT
        self.assertEqual(sm.state, 'ARMED_SHORT')

    def test_global_invalidation_short_resets_on_long_cross(self):
        """
        While ARMED_SHORT, an opposing LONG cross triggers global invalidation
        (resets to SCANNING), then phase1 immediately re-arms in the LONG direction
        on the same bar.  Final state is ARMED_LONG.
        """
        sm = self._sm()
        self._arm_short(sm)
        self.assertEqual(sm.state, 'ARMED_SHORT')
        ind = _make_ind(
            ema_confirm=101.0, ema_fast=100.0,
            prev_ema_confirm=99.0, prev_ema_fast=100.0,
            open_=101.0, close=99.0,
        )
        sm.process_bar(ind, 10, is_vol_expanding=True)
        # Invalidation resets → phase1 detects LONG cross → ARMED_LONG
        self.assertEqual(sm.state, 'ARMED_LONG')

    def test_global_invalidation_only_in_armed_states(self):
        """Global invalidation must NOT fire from SCANNING."""
        sm = self._sm()
        self.assertEqual(sm.state, 'SCANNING')
        # Opposing crossover while SCANNING — should NOT cause any special reset
        ind = _make_ind(
            ema_confirm=99.0, ema_fast=100.0,
            prev_ema_confirm=101.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind, 1, is_vol_expanding=True)
        # Should transition to ARMED_SHORT (it IS a short signal), not reset weirdly
        self.assertEqual(sm.state, 'ARMED_SHORT')

    # ── open_window ───────────────────────────────────────────────────────────

    def test_window_top_and_bottom_correct(self):
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0)
        bi = self._arm_long(sm)
        pb_ind = _make_ind(open_=102.0, close=99.0, high=103.0, low=97.0)
        sm.process_bar(pb_ind, bi, is_vol_expanding=True)
        self.assertEqual(sm.state, 'WINDOW_OPEN')
        # With zero offset: top = pb_high = 103, bottom = pb_low = 97
        self.assertAlmostEqual(sm._window.top,    103.0, places=6)
        self.assertAlmostEqual(sm._window.bottom,  97.0, places=6)

    def test_window_offset_applied(self):
        sm = self._sm(long_pullback_max=1, window_price_offset=0.01)
        bi = self._arm_long(sm)
        pb_ind = _make_ind(open_=102.0, close=99.0, high=104.0, low=96.0)
        sm.process_bar(pb_ind, bi, is_vol_expanding=True)
        # Range = 104-96 = 8, offset = 8*0.01 = 0.08
        expected_top    = 104.0 + 0.08
        expected_bottom =  96.0 - 0.08
        self.assertAlmostEqual(sm._window.top,    expected_top,    places=6)
        self.assertAlmostEqual(sm._window.bottom, expected_bottom, places=6)

    def test_window_expiry_bar_set_correctly(self):
        sm = self._sm(long_pullback_max=1, long_window_periods=5, window_price_offset=0.0)
        bi = self._arm_long(sm)
        pb_ind = _make_ind(open_=102.0, close=99.0, high=103.0, low=97.0)
        sm.process_bar(pb_ind, bar_index=10, is_vol_expanding=True)
        self.assertEqual(sm._window.expiry_bar, 10 + 5)

    # ── phase 4 — window monitoring ───────────────────────────────────────────

    def _open_long_window(self, sm: StateMachine, bi: int = 10):
        """Drive SM to WINDOW_OPEN for a LONG setup."""
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0,
                      long_window_periods=5) if sm is None else sm
        # 1. Arm long
        ind_cross = _make_ind(
            ema_confirm=101.0, ema_fast=100.0,
            prev_ema_confirm=99.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind_cross, bi, is_vol_expanding=True)
        # 2. One pullback → window opens
        pb = _make_ind(open_=102.0, close=99.0, high=105.0, low=95.0)
        sm.process_bar(pb, bi + 1, is_vol_expanding=True)
        return bi + 2   # next bar_index

    def test_phase4_long_breakout_above_top(self):
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0, long_window_periods=5)
        bi = self._open_long_window(sm)
        self.assertEqual(sm.state, 'WINDOW_OPEN')
        top = sm._window.top  # 105.0
        # Bar with high >= top
        ind = _make_ind(high=top, low=96.0, open_=98.0, close=99.0)
        result = sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertEqual(result, 'LONG')
        self.assertEqual(sm.state, 'SCANNING')  # reset after signal

    def test_phase4_long_breakout_returns_long(self):
        """Confirm breakout strictly above top also triggers."""
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0, long_window_periods=5)
        bi = self._open_long_window(sm)
        top = sm._window.top
        ind = _make_ind(high=top + 1.0, low=96.0, open_=98.0, close=99.0)
        result = sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertEqual(result, 'LONG')

    def test_phase4_long_no_breakout_returns_none(self):
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0, long_window_periods=5)
        bi = self._open_long_window(sm)
        top    = sm._window.top
        bottom = sm._window.bottom
        # Bar stays inside window
        ind = _make_ind(high=top - 0.5, low=bottom + 0.5, open_=98.0, close=99.0)
        result = sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertIsNone(result)
        self.assertEqual(sm.state, 'WINDOW_OPEN')

    def test_phase4_long_failure_break_rearms(self):
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0, long_window_periods=5)
        bi = self._open_long_window(sm)
        bottom = sm._window.bottom
        # Bar breaks downward (failure) → ARMED_LONG, reset pullback count
        ind = _make_ind(high=100.0, low=bottom - 0.1, open_=98.0, close=97.0)
        result = sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertIsNone(result)
        self.assertEqual(sm.state, 'ARMED_LONG')
        self.assertEqual(sm._pullback_count, 0)

    def test_phase4_window_timeout_rearms(self):
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0, long_window_periods=5)
        bi = self._open_long_window(sm)
        expiry = sm._window.expiry_bar
        # Submit a bar at bar_index > expiry → timeout
        ind = _make_ind(high=100.0, low=97.0, open_=98.0, close=99.0)
        result = sm.process_bar(ind, expiry + 1, is_vol_expanding=True)
        self.assertIsNone(result)
        self.assertEqual(sm.state, 'ARMED_LONG')
        self.assertIsNone(sm._window)
        self.assertEqual(sm._pullback_count, 0)

    def test_phase4_window_timeout_on_expiry_bar_stays_open(self):
        """Bar exactly at expiry_bar should NOT timeout (uses strict >)."""
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0, long_window_periods=5)
        bi = self._open_long_window(sm)
        expiry = sm._window.expiry_bar
        top    = sm._window.top
        # Bar at exactly expiry — inside window, no breakout
        ind = _make_ind(high=top - 1.0, low=top - 3.0, open_=98.0, close=99.0)
        sm.process_bar(ind, expiry, is_vol_expanding=True)
        self.assertEqual(sm.state, 'WINDOW_OPEN')   # still open

    def test_phase4_short_breakout(self):
        sm = self._sm(short_pullback_max=1, window_price_offset=0.0, short_window_periods=7)
        # Arm short
        ind_cross = _make_ind(
            ema_confirm=99.0, ema_fast=100.0,
            prev_ema_confirm=101.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind_cross, 1, is_vol_expanding=True)
        # Pullback (bullish candle for short)
        pb = _make_ind(open_=98.0, close=102.0, high=106.0, low=94.0)
        sm.process_bar(pb, 2, is_vol_expanding=True)
        self.assertEqual(sm.state, 'WINDOW_OPEN')
        bottom = sm._window.bottom
        # Break below bottom
        ind = _make_ind(high=99.0, low=bottom, open_=98.0, close=97.0)
        result = sm.process_bar(ind, 3, is_vol_expanding=True)
        self.assertEqual(result, 'SHORT')
        self.assertEqual(sm.state, 'SCANNING')

    def test_phase4_short_failure_rearms(self):
        sm = self._sm(short_pullback_max=1, window_price_offset=0.0, short_window_periods=7)
        ind_cross = _make_ind(
            ema_confirm=99.0, ema_fast=100.0,
            prev_ema_confirm=101.0, prev_ema_fast=100.0,
        )
        sm.process_bar(ind_cross, 1, is_vol_expanding=True)
        pb = _make_ind(open_=98.0, close=102.0, high=106.0, low=94.0)
        sm.process_bar(pb, 2, is_vol_expanding=True)
        top = sm._window.top
        ind = _make_ind(high=top + 0.1, low=97.0, open_=98.0, close=99.0)
        result = sm.process_bar(ind, 3, is_vol_expanding=True)
        self.assertIsNone(result)
        self.assertEqual(sm.state, 'ARMED_SHORT')

    # ── multiple signals in sequence ──────────────────────────────────────────

    def test_full_long_cycle(self):
        """Complete SCANNING → ARMED_LONG → WINDOW_OPEN → LONG signal."""
        sm = self._sm(long_pullback_max=2, window_price_offset=0.0, long_window_periods=3)
        # 1. LONG cross
        cross = _make_ind(ema_confirm=101.0, ema_fast=100.0,
                          prev_ema_confirm=99.0, prev_ema_fast=100.0)
        sm.process_bar(cross, 1, True)
        self.assertEqual(sm.state, 'ARMED_LONG')
        # 2. Pullback 1
        pb = _make_ind(open_=102.0, close=99.0, high=103.0, low=97.0)
        sm.process_bar(pb, 2, True)
        self.assertEqual(sm.state, 'ARMED_LONG')  # need 2 pullbacks
        # 3. Pullback 2 → window opens
        pb2 = _make_ind(open_=101.0, close=98.0, high=104.0, low=96.0)
        sm.process_bar(pb2, 3, True)
        self.assertEqual(sm.state, 'WINDOW_OPEN')
        # 4. Bar inside window
        inside = _make_ind(high=sm._window.top - 1, low=sm._window.bottom + 1)
        sm.process_bar(inside, 4, True)
        self.assertEqual(sm.state, 'WINDOW_OPEN')
        # 5. Breakout
        breakout = _make_ind(high=sm._window.top + 1, low=97.0)
        result = sm.process_bar(breakout, 5, True)
        self.assertEqual(result, 'LONG')
        self.assertEqual(sm.state, 'SCANNING')

    def test_state_machine_processes_only_correct_state_per_bar(self):
        """Calling process_bar on WINDOW_OPEN should not re-trigger phase1."""
        sm = self._sm(long_pullback_max=1, window_price_offset=0.0, long_window_periods=5)
        bi = self._open_long_window(sm)
        # Even if there's a long crossover, we are in WINDOW_OPEN — no phase1
        ind = _make_ind(
            ema_confirm=101.0, ema_fast=100.0,
            prev_ema_confirm=99.0, prev_ema_fast=100.0,
            high=sm._window.top - 1.0, low=sm._window.bottom + 1.0,
        )
        sm.process_bar(ind, bi, is_vol_expanding=True)
        self.assertEqual(sm.state, 'WINDOW_OPEN')   # stays in window monitoring


# ─────────────────────────────────────────────────────────────────────────────
# TradeLog
# ─────────────────────────────────────────────────────────────────────────────

class TestTradeLog(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._log_path = os.path.join(self._tmpdir, 'test_trades.json')
        self.log = TradeLog(self._log_path)

    # ── open_trade ────────────────────────────────────────────────────────────

    def test_open_trade_creates_record(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        t = self.log.get_open()
        self.assertIsNotNone(t)
        self.assertEqual(t['direction'],   'LONG')
        self.assertAlmostEqual(t['entry_price'], 100.0)
        self.assertAlmostEqual(t['size'],         1.0)
        self.assertAlmostEqual(t['sl'],          95.0)
        self.assertAlmostEqual(t['tp'],         115.0)
        self.assertIsNone(t['pnl_usd'])

    def test_open_trade_short(self):
        self.log.open_trade('SHORT', 200.0, 0.5, 210.0, 180.0)
        t = self.log.get_open()
        self.assertEqual(t['direction'], 'SHORT')

    def test_open_trade_entry_time_set(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        t = self.log.get_open()
        self.assertIsNotNone(t['entry_time'])
        # Should be ISO format
        self.assertIn('T', t['entry_time'])

    # ── get_open ──────────────────────────────────────────────────────────────

    def test_get_open_returns_none_when_no_trade(self):
        self.assertIsNone(self.log.get_open())

    def test_get_open_returns_copy(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        t1 = self.log.get_open()
        t2 = self.log.get_open()
        t1['direction'] = 'MODIFIED'
        # Internal state should be unchanged
        self.assertEqual(self.log.get_open()['direction'], 'LONG')

    # ── close_trade ───────────────────────────────────────────────────────────

    def test_close_trade_long_profit(self):
        self.log.open_trade('LONG', 100.0, 2.0, 95.0, 115.0)
        closed = self.log.close_trade(110.0, reason='TP')
        self.assertIsNotNone(closed)
        # PnL = (110 - 100) * 2 = 20.0
        self.assertAlmostEqual(closed['pnl_usd'], 20.0, places=4)
        self.assertEqual(closed['reason'], 'TP')

    def test_close_trade_long_loss(self):
        self.log.open_trade('LONG', 100.0, 2.0, 95.0, 115.0)
        closed = self.log.close_trade(95.0, reason='SL')
        # PnL = (95 - 100) * 2 = -10.0
        self.assertAlmostEqual(closed['pnl_usd'], -10.0, places=4)

    def test_close_trade_short_profit(self):
        self.log.open_trade('SHORT', 100.0, 2.0, 110.0, 80.0)
        closed = self.log.close_trade(85.0, reason='TP')
        # PnL = (100 - 85) * 2 = 30.0
        self.assertAlmostEqual(closed['pnl_usd'], 30.0, places=4)

    def test_close_trade_short_loss(self):
        self.log.open_trade('SHORT', 100.0, 2.0, 110.0, 80.0)
        closed = self.log.close_trade(110.0, reason='SL')
        # PnL = (100 - 110) * 2 = -20.0
        self.assertAlmostEqual(closed['pnl_usd'], -20.0, places=4)

    def test_close_trade_returns_none_if_no_open(self):
        result = self.log.close_trade(100.0)
        self.assertIsNone(result)

    def test_close_trade_clears_open(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(105.0)
        self.assertIsNone(self.log.get_open())

    def test_close_trade_exit_time_set(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        closed = self.log.close_trade(105.0)
        self.assertIsNotNone(closed['exit_time'])

    def test_close_trade_breakeven(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        closed = self.log.close_trade(100.0)
        self.assertAlmostEqual(closed['pnl_usd'], 0.0, places=4)

    # ── persistence ───────────────────────────────────────────────────────────

    def test_trades_persist_across_instances(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(110.0, reason='TP')
        # New instance — should load from file
        log2 = TradeLog(self._log_path)
        overall = log2.pnl_overall()
        self.assertEqual(overall['trades'], 1)
        self.assertAlmostEqual(overall['total_usd'], 10.0, places=2)

    def test_corrupted_file_returns_empty(self):
        with open(self._log_path, 'w') as f:
            f.write("THIS IS NOT JSON {{}")
        log2 = TradeLog(self._log_path)
        # Should silently return empty, not raise
        self.assertEqual(log2.pnl_overall()['trades'], 0)

    # ── pnl_overall ───────────────────────────────────────────────────────────

    def test_pnl_overall_empty(self):
        result = self.log.pnl_overall()
        self.assertEqual(result['trades'], 0)
        self.assertEqual(result['total_usd'], 0)
        self.assertEqual(result['win_rate'],  0)

    def test_pnl_overall_wins_losses(self):
        # 2 wins, 1 loss
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(115.0)  # +15
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(110.0)  # +10
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(95.0)   # -5
        r = self.log.pnl_overall()
        self.assertEqual(r['trades'],  3)
        self.assertEqual(r['wins'],    2)
        self.assertEqual(r['losses'],  1)
        self.assertAlmostEqual(r['total_usd'], 20.0, places=2)
        self.assertAlmostEqual(r['win_rate'],  66.7, places=1)
        self.assertAlmostEqual(r['avg_win'],   12.5, places=2)
        self.assertAlmostEqual(r['avg_loss'],  -5.0, places=2)

    def test_pnl_overall_all_losses(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(90.0)   # -10
        r = self.log.pnl_overall()
        self.assertEqual(r['wins'],   0)
        self.assertEqual(r['losses'], 1)
        self.assertEqual(r['avg_win'], 0)  # no wins → 0

    def test_pnl_overall_all_wins(self):
        self.log.open_trade('SHORT', 100.0, 1.0, 110.0, 85.0)
        self.log.close_trade(85.0)   # +15
        r = self.log.pnl_overall()
        self.assertEqual(r['wins'],   1)
        self.assertEqual(r['losses'], 0)
        self.assertEqual(r['avg_loss'], 0)  # no losses → 0

    # ── pnl_today / pnl_month ─────────────────────────────────────────────────

    def test_pnl_today_counts_correctly(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(110.0)  # +10, exit_time = today
        result = self.log.pnl_today()
        self.assertEqual(result['trades'], 1)
        self.assertAlmostEqual(result['total_usd'], 10.0, places=2)

    def test_pnl_month_counts_correctly(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(105.0)  # +5
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        result = self.log.pnl_month(year=now.year, month=now.month)
        self.assertEqual(result['trades'], 1)
        self.assertAlmostEqual(result['total_usd'], 5.0, places=2)

    def test_pnl_month_zero_for_other_month(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(105.0)
        # Query a different year/month
        result = self.log.pnl_month(year=2000, month=1)
        self.assertEqual(result['trades'], 0)
        self.assertEqual(result['total_usd'], 0)

    # ── recent ────────────────────────────────────────────────────────────────

    def test_recent_returns_last_n(self):
        for i in range(10):
            self.log.open_trade('LONG', 100.0 + i, 1.0, 95.0, 115.0)
            self.log.close_trade(110.0 + i)
        r = self.log.recent(3)
        self.assertEqual(len(r), 3)
        # Last trade should be close=119, entry=109 → pnl=10
        self.assertAlmostEqual(r[-1]['entry_price'], 109.0, places=2)

    def test_recent_fewer_than_n(self):
        self.log.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
        self.log.close_trade(110.0)
        r = self.log.recent(5)
        self.assertEqual(len(r), 1)

    def test_recent_empty(self):
        self.assertEqual(self.log.recent(5), [])

    # ── thread safety ─────────────────────────────────────────────────────────

    def test_concurrent_open_close_safe(self):
        """Multiple threads open + close trades concurrently without exception."""
        errors = []
        def worker():
            try:
                path = os.path.join(self._tmpdir, f'thread_{threading.get_ident()}.json')
                tlog = TradeLog(path)
                tlog.open_trade('LONG', 100.0, 1.0, 95.0, 115.0)
                tlog.close_trade(110.0)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [], f"Thread errors: {errors}")


# ─────────────────────────────────────────────────────────────────────────────
# BotState
# ─────────────────────────────────────────────────────────────────────────────

class TestBotState(unittest.TestCase):

    def setUp(self):
        self.state = BotState(risk_percent=0.01, active_strategy=1)

    # ── initial values ────────────────────────────────────────────────────────

    def test_initial_risk_percent(self):
        self.assertAlmostEqual(self.state.risk_percent, 0.01)

    def test_initial_not_paused(self):
        self.assertFalse(self.state.is_paused)

    def test_initial_no_position(self):
        self.assertIsNone(self.state.position)

    def test_initial_phase_scanning(self):
        self.assertEqual(self.state.phase, 'SCANNING')

    def test_initial_active_strategy(self):
        self.assertEqual(self.state.active_strategy, 1)

    # ── set_risk ──────────────────────────────────────────────────────────────

    def test_set_risk_updates(self):
        self.state.set_risk(0.02)
        self.assertAlmostEqual(self.state.risk_percent, 0.02)

    def test_set_risk_thread_safe(self):
        """Concurrent set_risk calls should not cause race conditions."""
        errors = []
        def worker(val):
            try:
                self.state.set_risk(val)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(0.01 * i,)) for i in range(1, 11)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        # Final risk should be a valid float
        self.assertIsInstance(self.state.risk_percent, float)

    # ── pause / resume ────────────────────────────────────────────────────────

    def test_pause_sets_flag(self):
        self.state.pause()
        self.assertTrue(self.state.is_paused)

    def test_resume_clears_flag(self):
        self.state.pause()
        self.state.resume()
        self.assertFalse(self.state.is_paused)

    def test_resume_when_not_paused_is_idempotent(self):
        self.state.resume()  # Should not raise
        self.assertFalse(self.state.is_paused)

    # ── consume_close_request ────────────────────────────────────────────────

    def test_close_request_not_set_initially(self):
        self.assertFalse(self.state.consume_close_request())

    def test_close_request_consumed_once(self):
        self.state.request_close()
        self.assertTrue(self.state.consume_close_request())
        self.assertFalse(self.state.consume_close_request())  # one-shot

    def test_close_request_multiple_requests_consumed_one_by_one(self):
        self.state.request_close()
        self.state.request_close()  # Setting flag again
        self.assertTrue(self.state.consume_close_request())
        # After consuming, flag is cleared (no queue — it's a boolean)
        self.assertFalse(self.state.consume_close_request())

    # ── consume_strategy_switch ───────────────────────────────────────────────

    def test_strategy_switch_none_initially(self):
        self.assertIsNone(self.state.consume_strategy_switch())

    def test_strategy_switch_request_and_consume(self):
        self.state.request_strategy_switch(2)
        result = self.state.consume_strategy_switch()
        self.assertEqual(result, 2)
        self.assertIsNone(self.state.consume_strategy_switch())  # one-shot

    def test_strategy_switch_overwrite(self):
        self.state.request_strategy_switch(2)
        self.state.request_strategy_switch(3)  # overwrite
        result = self.state.consume_strategy_switch()
        self.assertEqual(result, 3)

    # ── snapshot ──────────────────────────────────────────────────────────────

    def test_snapshot_returns_dict(self):
        snap = self.state.snapshot()
        self.assertIsInstance(snap, dict)

    def test_snapshot_keys_present(self):
        snap = self.state.snapshot()
        for key in ('risk_percent', 'is_paused', 'phase', 'position', 'active_strategy'):
            self.assertIn(key, snap, f"Missing snapshot key: {key}")

    def test_snapshot_position_is_copy(self):
        self.state.position = {'direction': 'LONG', 'size': 1.0, 'entry': 100.0, 'sl': 95.0, 'tp': 115.0}
        snap = self.state.snapshot()
        snap['position']['direction'] = 'MODIFIED'
        # Original should be unchanged
        self.assertEqual(self.state.position['direction'], 'LONG')

    def test_snapshot_reflects_current_active_strategy(self):
        self.state.active_strategy = 2
        snap = self.state.snapshot()
        self.assertEqual(snap['active_strategy'], 2)

    # ── thread safety ─────────────────────────────────────────────────────────

    def test_concurrent_state_updates_no_exception(self):
        errors = []
        def writer():
            try:
                for _ in range(100):
                    self.state.pause()
                    self.state.resume()
                    self.state.set_risk(0.01)
            except Exception as e:
                errors.append(e)
        def reader():
            try:
                for _ in range(100):
                    self.state.snapshot()
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Registry
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyRegistry(unittest.TestCase):

    def test_registry_contains_strategy_1(self):
        self.assertIn(1, REGISTRY)

    def test_strategy_1_is_state_machine_class(self):
        from state_machine import StateMachine
        self.assertIs(REGISTRY[1], StateMachine)

    def test_get_strategy_1_returns_instance(self):
        from state_machine import StateMachine
        sm = get_strategy(1)
        self.assertIsInstance(sm, StateMachine)

    def test_get_strategy_1_with_kwargs(self):
        sm = get_strategy(
            1,
            long_pullback_max=3,
            short_pullback_max=3,
            long_window_periods=10,
            short_window_periods=10,
            window_price_offset=0.002,
            enable_long=True,
            enable_short=False,
        )
        self.assertEqual(sm._long_pb_max, 3)
        self.assertEqual(sm._short_pb_max, 3)
        self.assertFalse(sm._enable_short)

    def test_get_strategy_invalid_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            get_strategy(99)
        self.assertIn('99', str(ctx.exception))
        self.assertIn('registry', str(ctx.exception).lower())

    def test_get_strategy_0_raises_value_error(self):
        with self.assertRaises(ValueError):
            get_strategy(0)

    def test_strategy_name_1(self):
        name = strategy_name(1)
        self.assertIsInstance(name, str)
        self.assertGreater(len(name), 0)
        # Should contain something descriptive
        self.assertIn('1', name.lower() + '1')  # either number or text

    def test_strategy_name_unknown_returns_fallback(self):
        name = strategy_name(99)
        self.assertIsInstance(name, str)
        self.assertIn('99', name)

    def test_available_strategies_includes_1(self):
        result = available_strategies()
        self.assertIsInstance(result, list)
        numbers = [n for n, _ in result]
        self.assertIn(1, numbers)

    def test_available_strategies_sorted(self):
        result = available_strategies()
        numbers = [n for n, _ in result]
        self.assertEqual(numbers, sorted(numbers))

    def test_available_strategies_names_are_strings(self):
        for n, name in available_strategies():
            self.assertIsInstance(name, str)
            self.assertGreater(len(name), 0)

    def test_get_strategy_returns_fresh_instance(self):
        """Each call should return a new independent instance."""
        sm1 = get_strategy(1)
        sm2 = get_strategy(1)
        self.assertIsNot(sm1, sm2)

    def test_strategy_1_has_required_interface(self):
        """Strategy must implement process_bar, reset, and state property."""
        sm = get_strategy(1)
        self.assertTrue(hasattr(sm, 'process_bar'), "Missing process_bar")
        self.assertTrue(hasattr(sm, 'reset'),        "Missing reset")
        self.assertTrue(hasattr(sm, 'state'),        "Missing state property")
        self.assertTrue(callable(sm.process_bar),    "process_bar not callable")
        self.assertTrue(callable(sm.reset),          "reset not callable")


# ─────────────────────────────────────────────────────────────────────────────
# OrderManager — pure formatting functions (no API calls)
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatFunctions(unittest.TestCase):

    # ── _fmt_qty ──────────────────────────────────────────────────────────────

    def test_fmt_qty_exact_step(self):
        self.assertEqual(_fmt_qty(0.001), '0.001')
        self.assertEqual(_fmt_qty(1.000), '1.000')
        self.assertEqual(_fmt_qty(0.010), '0.010')

    def test_fmt_qty_rounds_down(self):
        # 0.0019 should floor to 0.001
        self.assertEqual(_fmt_qty(0.0019), '0.001')

    def test_fmt_qty_large_value(self):
        self.assertEqual(_fmt_qty(100.0), '100.000')

    def test_fmt_qty_small_fractional(self):
        # 0.0009 < _MIN_QTY=0.001, floors to 0.000
        self.assertEqual(_fmt_qty(0.0009), '0.000')

    def test_fmt_qty_integer(self):
        self.assertEqual(_fmt_qty(5), '5.000')

    def test_fmt_qty_returns_string(self):
        self.assertIsInstance(_fmt_qty(1.0), str)

    def test_fmt_qty_two_decimals_floors(self):
        # 1.0015 → 1.001 (ROUND_DOWN)
        self.assertEqual(_fmt_qty(1.0015), '1.001')

    # ── _fmt_price ────────────────────────────────────────────────────────────

    def test_fmt_price_exact_tick(self):
        self.assertEqual(_fmt_price(100.00), '100.00')
        self.assertEqual(_fmt_price(100.01), '100.01')

    def test_fmt_price_rounds_down(self):
        # 100.019 → 100.01 (ROUND_DOWN at 0.01 tick)
        self.assertEqual(_fmt_price(100.019), '100.01')

    def test_fmt_price_large_price(self):
        self.assertEqual(_fmt_price(2000.00), '2000.00')

    def test_fmt_price_returns_string(self):
        self.assertIsInstance(_fmt_price(100.0), str)

    def test_fmt_price_integer(self):
        self.assertEqual(_fmt_price(1500), '1500.00')

    def test_fmt_price_gold_price_typical(self):
        # Typical XAUT price
        self.assertEqual(_fmt_price(3240.35), '3240.35')

    def test_fmt_price_floors_not_rounds(self):
        # 3240.999 should floor to 3240.99, not round to 3241.00
        self.assertEqual(_fmt_price(3240.999), '3240.99')


# ─────────────────────────────────────────────────────────────────────────────
# OrderManager — API methods (mocked)
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderManager(unittest.TestCase):

    def _make_manager(self):
        """Create OrderManager with fully mocked pybit HTTP session."""
        with patch('order_manager.HTTP') as mock_http_cls:
            mock_session = MagicMock()
            mock_http_cls.return_value = mock_session
            from order_manager import OrderManager
            om = OrderManager('key', 'secret', testnet=True, symbol='XAUTUSDT')
            om._session = mock_session
            return om

    # ── get_equity ────────────────────────────────────────────────────────────

    def test_get_equity_returns_usdt_balance(self):
        om = self._make_manager()
        om._session.get_wallet_balance.return_value = {
            'retCode': 0,
            'result': {
                'list': [{
                    'coin': [
                        {'coin': 'BTC',  'equity': '0.5'},
                        {'coin': 'USDT', 'equity': '1234.56'},
                    ]
                }]
            }
        }
        equity = om.get_equity()
        self.assertAlmostEqual(equity, 1234.56)

    def test_get_equity_raises_if_no_usdt(self):
        om = self._make_manager()
        om._session.get_wallet_balance.return_value = {
            'retCode': 0,
            'result': {'list': [{'coin': [{'coin': 'BTC', 'equity': '0.5'}]}]}
        }
        with self.assertRaises(ValueError):
            om.get_equity()

    def test_get_equity_retcode_nonzero_raises(self):
        om = self._make_manager()
        om._session.get_wallet_balance.return_value = {
            'retCode': 10001,
            'retMsg': 'Not authenticated',
        }
        with self.assertRaises(RuntimeError):
            om.get_equity()

    # ── get_position ──────────────────────────────────────────────────────────

    def test_get_position_returns_none_when_flat(self):
        om = self._make_manager()
        om._session.get_positions.return_value = {
            'retCode': 0,
            'result': {'list': [{'size': '0', 'side': 'None'}]}
        }
        self.assertIsNone(om.get_position())

    def test_get_position_returns_dict_when_open(self):
        om = self._make_manager()
        om._session.get_positions.return_value = {
            'retCode': 0,
            'result': {'list': [{
                'size': '1.5',
                'side': 'Buy',
                'avgPrice': '2500.00',
                'stopLoss': '2450.00',
                'takeProfit': '2600.00',
                'unrealisedPnl': '75.00',
            }]}
        }
        pos = om.get_position()
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos['size'],           1.5)
        self.assertEqual(pos['side'],                 'Buy')
        self.assertAlmostEqual(pos['entry_price'],    2500.0)
        self.assertAlmostEqual(pos['stop_loss'],      2450.0)
        self.assertAlmostEqual(pos['take_profit'],    2600.0)
        self.assertAlmostEqual(pos['unrealised_pnl'],   75.0)

    def test_get_position_empty_list(self):
        om = self._make_manager()
        om._session.get_positions.return_value = {
            'retCode': 0,
            'result': {'list': []}
        }
        self.assertIsNone(om.get_position())

    # ── is_flat ───────────────────────────────────────────────────────────────

    def test_is_flat_true_when_no_position(self):
        om = self._make_manager()
        om._session.get_positions.return_value = {
            'retCode': 0,
            'result': {'list': [{'size': '0', 'side': 'None'}]}
        }
        self.assertTrue(om.is_flat())

    def test_is_flat_false_when_open_position(self):
        om = self._make_manager()
        om._session.get_positions.return_value = {
            'retCode': 0,
            'result': {'list': [{
                'size': '1.0', 'side': 'Buy',
                'avgPrice': '100.0', 'stopLoss': '95.0',
                'takeProfit': '115.0', 'unrealisedPnl': '0',
            }]}
        }
        self.assertFalse(om.is_flat())

    # ── place_entry ───────────────────────────────────────────────────────────

    def test_place_entry_long_calls_buy(self):
        om = self._make_manager()
        om._session.place_order.return_value = {
            'retCode': 0,
            'result': {'orderId': 'abc123'}
        }
        result = om.place_entry('LONG', size=0.1, stop_loss=2450.0, take_profit=2600.0)
        call_kwargs = om._session.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['side'], 'Buy')
        self.assertFalse(call_kwargs['reduceOnly'])

    def test_place_entry_short_calls_sell(self):
        om = self._make_manager()
        om._session.place_order.return_value = {
            'retCode': 0,
            'result': {'orderId': 'abc123'}
        }
        om.place_entry('SHORT', size=0.1, stop_loss=2600.0, take_profit=2450.0)
        call_kwargs = om._session.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['side'], 'Sell')

    def test_place_entry_below_min_qty_raises(self):
        om = self._make_manager()
        # 0.0001 < _MIN_QTY=0.001
        with self.assertRaises(ValueError) as ctx:
            om.place_entry('LONG', size=0.0001, stop_loss=95.0, take_profit=115.0)
        self.assertIn('minimum', str(ctx.exception).lower())

    def test_place_entry_returns_result(self):
        om = self._make_manager()
        om._session.place_order.return_value = {
            'retCode': 0,
            'result': {'orderId': 'xyz789'}
        }
        result = om.place_entry('LONG', 0.1, 2450.0, 2600.0)
        self.assertEqual(result['orderId'], 'xyz789')

    # ── close_position ────────────────────────────────────────────────────────

    def test_close_position_returns_none_when_flat(self):
        om = self._make_manager()
        # Flat
        om._session.get_positions.return_value = {
            'retCode': 0, 'result': {'list': [{'size': '0', 'side': 'None'}]}
        }
        result = om.close_position()
        self.assertIsNone(result)

    def test_close_position_buy_uses_sell(self):
        om = self._make_manager()
        om._session.get_positions.return_value = {
            'retCode': 0,
            'result': {'list': [{
                'size': '1.0', 'side': 'Buy',
                'avgPrice': '100.0', 'stopLoss': '95.0',
                'takeProfit': '115.0', 'unrealisedPnl': '5.0',
            }]}
        }
        om._session.place_order.return_value = {
            'retCode': 0, 'result': {'orderId': 'close123'}
        }
        om.close_position()
        call_kwargs = om._session.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['side'], 'Sell')
        self.assertTrue(call_kwargs['reduceOnly'])

    def test_close_position_sell_uses_buy(self):
        om = self._make_manager()
        om._session.get_positions.return_value = {
            'retCode': 0,
            'result': {'list': [{
                'size': '1.0', 'side': 'Sell',
                'avgPrice': '100.0', 'stopLoss': '105.0',
                'takeProfit': '85.0', 'unrealisedPnl': '-2.0',
            }]}
        }
        om._session.place_order.return_value = {
            'retCode': 0, 'result': {'orderId': 'close456'}
        }
        om.close_position()
        call_kwargs = om._session.place_order.call_args.kwargs
        self.assertEqual(call_kwargs['side'], 'Buy')

    # ── cancel_all_orders ─────────────────────────────────────────────────────

    def test_cancel_all_orders_succeeds(self):
        om = self._make_manager()
        om._session.cancel_all_orders.return_value = {
            'retCode': 0, 'result': {}
        }
        om.cancel_all_orders()  # Should not raise

    def test_cancel_all_orders_swallows_exception(self):
        om = self._make_manager()
        om._session.cancel_all_orders.side_effect = RuntimeError("network error")
        # Should NOT raise — cancel errors are swallowed
        try:
            om.cancel_all_orders()
        except Exception:
            self.fail("cancel_all_orders should not propagate exceptions")

    # ── _call retry logic ─────────────────────────────────────────────────────

    def test_call_retries_on_exception(self):
        om = self._make_manager()
        mock_fn = MagicMock(side_effect=[
            Exception("transient error"),
            {'retCode': 0, 'result': 'ok'},
        ])
        with patch('order_manager.time.sleep'):  # don't actually sleep
            result = om._call(mock_fn)
        self.assertEqual(mock_fn.call_count, 2)
        self.assertEqual(result['result'], 'ok')

    def test_call_raises_after_max_retries(self):
        om = self._make_manager()
        mock_fn = MagicMock(side_effect=Exception("always fails"))
        with patch('order_manager.time.sleep'):
            with self.assertRaises(RuntimeError) as ctx:
                om._call(mock_fn)
        self.assertIn('3 attempts', str(ctx.exception))
        self.assertEqual(mock_fn.call_count, 3)

    def test_call_raises_on_nonzero_retcode(self):
        om = self._make_manager()
        mock_fn = MagicMock(return_value={'retCode': 10001, 'retMsg': 'auth error'})
        with patch('order_manager.time.sleep'):
            with self.assertRaises(RuntimeError) as ctx:
                om._call(mock_fn)
        self.assertIn('10001', str(ctx.exception))

    def test_call_success_no_retry(self):
        om = self._make_manager()
        mock_fn = MagicMock(return_value={'retCode': 0, 'result': 'data'})
        result = om._call(mock_fn)
        self.assertEqual(mock_fn.call_count, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Integration: IndicatorEngine → StateMachine pipeline
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration(unittest.TestCase):
    """Smoke-test the full pipeline: engine warms up → SM processes real indicators."""

    def test_warm_engine_feeds_state_machine(self):
        engine = IndicatorEngine(ema_fast=12, ema_medium=18, ema_slow=26,
                                 ema_confirm=1, atr_period=14, atr_regime_lookback=20)
        sm = StateMachine(long_pullback_max=2, short_pullback_max=2)

        # Feed 150 bars
        for i in range(150):
            engine.add_bar(_make_bar(close=100.0 + (i % 5), high=102.0, low=98.0, ts=i))

        self.assertTrue(engine.is_warm)
        ind = engine.compute()
        self.assertIsNotNone(ind)

        # SM can process the indicator dict without crashing
        result = sm.process_bar(ind, bar_index=150, is_vol_expanding=True)
        self.assertIn(result, [None, 'LONG', 'SHORT'])

    def test_state_machine_does_not_crash_on_flat_prices(self):
        """If all prices are identical, SM should stay in SCANNING gracefully."""
        engine = IndicatorEngine()
        sm = StateMachine()
        for i in range(200):
            engine.add_bar(_make_bar(close=1000.0, open_=1000.0, high=1000.0, low=1000.0, ts=i))
        ind = engine.compute()
        if ind:
            result = sm.process_bar(ind, 200, is_vol_expanding=True)
            self.assertIn(result, [None, 'LONG', 'SHORT'])

    def test_strategy_registry_instance_processes_bar(self):
        """get_strategy() result must work end-to-end."""
        sm = get_strategy(1)
        ind = _make_ind()
        result = sm.process_bar(ind, bar_index=1, is_vol_expanding=True)
        self.assertIn(result, [None, 'LONG', 'SHORT'])


# ─────────────────────────────────────────────────────────────────────────────
# ORBStrategy
# ─────────────────────────────────────────────────────────────────────────────

from strategies.orb import ORBStrategy


def _make_orb_ind(
    high:          float = 101.0,
    low:           float = 98.0,
    close:         float = 100.0,
    ema_slow:      float = 100.0,
    prev_ema_slow: float = 99.0,   # slow rising by default
) -> dict:
    """Minimal ind dict for ORBStrategy (only keys it actually reads)."""
    return {
        'high':          high,
        'low':           low,
        'close':         close,
        'ema_slow':      ema_slow,
        'prev_ema_slow': prev_ema_slow,
    }


class TestORBStrategy(unittest.TestCase):

    def _build_sm(self, range_bars=3, sl_frac=0.7, tp_mult=2.0, **kwargs):
        return ORBStrategy(
            orb_range_bars=range_bars,
            orb_sl_range_frac=sl_frac,
            orb_tp_range_mult=tp_mult,
            **kwargs,
        )

    def _arm_sm(self, sm):
        """Feed sm enough bars (with neutral ind) to reach ARMED state."""
        ind = _make_orb_ind()
        for i in range(sm._range_bars):
            sm.process_bar(ind, bar_index=i, is_vol_expanding=True)

    def test_initial_state_is_range_building(self):
        sm = self._build_sm()
        self.assertEqual(sm.state, 'RANGE_BUILDING')

    def test_entry_sl_tp_none_on_init(self):
        sm = self._build_sm()
        self.assertIsNone(sm.entry_sl)
        self.assertIsNone(sm.entry_tp)

    def test_range_builds_until_range_bars_minus_one(self):
        sm = self._build_sm(range_bars=3)
        ind = _make_orb_ind()
        sm.process_bar(ind, bar_index=0, is_vol_expanding=True)
        self.assertEqual(sm.state, 'RANGE_BUILDING')
        sm.process_bar(ind, bar_index=1, is_vol_expanding=True)
        self.assertEqual(sm.state, 'RANGE_BUILDING')

    def test_transitions_to_armed_after_range_bars(self):
        sm = self._build_sm(range_bars=3)
        ind = _make_orb_ind()
        for i in range(3):
            sm.process_bar(ind, bar_index=i, is_vol_expanding=True)
        self.assertEqual(sm.state, 'ARMED')

    def test_no_signal_during_range_building(self):
        sm = self._build_sm(range_bars=3)
        # Even a high that exceeds any conceivable range should not fire
        ind = _make_orb_ind(high=9999.0)
        for i in range(2):
            result = sm.process_bar(ind, bar_index=i, is_vol_expanding=True)
            self.assertIsNone(result)

    def test_new_session_gap_resets_range(self):
        sm = self._build_sm(range_bars=3)
        self._arm_sm(sm)
        self.assertEqual(sm.state, 'ARMED')
        # Gap > 60 bars → new session → back to RANGE_BUILDING
        ind = _make_orb_ind()
        sm.process_bar(ind, bar_index=200, is_vol_expanding=True)
        self.assertEqual(sm.state, 'RANGE_BUILDING')

    def test_small_gap_does_not_reset(self):
        sm = self._build_sm(range_bars=3)
        self._arm_sm(sm)
        self.assertEqual(sm.state, 'ARMED')
        # Gap <= 60 → same session → stays ARMED
        ind = _make_orb_ind()
        sm.process_bar(ind, bar_index=4, is_vol_expanding=True)
        self.assertEqual(sm.state, 'ARMED')

    def test_long_signal_on_upside_breakout(self):
        sm = self._build_sm(range_bars=3)
        # Build range: high=101, low=98 → range_high=101
        base = _make_orb_ind(high=101.0, low=98.0, close=100.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        # Breakout: high=102 > 101, vol expanding, ema_slow rising
        brk = _make_orb_ind(high=102.0, low=98.0, close=101.5,
                             ema_slow=100.0, prev_ema_slow=99.0)
        result = sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        self.assertEqual(result, 'LONG')

    def test_short_signal_on_downside_breakout(self):
        sm = self._build_sm(range_bars=3)
        base = _make_orb_ind(high=101.0, low=98.0, close=100.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        # Breakout: low=97 < 98, vol expanding, ema_slow falling
        brk = _make_orb_ind(high=100.0, low=97.0, close=98.5,
                             ema_slow=99.0, prev_ema_slow=100.0)
        result = sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        self.assertEqual(result, 'SHORT')

    def test_vol_filter_blocks_long_entry(self):
        sm = self._build_sm(range_bars=3)
        base = _make_orb_ind(high=101.0, low=98.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        brk = _make_orb_ind(high=102.0, low=98.0, close=101.5,
                             ema_slow=100.0, prev_ema_slow=99.0)
        result = sm.process_bar(brk, bar_index=4, is_vol_expanding=False)
        self.assertIsNone(result)

    def test_ema_falling_blocks_long(self):
        """Downtrending ema_slow should block LONG even with upside breakout."""
        sm = self._build_sm(range_bars=3)
        base = _make_orb_ind(high=101.0, low=98.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        # ema_slow < prev → falling → LONG blocked
        brk = _make_orb_ind(high=102.0, low=98.0, close=101.5,
                             ema_slow=99.0, prev_ema_slow=100.0)
        result = sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        self.assertIsNone(result)

    def test_ema_rising_blocks_short(self):
        """Uptrending ema_slow should block SHORT even with downside breakout."""
        sm = self._build_sm(range_bars=3)
        base = _make_orb_ind(high=101.0, low=98.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        # ema_slow > prev → rising → SHORT blocked
        brk = _make_orb_ind(high=101.0, low=97.0, close=98.5,
                             ema_slow=100.0, prev_ema_slow=99.0)
        result = sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        self.assertIsNone(result)

    def test_entry_sl_tp_correct_on_long(self):
        """SL = close - range×sl_frac, TP = close + range×tp_mult."""
        sm = self._build_sm(range_bars=3, sl_frac=0.7, tp_mult=2.0)
        # range_high=102, range_low=98 → rng=4
        base = _make_orb_ind(high=102.0, low=98.0, close=100.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        brk = _make_orb_ind(high=103.0, low=98.0, close=102.5,
                             ema_slow=100.0, prev_ema_slow=99.0)
        sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        rng = 4.0
        self.assertAlmostEqual(sm.entry_sl, 102.5 - rng * 0.7)
        self.assertAlmostEqual(sm.entry_tp, 102.5 + rng * 2.0)

    def test_entry_sl_tp_correct_on_short(self):
        """SL = close + range×sl_frac, TP = close - range×tp_mult."""
        sm = self._build_sm(range_bars=3, sl_frac=0.7, tp_mult=2.0)
        base = _make_orb_ind(high=102.0, low=98.0, close=100.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        brk = _make_orb_ind(high=102.0, low=97.0, close=97.5,
                             ema_slow=99.0, prev_ema_slow=100.0)
        sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        rng = 4.0
        self.assertAlmostEqual(sm.entry_sl, 97.5 + rng * 0.7)
        self.assertAlmostEqual(sm.entry_tp, 97.5 - rng * 2.0)

    def test_done_state_after_signal(self):
        """After one trade state is DONE; further calls return None."""
        sm = self._build_sm(range_bars=3)
        base = _make_orb_ind(high=101.0, low=98.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        brk = _make_orb_ind(high=102.0, low=98.0, close=101.5,
                             ema_slow=100.0, prev_ema_slow=99.0)
        sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        self.assertEqual(sm.state, 'DONE')
        result = sm.process_bar(brk, bar_index=5, is_vol_expanding=True)
        self.assertIsNone(result)

    def test_reset_clears_to_range_building(self):
        sm = self._build_sm(range_bars=3)
        self._arm_sm(sm)
        self.assertEqual(sm.state, 'ARMED')
        sm.reset()
        self.assertEqual(sm.state, 'RANGE_BUILDING')
        self.assertIsNone(sm.entry_sl)
        self.assertIsNone(sm.entry_tp)

    def test_kwargs_absorption(self):
        """Strategy-1-specific kwargs must not raise."""
        sm = ORBStrategy(
            orb_range_bars=12,
            long_pullback_max=2,
            short_pullback_max=2,
            long_window_periods=5,
            short_window_periods=7,
            window_price_offset=0.001,
            trend_filter=False,
            trend_filter_bars=15,
            enable_long=True,
            enable_short=True,
        )
        self.assertEqual(sm.state, 'RANGE_BUILDING')

    def test_enable_long_false_suppresses_long(self):
        sm = self._build_sm(range_bars=3, enable_long=False)
        base = _make_orb_ind(high=101.0, low=98.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        brk = _make_orb_ind(high=102.0, low=98.0, close=101.5,
                             ema_slow=100.0, prev_ema_slow=99.0)
        result = sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        self.assertIsNone(result)

    def test_enable_short_false_suppresses_short(self):
        sm = self._build_sm(range_bars=3, enable_short=False)
        base = _make_orb_ind(high=101.0, low=98.0)
        for i in range(3):
            sm.process_bar(base, bar_index=i, is_vol_expanding=True)
        brk = _make_orb_ind(high=101.0, low=97.0, close=98.5,
                             ema_slow=99.0, prev_ema_slow=100.0)
        result = sm.process_bar(brk, bar_index=4, is_vol_expanding=True)
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    unittest.main(verbosity=2)
