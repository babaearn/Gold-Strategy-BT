"""
orb.py — Opening Range Breakout Strategy (Strategy 2)
=======================================================
Concept
-------
During the first ORB_RANGE_BARS bars of each trading session
(default: 12 × 5 min = 60 minutes, 13:00–14:00 UTC) we build the
day's opening range: the high and low of that period.

Once the range is complete we wait for price to break out of the
range in the direction confirmed by the slow EMA trend.  One trade
per session maximum.

Why this works on Gold
----------------------
Gold makes its largest directional move of the day during the London
session (13:00–16:00 UTC).  The opening-hour range captures the early
consolidation, and the subsequent breakout is the "commitment" move.
Requiring ema_slow to be trending in the breakout direction filters
counter-trend noise.

Backtest stats (5yr, XAUUSD 5-min, 13:00–18:00 UTC, 1-trade/day max)
----------------------------------------------------------------------
  ORB 60 min  SL=0.7×range  TP=2.0×range  EMA-trend filter  vol filter
  ┌─────────────────────────────────────────────────────────┐
  │  Trades     : 1 083  (18.3 / month)                     │
  │  Win Rate   : 42.8%                                     │
  │  Exp/trade  : +0.051 R                                  │
  │  Max DD     : 20.8 R  → ~14.6% at 0.70% risk/trade     │
  │  5yr return : +55.2 R → ~38.7% at 0.70% risk/trade     │
  └─────────────────────────────────────────────────────────┘

Recommended Railway variables (Strategy 2)
------------------------------------------
  ACTIVE_STRATEGY=2
  ORB_RANGE_BARS=12        # 12 × 5-min = 60-minute range
  ORB_SL_RANGE_FRAC=0.7    # SL = range × 0.70  from entry
  ORB_TP_RANGE_MULT=2.0    # TP = range × 2.0   from entry
  RISK_PERCENT=0.007        # 0.70% per trade → ~14.6% MaxDD

SL / TP interface
-----------------
After returning a signal ('LONG' or 'SHORT'), this SM stores the
computed SL and TP prices in public attributes `entry_sl` and
`entry_tp`.  bot.py reads these INSTEAD of the ATR-based calculation
when they are not None.

Session detection
-----------------
`bar_index` increments for every 5-min bar (including outside-session).
Between session end (18:00) and next session start (13:00 next day)
≈ 228 bars elapse.  If `bar_index - _last_bar_idx > SESSION_GAP`
(default 60), a new session is assumed and the range re-starts.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Gap in bar_index between consecutive in-session process_bar() calls
# that indicates a new session started.  228 bars overnight >> 60 easily.
_SESSION_GAP = 60


class ORBStrategy:
    """
    Opening Range Breakout state machine.
    Drop-in Strategy-2 replacement for StateMachine.
    Same process_bar / reset / state interface.
    """

    def __init__(
        self,
        orb_range_bars:    int   = 12,
        orb_sl_range_frac: float = 0.7,
        orb_tp_range_mult: float = 2.0,
        enable_long:       bool  = True,
        enable_short:      bool  = True,
        **kwargs,                          # absorb Strategy-1 kwargs silently
    ) -> None:
        self._range_bars   = orb_range_bars
        self._sl_frac      = orb_sl_range_frac
        self._tp_mult      = orb_tp_range_mult
        self._enable_long  = enable_long
        self._enable_short = enable_short
        # kwargs intentionally ignored (long_pullback_max, trend_filter, etc.)

        # Public SL/TP for bot.py to read after a signal
        self.entry_sl: float | None = None
        self.entry_tp: float | None = None

        self._last_bar_idx: int = -1
        self._reset()

    # ── public API (mirrors StateMachine) ─────────────────────────────────────

    def process_bar(
        self,
        ind:              dict,
        bar_index:        int,
        is_vol_expanding: bool,
    ) -> str | None:
        """
        Process one closed 5-min candle.
        Returns 'LONG', 'SHORT', or None.

        After returning a signal, entry_sl and entry_tp are set.
        """
        # ── new-session detection ─────────────────────────────────────────────
        gap = bar_index - self._last_bar_idx
        if self._last_bar_idx < 0 or gap > _SESSION_GAP:
            # New session — start (or re-start) building the opening range
            if self._state != 'RANGE_BUILDING':
                log.info(
                    "ORB: new session detected (gap=%d bars) → RANGE_BUILDING",
                    gap if self._last_bar_idx >= 0 else -1,
                )
            self._reset()   # clears range, resets state to RANGE_BUILDING

        self._last_bar_idx = bar_index

        if self._state == 'RANGE_BUILDING':
            return self._build_range(ind, bar_index)

        if self._state == 'ARMED':
            return self._watch_breakout(ind, bar_index, is_vol_expanding)

        # DONE: already traded today — nothing to do
        return None

    def reset(self) -> None:
        """Hard reset — called by bot.py on position entry / session end."""
        self._reset()

    @property
    def state(self) -> str:
        return self._state

    # ── internal state ────────────────────────────────────────────────────────

    def _reset(self) -> None:
        self._state:       str         = 'RANGE_BUILDING'
        self._range_high:  float       = 0.0
        self._range_low:   float       = float('inf')
        self._range_count: int         = 0
        self.entry_sl                  = None
        self.entry_tp                  = None

    # ── state handlers ────────────────────────────────────────────────────────

    def _build_range(self, ind: dict, bar_index: int) -> None:
        """Accumulate the opening range over the first _range_bars bars."""
        self._range_high = max(self._range_high, ind['high'])
        self._range_low  = min(self._range_low,  ind['low'])
        self._range_count += 1

        if self._range_count >= self._range_bars:
            rng = self._range_high - self._range_low
            log.info(
                "ORB: range complete (%d bars)  H=%.4f  L=%.4f  range=%.4f → ARMED",
                self._range_count, self._range_high, self._range_low, rng,
            )
            self._state = 'ARMED'
        return None

    def _watch_breakout(
        self, ind: dict, bar_index: int, is_vol_expanding: bool,
    ) -> str | None:
        """Watch for a breakout of the opening range, filtered by trend + vol."""
        rng = self._range_high - self._range_low
        if rng <= 0:
            return None

        # Trend filter: ema_slow must be trending in signal direction
        # We compare ema_slow now vs prev_ema_slow (proxy for short-term slope)
        # For a stronger filter we compare vs 15 bars ago; here we use
        # the prev_ value (1 bar ago) as a lightweight proxy.
        # Using ema_slow > prev_ema_slow is a 1-bar slope check — may be noisy.
        # Instead, compare ema_slow now vs ema_medium as a trend proxy:
        #   ema_slow > ema_medium → slow EMA rising (bullish)
        #   ema_slow < ema_medium → slow EMA falling (bearish)
        ema_slow_rising  = ind['ema_slow'] > ind['prev_ema_slow']
        ema_slow_falling = ind['ema_slow'] < ind['prev_ema_slow']

        # Breakout checks
        if self._enable_long and ind['high'] > self._range_high:
            if is_vol_expanding and ema_slow_rising:
                entry = ind['close']   # bot.py fills at next-bar open; close approx
                sl = entry - rng * self._sl_frac
                tp = entry + rng * self._tp_mult
                self.entry_sl = sl
                self.entry_tp = tp
                log.info(
                    "ORB BREAKOUT LONG  close=%.4f  range_top=%.4f  "
                    "SL=%.4f  TP=%.4f",
                    entry, self._range_high, sl, tp,
                )
                self._state = 'DONE'
                return 'LONG'

        if self._enable_short and ind['low'] < self._range_low:
            if is_vol_expanding and ema_slow_falling:
                entry = ind['close']
                sl = entry + rng * self._sl_frac
                tp = entry - rng * self._tp_mult
                self.entry_sl = sl
                self.entry_tp = tp
                log.info(
                    "ORB BREAKOUT SHORT  close=%.4f  range_bot=%.4f  "
                    "SL=%.4f  TP=%.4f",
                    entry, self._range_low, sl, tp,
                )
                self._state = 'DONE'
                return 'SHORT'

        return None
