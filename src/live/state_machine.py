"""
state_machine.py — 4-phase entry state machine
================================================
Direct port of the Volatility Expansion Channel entry system from
SunriseOgleXAUT (Backtrader) into a pure-Python class with no Backtrader
dependency.

Phases
------
  SCANNING     → detect EMA crossover signal (LONG or SHORT)
  ARMED_LONG   → count bearish pullback candles (LONG setup)
  ARMED_SHORT  → count bullish pullback candles (SHORT setup)
  WINDOW_OPEN  → monitor two-sided channel for breakout or failure

On every closed bar, call process_bar().  It returns:
  'LONG'  — enter long position now
  'SHORT' — enter short position now
  None    — no action

Global Invalidation Rule
------------------------
If an opposing EMA crossover appears while ARMED, the setup is cancelled
and the machine resets to SCANNING, exactly as in the backtest strategy.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

from indicators import IndicatorEngine

log = logging.getLogger(__name__)


@dataclass
class _Window:
    """Tracks the two-sided breakout channel for PHASE 4."""
    top:        float
    bottom:     float
    expiry_bar: int      # bar_index at which the window times out


class StateMachine:
    """
    4-phase Volatility Expansion Channel state machine.
    Designed to be called once per closed 5-min candle.
    """

    def __init__(
        self,
        long_pullback_max:    int   = 2,
        short_pullback_max:   int   = 2,
        long_window_periods:  int   = 5,
        short_window_periods: int   = 7,
        window_price_offset:  float = 0.001,
        enable_long:          bool  = True,
        enable_short:         bool  = True,
        trend_filter:         bool  = False,
        trend_filter_bars:    int   = 15,
    ) -> None:
        self._long_pb_max    = long_pullback_max
        self._short_pb_max   = short_pullback_max
        self._long_win       = long_window_periods
        self._short_win      = short_window_periods
        self._price_offset   = window_price_offset
        self._enable_long    = enable_long
        self._enable_short   = enable_short
        self._trend_filter      = trend_filter
        self._trend_filter_bars = trend_filter_bars
        # Rolling EMA-slow history for trend filter; persists across state resets
        self._ema_slow_hist: deque = deque(maxlen=trend_filter_bars + 1)

        self._reset()

    # ── public API ────────────────────────────────────────────────────────────

    def process_bar(
        self,
        ind:              dict,
        bar_index:        int,
        is_vol_expanding: bool,
    ) -> str | None:
        """
        Process one closed candle.

        Parameters
        ----------
        ind              : dict from IndicatorEngine.compute()
        bar_index        : monotonically increasing bar counter
        is_vol_expanding : True when ATR > EMA-of-ATR (or regime filter off)

        Returns 'LONG', 'SHORT', or None.
        """
        # Track EMA-slow for trend filter (persists across state resets)
        if self._trend_filter:
            self._ema_slow_hist.append(ind['ema_slow'])

        # Global invalidation must run before the state router
        self._check_global_invalidation(ind)

        if self.state == 'SCANNING':
            return self._phase1(ind, bar_index, is_vol_expanding)

        if self.state in ('ARMED_LONG', 'ARMED_SHORT'):
            return self._phase2(ind, bar_index)

        if self.state == 'WINDOW_OPEN':
            return self._phase4(ind, bar_index)

        return None

    def reset(self) -> None:
        """Hard reset — call when entering a new position or on session start."""
        self._reset()

    @property
    def state(self) -> str:
        return self._state

    # ── internal state ────────────────────────────────────────────────────────

    def _reset(self) -> None:
        self._state:          str          = 'SCANNING'
        self._direction:      str | None   = None
        self._pullback_count: int          = 0
        self._pb_high:        float | None = None
        self._pb_low:         float | None = None
        self._window:         _Window | None = None

    # ── crossover helpers (thin wrappers for readability) ────────────────────

    def _xabove(self, ind: dict, other: str) -> bool:
        return IndicatorEngine.cross_above(
            ind['ema_confirm'], ind[other],
            ind['prev_ema_confirm'], ind[f'prev_{other}'],
        )

    def _xbelow(self, ind: dict, other: str) -> bool:
        return IndicatorEngine.cross_below(
            ind['ema_confirm'], ind[other],
            ind['prev_ema_confirm'], ind[f'prev_{other}'],
        )

    # ── GLOBAL INVALIDATION ───────────────────────────────────────────────────

    def _check_global_invalidation(self, ind: dict) -> None:
        """
        Reset if an opposing EMA crossover is detected while ARMED.
        Mirrors the Backtrader strategy's global invalidation rule.
        """
        if self._state == 'ARMED_LONG':
            opposing = (
                self._xbelow(ind, 'ema_fast') or
                self._xbelow(ind, 'ema_medium') or
                self._xbelow(ind, 'ema_slow')
            )
            if opposing:
                log.debug("GLOBAL INVALIDATION: SHORT crossover while ARMED_LONG → SCANNING")
                self._reset()

        elif self._state == 'ARMED_SHORT':
            opposing = (
                self._xabove(ind, 'ema_fast') or
                self._xabove(ind, 'ema_medium') or
                self._xabove(ind, 'ema_slow')
            )
            if opposing:
                log.debug("GLOBAL INVALIDATION: LONG crossover while ARMED_SHORT → SCANNING")
                self._reset()

    # ── PHASE 1 — scan for EMA crossover signal ───────────────────────────────

    def _phase1(self, ind: dict, bar_index: int, is_vol_expanding: bool) -> None:
        if not is_vol_expanding:
            return None   # Volatility contracting — skip

        if self._enable_long:
            cross = (
                self._xabove(ind, 'ema_fast') or
                self._xabove(ind, 'ema_medium') or
                self._xabove(ind, 'ema_slow')
            )
            if cross:
                if self._trend_filter and not self._is_trending('LONG'):
                    log.debug("PHASE 1: LONG signal rejected — ema_slow not trending up")
                    return None
                log.info("PHASE 1: LONG signal → ARMED_LONG")
                self._state     = 'ARMED_LONG'
                self._direction = 'LONG'
                self._pullback_count = 0
                return None

        if self._enable_short:
            cross = (
                self._xbelow(ind, 'ema_fast') or
                self._xbelow(ind, 'ema_medium') or
                self._xbelow(ind, 'ema_slow')
            )
            if cross:
                if self._trend_filter and not self._is_trending('SHORT'):
                    log.debug("PHASE 1: SHORT signal rejected — ema_slow not trending down")
                    return None
                log.info("PHASE 1: SHORT signal → ARMED_SHORT")
                self._state     = 'ARMED_SHORT'
                self._direction = 'SHORT'
                self._pullback_count = 0
                return None

        return None

    # ── TREND FILTER ──────────────────────────────────────────────────────────

    def _is_trending(self, direction: str) -> bool:
        """
        Return True if ema_slow is trending in the signal direction.
        Requires at least (trend_filter_bars + 1) history entries.
        """
        n = self._trend_filter_bars
        if len(self._ema_slow_hist) < n + 1:
            return False   # not enough history yet
        oldest = self._ema_slow_hist[0]    # value n bars ago
        newest = self._ema_slow_hist[-1]   # current bar
        return newest > oldest if direction == 'LONG' else newest < oldest

    # ── PHASE 2 — count pullback candles ──────────────────────────────────────

    def _phase2(self, ind: dict, bar_index: int) -> None:
        direction  = self._direction
        max_candles = (
            self._long_pb_max if direction == 'LONG' else self._short_pb_max
        )

        # A pullback candle is bearish for LONG, bullish for SHORT
        is_pb = (
            ind['close'] < ind['open'] if direction == 'LONG'
            else ind['close'] > ind['open']
        )

        if is_pb:
            self._pullback_count += 1
            if self._pullback_count >= max_candles:
                # Enough pullback candles — record the last one's range
                self._pb_high = ind['high']
                self._pb_low  = ind['low']
                self._open_window(direction, bar_index)
                log.info(
                    f"PHASE 2: Pullback complete ({self._pullback_count} candles) → WINDOW_OPEN"
                )
        else:
            # Non-pullback candle — invalidate setup
            log.debug(f"PHASE 2: Non-pullback candle → SCANNING")
            self._reset()

        return None

    # ── PHASE 3 — open the two-sided breakout window ──────────────────────────

    def _open_window(self, direction: str, bar_index: int) -> None:
        candle_range = self._pb_high - self._pb_low
        offset       = candle_range * self._price_offset
        periods      = self._long_win if direction == 'LONG' else self._short_win

        self._window = _Window(
            top        = self._pb_high + offset,
            bottom     = self._pb_low  - offset,
            expiry_bar = bar_index + periods,
        )
        self._state = 'WINDOW_OPEN'
        log.info(
            f"WINDOW OPEN ({direction}): "
            f"top={self._window.top:.4f}  bottom={self._window.bottom:.4f}  "
            f"expires @ bar {self._window.expiry_bar}"
        )

    # ── PHASE 4 — monitor for breakout or failure ─────────────────────────────

    def _phase4(self, ind: dict, bar_index: int) -> str | None:
        w         = self._window
        direction = self._direction

        # Window timeout → drop back to ARMED, let it keep scanning for pullbacks
        if bar_index > w.expiry_bar:
            log.debug(f"WINDOW TIMEOUT ({direction}) → back to ARMED_{direction}")
            self._state          = f'ARMED_{direction}'
            self._pullback_count = 0
            self._window         = None
            return None

        if direction == 'LONG':
            if ind['high'] >= w.top:
                log.info(f"BREAKOUT LONG  high={ind['high']:.4f} >= top={w.top:.4f}")
                self._reset()
                return 'LONG'
            if ind['low'] <= w.bottom:
                # Failure — price broke downward: reset to ARMED to look for new pullback
                log.debug(
                    f"FAILURE LONG  low={ind['low']:.4f} <= bottom={w.bottom:.4f} "
                    f"→ ARMED_LONG"
                )
                self._state          = 'ARMED_LONG'
                self._pullback_count = 0
                self._window         = None

        elif direction == 'SHORT':
            if ind['low'] <= w.bottom:
                log.info(f"BREAKOUT SHORT  low={ind['low']:.4f} <= bottom={w.bottom:.4f}")
                self._reset()
                return 'SHORT'
            if ind['high'] >= w.top:
                log.debug(
                    f"FAILURE SHORT  high={ind['high']:.4f} >= top={w.top:.4f} "
                    f"→ ARMED_SHORT"
                )
                self._state          = 'ARMED_SHORT'
                self._pullback_count = 0
                self._window         = None

        return None
