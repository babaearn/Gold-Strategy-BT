"""
indicators.py — Rolling indicator engine
=========================================
Maintains a sliding window of closed OHLCV bars and computes the same
indicators used in the SunriseOgleXAUT Backtrader strategy, using pandas
so that calculations match exactly:

  Backtrader           pandas equivalent
  ─────────────────────────────────────────────────────────────────────
  bt.ind.EMA(c, p)  →  series.ewm(span=p, adjust=False).mean()
                        (alpha = 2 / (p + 1))
  bt.ind.ATR(d, p)  →  Wilder EMA of True Range
                        series.ewm(alpha=1/p, adjust=False).mean()
  EMA of ATR        →  atr_series.ewm(span=p, adjust=False).mean()

Cross-over logic mirrors Pine Script ta.crossover / ta.crossunder:
  cross_above(a, b) → a[0] > b[0]  AND  a[-1] <= b[-1]
  cross_below(a, b) → a[0] < b[0]  AND  a[-1] >= b[-1]
"""
from __future__ import annotations

import pandas as pd


class IndicatorEngine:
    """
    Keeps the last MAX_BARS closed candles and recomputes all indicators
    on each new bar.  Call add_bar() then compute() on every closed 5-min candle.
    """

    MAX_BARS  = 300   # Keep at most 300 bars in memory
    # Strategy needs at least this many bars before indicators are reliable.
    # Using 3× the longest period (EMA_SLOW=26, ATR_REGIME=20, ATR=14).
    MIN_WARM  = 150

    def __init__(
        self,
        ema_fast:            int = 12,
        ema_medium:          int = 18,
        ema_slow:            int = 26,
        ema_confirm:         int = 1,
        atr_period:          int = 14,
        atr_regime_lookback: int = 20,
    ) -> None:
        self._p_fast    = ema_fast
        self._p_medium  = ema_medium
        self._p_slow    = ema_slow
        self._p_confirm = ema_confirm
        self._p_atr     = atr_period
        self._p_regime  = atr_regime_lookback

        self._bars: list[dict] = []

    # ── public interface ──────────────────────────────────────────────────────

    def add_bar(self, bar: dict) -> None:
        """
        Append a closed OHLCV bar.
        Required keys: open, high, low, close, volume, timestamp
        """
        self._bars.append(bar)
        if len(self._bars) > self.MAX_BARS:
            self._bars.pop(0)

    @property
    def bar_count(self) -> int:
        return len(self._bars)

    @property
    def is_warm(self) -> bool:
        return self.bar_count >= self.MIN_WARM

    def compute(self) -> dict | None:
        """
        Compute all indicator values for the **most recent** closed bar.

        Returns a flat dict with current-bar values and one-bar-ago values
        (needed for cross-over detection), or None if not enough bars.

        Returned keys
        -------------
        Current bar  : ema_fast, ema_medium, ema_slow, ema_confirm,
                       atr, atr_regime, open, high, low, close
        Previous bar : prev_ema_fast, prev_ema_medium, prev_ema_slow,
                       prev_ema_confirm, prev_open, prev_close
        """
        if not self.is_warm:
            return None

        df = pd.DataFrame(self._bars)

        close  = df['close'].astype(float)
        high   = df['high'].astype(float)
        low    = df['low'].astype(float)
        open_  = df['open'].astype(float)

        ema_fast    = self._ema(close, self._p_fast)
        ema_medium  = self._ema(close, self._p_medium)
        ema_slow    = self._ema(close, self._p_slow)
        ema_confirm = self._ema(close, self._p_confirm)
        atr         = self._atr(high, low, close, self._p_atr)
        atr_regime  = self._ema(atr, self._p_regime)

        return {
            # ── current bar [0] ───────────────────────────────────────────────
            'open':        float(open_.iloc[-1]),
            'high':        float(high.iloc[-1]),
            'low':         float(low.iloc[-1]),
            'close':       float(close.iloc[-1]),
            'ema_fast':    float(ema_fast.iloc[-1]),
            'ema_medium':  float(ema_medium.iloc[-1]),
            'ema_slow':    float(ema_slow.iloc[-1]),
            'ema_confirm': float(ema_confirm.iloc[-1]),
            'atr':         float(atr.iloc[-1]),
            'atr_regime':  float(atr_regime.iloc[-1]),

            # ── previous bar [-1] — needed for crossover detection ─────────
            'prev_open':        float(open_.iloc[-2]),
            'prev_close':       float(close.iloc[-2]),
            'prev_ema_fast':    float(ema_fast.iloc[-2]),
            'prev_ema_medium':  float(ema_medium.iloc[-2]),
            'prev_ema_slow':    float(ema_slow.iloc[-2]),
            'prev_ema_confirm': float(ema_confirm.iloc[-2]),
        }

    # ── crossover helpers ─────────────────────────────────────────────────────

    @staticmethod
    def cross_above(
        curr_a: float, curr_b: float,
        prev_a: float, prev_b: float,
    ) -> bool:
        """True if `a` crossed above `b` on the current bar (Pine ta.crossover)."""
        return curr_a > curr_b and prev_a <= prev_b

    @staticmethod
    def cross_below(
        curr_a: float, curr_b: float,
        prev_a: float, prev_b: float,
    ) -> bool:
        """True if `a` crossed below `b` on the current bar (Pine ta.crossunder)."""
        return curr_a < curr_b and prev_a >= prev_b

    # ── private calculations ──────────────────────────────────────────────────

    @staticmethod
    def _ema(series: pd.Series, period: int) -> pd.Series:
        """Standard EMA — alpha = 2/(period+1), matching bt.ind.EMA."""
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _atr(
        high:  pd.Series,
        low:   pd.Series,
        close: pd.Series,
        period: int,
    ) -> pd.Series:
        """
        ATR using Wilder's smoothing (alpha = 1/period), matching bt.ind.ATR.
        True Range = max(H-L, |H-C[-1]|, |L-C[-1]|)
        """
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1.0 / period, adjust=False).mean()
