"""
bot.py — XAUT/USDT Live Trading Bot (Bybit)
============================================
Entry point for the live bot.  Run directly:

    python src/live/bot.py

Or via Railway (see Procfile / railway.toml at repo root).

Architecture
------------
1. Startup
   - Load config from environment variables
   - Fetch last WARMUP_BARS historical 5-min candles via REST → warm up indicators
   - Reconcile with any existing open position on Bybit

2. Live loop (WebSocket kline stream, 5-min interval)
   - On every CLOSED candle:
       a. Update indicator engine
       b. Session end check → force-close open position if past SESSION_END_HOUR
       c. Session start check → skip new entries outside SESSION_START–END window
       d. Volatility regime check (ATR > EMA-of-ATR)
       e. State machine → 'LONG' / 'SHORT' / None
       f. If signal and not in position → size → place entry with SL + TP on Bybit

3. Position sizing
       size (XAUT) = (equity × RISK_PERCENT) / stop_distance_USD
   No contract multipliers, no lot floors.

4. SL / TP are set as position TP/SL on the Bybit order itself, so they
   are managed 24 × 7 by the exchange even if the bot restarts.
"""
from __future__ import annotations

import logging
import math
import os
import sys
import time
from datetime import datetime, timezone

# ── ensure local imports work regardless of working directory ────────────────
sys.path.insert(0, os.path.dirname(__file__))

from pybit.unified_trading import HTTP, WebSocket

import config as C
from indicators import IndicatorEngine
from order_manager import OrderManager
from state_machine import StateMachine

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, C.LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('bot')


class XAUTBot:
    """Live XAUT/USDT trading bot — one instance per process."""

    def __init__(self) -> None:
        log.info("=" * 60)
        log.info("  XAUT Live Bot  |  Bybit %s", 'TESTNET' if C.TESTNET else 'MAINNET')
        log.info("=" * 60)
        log.info("  Symbol     : %s", C.SYMBOL)
        log.info("  Session    : %02d:00 – %02d:00 UTC", C.SESSION_START_HOUR, C.SESSION_END_HOUR)
        log.info("  Risk/trade : %.1f%%", C.RISK_PERCENT * 100)
        log.info(
            "  Direction  : %s",
            'LONG+SHORT' if C.ENABLE_LONG and C.ENABLE_SHORT
            else 'LONG' if C.ENABLE_LONG else 'SHORT',
        )
        log.info(
            "  Vol Regime : %s  (ATR EMA-%d)",
            'ON' if C.USE_VOLATILITY_REGIME else 'OFF',
            C.ATR_REGIME_LOOKBACK,
        )
        log.info("=" * 60)

        # Order manager (Bybit REST)
        self._orders = OrderManager(
            api_key=C.BYBIT_API_KEY,
            api_secret=C.BYBIT_API_SECRET,
            testnet=C.TESTNET,
            symbol=C.SYMBOL,
        )

        # Indicator engine
        self._engine = IndicatorEngine(
            ema_fast=C.EMA_FAST,
            ema_medium=C.EMA_MEDIUM,
            ema_slow=C.EMA_SLOW,
            ema_confirm=C.EMA_CONFIRM,
            atr_period=C.ATR_PERIOD,
            atr_regime_lookback=C.ATR_REGIME_LOOKBACK,
        )

        # State machine
        self._sm = StateMachine(
            long_pullback_max=C.LONG_PULLBACK_MAX,
            short_pullback_max=C.SHORT_PULLBACK_MAX,
            long_window_periods=C.LONG_WINDOW_PERIODS,
            short_window_periods=C.SHORT_WINDOW_PERIODS,
            window_price_offset=C.WINDOW_PRICE_OFFSET,
            enable_long=C.ENABLE_LONG,
            enable_short=C.ENABLE_SHORT,
        )

        self._bar_index: int = 0   # monotonic counter for window expiry logic

        # Warmup historical data
        self._warmup()

    # ── startup warmup ────────────────────────────────────────────────────────

    def _warmup(self) -> None:
        """Fetch the last WARMUP_BARS 5-min candles to warm up indicators."""
        needed = max(C.WARMUP_BARS, 60)   # at least 60 bars
        log.info("Warming up indicators — fetching %d bars from REST API…", needed)

        http = HTTP(
            testnet=C.TESTNET,
            api_key=C.BYBIT_API_KEY,
            api_secret=C.BYBIT_API_SECRET,
        )

        # Bybit returns max 200 bars per request, newest-first
        limit  = min(needed, 200)
        resp   = http.get_kline(
            category='linear',
            symbol=C.SYMBOL,
            interval='5',
            limit=limit,
        )
        raw_bars = resp['result']['list']
        raw_bars.reverse()   # chronological order

        for rb in raw_bars:
            # Bybit kline format: [startTime, open, high, low, close, volume, turnover]
            bar = {
                'timestamp': int(rb[0]),
                'open':      float(rb[1]),
                'high':      float(rb[2]),
                'low':       float(rb[3]),
                'close':     float(rb[4]),
                'volume':    float(rb[5]),
            }
            self._engine.add_bar(bar)
            self._bar_index += 1

        log.info(
            "Warmup done: %d bars loaded, indicators warm: %s",
            self._engine.bar_count,
            self._engine.is_warm,
        )

        # Reconcile any existing position
        pos = self._orders.get_position()
        if pos:
            log.warning(
                "Open position detected on startup: %s %.4f XAUT @ %.4f | "
                "SL=%.4f  TP=%.4f  uPnL=%+.2f",
                pos['side'], pos['size'], pos['entry_price'],
                pos['stop_loss'], pos['take_profit'], pos['unrealised_pnl'],
            )
            log.warning(
                "SL/TP are already set on the exchange — bot will monitor session end only."
            )
        else:
            log.info("No open position — ready to scan for signals.")

    # ── session helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _bar_dt(ts_ms: int) -> datetime:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    def _in_session(self, ts_ms: int) -> bool:
        if not C.USE_SESSION_FILTER:
            return True
        dt  = self._bar_dt(ts_ms)
        now = dt.hour * 60 + dt.minute
        return C.SESSION_START_HOUR * 60 <= now < C.SESSION_END_HOUR * 60

    def _past_session_end(self, ts_ms: int) -> bool:
        if not C.USE_SESSION_FILTER:
            return False
        dt  = self._bar_dt(ts_ms)
        now = dt.hour * 60 + dt.minute
        return now >= C.SESSION_END_HOUR * 60

    # ── position sizing ───────────────────────────────────────────────────────

    def _calc_size(
        self,
        direction:   str,
        entry_price: float,
        stop_price:  float,
    ) -> float:
        """
        Pure crypto risk-based sizing:
            size (XAUT) = (equity × RISK_PERCENT) / stop_distance_USD
        """
        equity        = self._orders.get_equity()
        risk_amount   = equity * C.RISK_PERCENT
        stop_distance = abs(entry_price - stop_price)

        if stop_distance <= 0:
            log.error("Stop distance is zero — cannot size position.")
            return 0.0

        size = risk_amount / stop_distance
        log.info(
            "Sizing  equity=%.2f  risk=%.2f USDT  stop_dist=%.4f  → %.4f XAUT",
            equity, risk_amount, stop_distance, size,
        )
        return size

    # ── entry execution ───────────────────────────────────────────────────────

    def _execute_entry(self, direction: str, ind: dict) -> None:
        if not self._orders.is_flat():
            log.info("Entry signal ignored — already in position.")
            return

        entry_price = ind['close']   # market order fills near current close
        atr         = ind['atr']
        bar_high    = ind['high']
        bar_low     = ind['low']

        if direction == 'LONG':
            stop_loss   = bar_low  - atr * C.LONG_ATR_SL_MULT
            take_profit = bar_high + atr * C.LONG_ATR_TP_MULT
        else:
            stop_loss   = bar_high + atr * C.SHORT_ATR_SL_MULT
            take_profit = bar_low  - atr * C.SHORT_ATR_TP_MULT

        size = self._calc_size(direction, entry_price, stop_loss)
        if size <= 0:
            log.warning("Size is 0 — entry skipped.")
            return

        log.info(
            "SIGNAL %s  entry≈%.4f  SL=%.4f  TP=%.4f  size=%.4f XAUT",
            direction, entry_price, stop_loss, take_profit, size,
        )

        try:
            self._orders.place_entry(direction, size, stop_loss, take_profit)
            # Reset state machine after entry so it starts fresh for the next trade
            self._sm.reset()
        except Exception as exc:
            log.error("Order placement failed: %s", exc)

    # ── session end close ─────────────────────────────────────────────────────

    def _session_end_close(self, dt_str: str) -> None:
        """Force-close position and reset state machine at session end."""
        log.info("SESSION END [%s] — cancelling orders and closing position.", dt_str)
        try:
            self._orders.cancel_all_orders()
            self._orders.close_position(reason='SESSION_END')
        except Exception as exc:
            log.error("Session-end close failed: %s", exc)
        self._sm.reset()

    # ── WebSocket callback ────────────────────────────────────────────────────

    def _on_kline(self, msg: dict) -> None:
        """
        Called by pybit WebSocket for every kline update.
        We only process CLOSED (confirmed) candles.
        """
        try:
            data = msg.get('data', [])
            if not data:
                return

            candle = data[0]
            if not candle.get('confirm', False):
                return   # candle still forming — skip

            ts_ms = int(candle['start'])
            bar   = {
                'timestamp': ts_ms,
                'open':      float(candle['open']),
                'high':      float(candle['high']),
                'low':       float(candle['low']),
                'close':     float(candle['close']),
                'volume':    float(candle['volume']),
            }

            self._bar_index += 1
            self._engine.add_bar(bar)

            dt_str = self._bar_dt(ts_ms).strftime('%Y-%m-%d %H:%M UTC')
            log.debug(
                "Bar [%s] O=%.4f H=%.4f L=%.4f C=%.4f  Vol=%.2f",
                dt_str, bar['open'], bar['high'], bar['low'],
                bar['close'], bar['volume'],
            )

            if not self._engine.is_warm:
                log.debug("Indicators not warm yet — skipping bar.")
                return

            ind = self._engine.compute()
            if ind is None:
                return

            # ── 1. SESSION END: close any open position ───────────────────────
            if self._past_session_end(ts_ms):
                if not self._orders.is_flat():
                    self._session_end_close(dt_str)
                return

            # ── 2. SESSION START: only scan inside trading hours ──────────────
            if not self._in_session(ts_ms):
                log.debug("Outside session [%s] — no new entries.", dt_str)
                return

            # ── 3. Already in a position: exchange manages SL/TP ─────────────
            if not self._orders.is_flat():
                log.debug("In position — holding.")
                return

            # ── 4. Volatility regime pre-filter ──────────────────────────────
            is_vol_expanding = (
                ind['atr'] > ind['atr_regime'] if C.USE_VOLATILITY_REGIME else True
            )
            if not is_vol_expanding:
                log.debug(
                    "Volatility contracting (ATR %.4f < regime %.4f) — skip.",
                    ind['atr'], ind['atr_regime'],
                )

            # ── 5. State machine ──────────────────────────────────────────────
            signal = self._sm.process_bar(ind, self._bar_index, is_vol_expanding)
            log.debug("State machine: state=%s  signal=%s", self._sm.state, signal)

            if signal:
                self._execute_entry(signal, ind)

        except Exception as exc:
            log.exception("Error processing kline: %s", exc)

    # ── main run loop ─────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("Starting WebSocket kline stream (5-min, %s)…", C.SYMBOL)

        ws = WebSocket(
            testnet=C.TESTNET,
            channel_type='linear',
        )
        ws.kline_stream(
            callback=self._on_kline,
            symbol=C.SYMBOL,
            interval=5,
        )

        log.info("Bot is live.  Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down…")
            ws.exit()
            log.info("Bot stopped.")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    bot = XAUTBot()
    bot.run()
