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
   - Create BotState (shared with Telegram) + TradeLog
   - Start TelegramNotifier in background thread (if TELEGRAM_TOKEN set)
   - Fetch last WARMUP_BARS historical 5-min candles via REST → warm up indicators
   - Reconcile with any existing open position on Bybit

2. Live loop (WebSocket kline stream, 5-min interval)
   - On every CLOSED candle:
       a. Update indicator engine
       b. Detect if position was closed by exchange (SL/TP) → notify + log
       c. Handle /close command from Telegram
       d. Session end check → force-close if past SESSION_END_HOUR
       e. Session start check → skip new entries outside window
       f. Skip if bot is PAUSED via Telegram /pause
       g. Volatility regime check (ATR > EMA-of-ATR)
       h. State machine → 'LONG' / 'SHORT' / None
       i. If signal and not in position → size → place entry

3. Position sizing
       size (XAUT) = (equity × risk_percent) / stop_distance_USD
   No contract multipliers, no lot floors.

4. SL / TP are set as position TP/SL on the Bybit order itself, so they
   are managed 24×7 by the exchange even if the bot restarts.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

# ── ensure local imports work regardless of working directory ─────────────────
sys.path.insert(0, os.path.dirname(__file__))

from pybit.unified_trading import HTTP, WebSocket

import config as C
from indicators import IndicatorEngine
from order_manager import OrderManager
from strategies import get_strategy, strategy_name
from trade_log import TradeLog
from telegram_bot import BotState, TelegramNotifier

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, C.LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('bot')

# Silence HTTP-client libraries that embed the Telegram bot token in request URLs.
# Without this, DEBUG logging exposes the token in lines like:
#   DEBUG httpx - HTTP Request: POST https://api.telegram.org/bot<TOKEN>/getUpdates
for _lib in ("httpx", "httpcore", "telegram", "apscheduler"):
    logging.getLogger(_lib).setLevel(logging.WARNING)


def _assert_testnet_or_warn() -> None:
    """
    Safety guard: refuse to start on MAINNET unless BYBIT_TESTNET is explicitly
    set to 'false' in the environment.  This prevents accidental live trading
    caused by a missing or mis-spelled environment variable.
    """
    if C.TESTNET:
        return  # safe — paper trading, no funds at risk

    raw = os.getenv('BYBIT_TESTNET', '').strip().lower()
    if raw != 'false':
        # The flag was not explicitly set to false — refuse to start.
        log.critical(
            "MAINNET requested but BYBIT_TESTNET env var is not explicitly 'false'. "
            "Set BYBIT_TESTNET=false to confirm you intend to trade with real funds."
        )
        sys.exit(1)

    log.warning("=" * 60)
    log.warning("  *** MAINNET MODE — REAL FUNDS AT RISK ***")
    log.warning("  Set BYBIT_TESTNET=true to use paper trading instead.")
    log.warning("=" * 60)


class XAUTBot:
    """Live XAUT/USDT trading bot — one instance per process."""

    def __init__(self) -> None:
        _assert_testnet_or_warn()   # ← safety gate before any exchange calls

        log.info("=" * 60)
        _mode = ('TESTNET' if C.TESTNET else 'MAINNET') + (' [PAPER]' if C.PAPER_MODE else '')
        log.info("  XAUT Live Bot  |  Bybit %s", _mode)
        log.info("=" * 60)
        log.info("  Symbol     : %s", C.SYMBOL)
        log.info("  Strategy   : #%d — %s", C.ACTIVE_STRATEGY, strategy_name(C.ACTIVE_STRATEGY))
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
        log.info("  Telegram   : %s", 'ON' if C.TELEGRAM_ENABLED else 'OFF')
        log.info("=" * 60)

        # ── trade log (persistent JSON) ───────────────────────────────────────
        self._trade_log = TradeLog()

        # ── shared state bridge ───────────────────────────────────────────────
        self._state = BotState(risk_percent=C.RISK_PERCENT, active_strategy=C.ACTIVE_STRATEGY)

        # ── Telegram notifier (optional) ──────────────────────────────────────
        if C.TELEGRAM_ENABLED:
            self._tg = TelegramNotifier(
                token=C.TELEGRAM_TOKEN,
                chat_id=C.TELEGRAM_CHAT_ID,
                state=self._state,
                trade_log=self._trade_log,
                testnet=C.TESTNET,
                paper_mode=C.PAPER_MODE,
            )
        else:
            self._tg = None

        # ── order manager (Bybit REST) ────────────────────────────────────────
        self._orders = OrderManager(
            api_key=C.BYBIT_API_KEY,
            api_secret=C.BYBIT_API_SECRET,
            testnet=C.TESTNET,
            symbol=C.SYMBOL,
        )

        # ── indicator engine ──────────────────────────────────────────────────
        self._engine = IndicatorEngine(
            ema_fast=C.EMA_FAST,
            ema_medium=C.EMA_MEDIUM,
            ema_slow=C.EMA_SLOW,
            ema_confirm=C.EMA_CONFIRM,
            atr_period=C.ATR_PERIOD,
            atr_regime_lookback=C.ATR_REGIME_LOOKBACK,
        )

        # ── state machine (strategy selected by ACTIVE_STRATEGY) ─────────────
        self._sm = get_strategy(
            C.ACTIVE_STRATEGY,
            long_pullback_max=C.LONG_PULLBACK_MAX,
            short_pullback_max=C.SHORT_PULLBACK_MAX,
            long_window_periods=C.LONG_WINDOW_PERIODS,
            short_window_periods=C.SHORT_WINDOW_PERIODS,
            window_price_offset=C.WINDOW_PRICE_OFFSET,
            enable_long=C.ENABLE_LONG,
            enable_short=C.ENABLE_SHORT,
            trend_filter=C.TREND_FILTER,
            trend_filter_bars=C.TREND_FILTER_BARS,
        )

        self._bar_index:    int  = 0   # monotonic counter for window expiry
        self._had_position: bool = False  # for exit detection

        # Warmup historical data
        self._warmup()

    # ── startup warmup ────────────────────────────────────────────────────────

    def _fetch_klines(self, limit: int):
        """
        Fetch historical klines for indicator warmup.

        Bybit Testnet blocks US IPs on public endpoints (Railway runs in the US).
        Strategy: try testnet first, fall back to mainnet public API if blocked.
        Kline data is identical between testnet and mainnet for XAUTUSDT.
        Returns the raw list, or None if both attempts fail.
        """
        # If running on testnet, try testnet then mainnet; mainnet → mainnet only
        endpoints = [True, False] if C.TESTNET else [False]
        for testnet_flag in endpoints:
            try:
                # Kline is a public endpoint — no auth headers needed
                http = HTTP(testnet=testnet_flag)
                resp = http.get_kline(
                    category='linear', symbol=C.SYMBOL, interval='5', limit=limit
                )
                if testnet_flag != C.TESTNET:
                    log.info("Warmup: using mainnet market data (testnet blocked from this IP).")
                return resp['result']['list']
            except Exception as exc:
                log.warning(
                    "Warmup kline fetch failed (testnet=%s): %s — %s",
                    testnet_flag, type(exc).__name__, exc,
                )
        return None

    def _reconcile_position(self) -> None:
        """Sync bot state with any existing open position."""
        if C.PAPER_MODE:
            # In paper mode there is no real position; restore any open paper trade from log
            open_trade = self._trade_log.get_open()
            if open_trade:
                self._had_position = True
                self._state.position = {
                    "direction": open_trade['direction'],
                    "size":      open_trade['size'],
                    "entry":     open_trade['entry_price'],
                    "sl":        open_trade['sl'],
                    "tp":        open_trade['tp'],
                }
                log.warning(
                    "Restored open paper trade from log: %s %.4f XAUT @ %.4f | SL=%.4f TP=%.4f",
                    open_trade['direction'], open_trade['size'], open_trade['entry_price'],
                    open_trade['sl'], open_trade['tp'],
                )
            else:
                log.info("No open paper trade — ready to scan.")
            return

        try:
            pos = self._orders.get_position()
        except Exception as exc:
            log.warning("Could not check existing position at startup: %s", exc)
            return

        if pos:
            self._had_position = True
            self._state.position = {
                "direction": pos['side'],
                "size":      pos['size'],
                "entry":     pos['entry_price'],
                "sl":        pos['stop_loss'],
                "tp":        pos['take_profit'],
            }
            log.warning(
                "Open position on startup: %s %.4f XAUT @ %.4f | SL=%.4f TP=%.4f uPnL=%+.2f",
                pos['side'], pos['size'], pos['entry_price'],
                pos['stop_loss'], pos['take_profit'], pos['unrealised_pnl'],
            )
            if self._trade_log.get_open() is None:
                self._trade_log.open_trade(
                    direction=pos['side'],
                    entry_price=pos['entry_price'],
                    size=pos['size'],
                    sl=pos['stop_loss'],
                    tp=pos['take_profit'],
                )
        else:
            log.info("No open position — ready to scan.")

    def _warmup(self) -> None:
        needed = max(C.WARMUP_BARS, 60)
        limit  = min(needed, 200)
        log.info("Warming up indicators — fetching %d bars from REST API…", needed)

        raw_bars = self._fetch_klines(limit)

        if raw_bars is None:
            log.warning(
                "Could not fetch warmup data — indicators will warm from live WebSocket feed. "
                "No trades will be taken for the first ~%d bars (~%.0f min).",
                needed, needed * 5.0,
            )
            self._reconcile_position()
            return

        raw_bars.reverse()   # oldest → newest
        for rb in raw_bars:
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

        log.info("Warmup done: %d bars, indicators warm: %s", self._engine.bar_count, self._engine.is_warm)
        self._reconcile_position()

    # ── helpers ───────────────────────────────────────────────────────────────

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

    def _notify(self, text: str) -> None:
        """Send to Telegram if enabled, always log."""
        log.info("[TG] %s", text.replace('\n', ' | '))
        if self._tg:
            self._tg.send(text)

    # ── position sizing ───────────────────────────────────────────────────────

    def _calc_size(self, entry_price: float, stop_price: float) -> float:
        """size (XAUT) = (equity × risk_percent) / stop_distance"""
        equity        = self._orders.get_equity()
        risk_amount   = equity * self._state.risk_percent   # live risk from BotState
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            log.error("Stop distance is zero — cannot size position.")
            return 0.0
        size = risk_amount / stop_distance
        log.info(
            "Sizing  equity=%.2f  risk=%.2f  stop_dist=%.4f  → %.4f XAUT",
            equity, risk_amount, stop_distance, size,
        )
        return size

    # ── entry execution ───────────────────────────────────────────────────────

    def _execute_entry(self, direction: str, ind: dict) -> None:
        if not self._orders.is_flat():
            log.info("Entry signal ignored — already in position.")
            return

        entry_price = ind['close']
        atr         = ind['atr']

        if direction == 'LONG':
            stop_loss   = ind['low']  - atr * C.LONG_ATR_SL_MULT
            take_profit = ind['high'] + atr * C.LONG_ATR_TP_MULT
        else:
            stop_loss   = ind['high'] + atr * C.SHORT_ATR_SL_MULT
            take_profit = ind['low']  - atr * C.SHORT_ATR_TP_MULT

        size = self._calc_size(entry_price, stop_loss)
        if size <= 0:
            log.warning("Size is 0 — entry skipped.")
            return

        log.info(
            "%s %s  entry≈%.4f  SL=%.4f  TP=%.4f  size=%.4f XAUT",
            "PAPER" if C.PAPER_MODE else "SIGNAL",
            direction, entry_price, stop_loss, take_profit, size,
        )

        if C.PAPER_MODE:
            # Virtual position — no real order, track candle-by-candle
            self._trade_log.open_trade(direction, entry_price, size, stop_loss, take_profit)
            self._state.position = {
                "direction": direction,
                "size":      size,
                "entry":     entry_price,
                "sl":        stop_loss,
                "tp":        take_profit,
            }
            self._had_position = True
            self._sm.reset()
            emoji = "📊🟢" if direction == "LONG" else "📊🔴"
            self._notify(
                f"{emoji} <b>PAPER {direction}</b>\n"
                f"Size  : {size:.4f} XAUT\n"
                f"Entry : ${entry_price:,.2f}\n"
                f"SL    : ${stop_loss:,.2f}\n"
                f"TP    : ${take_profit:,.2f}\n"
                f"Risk  : {self._state.risk_percent*100:.2f}%"
            )
            return

        try:
            self._orders.place_entry(direction, size, stop_loss, take_profit)
            self._sm.reset()

            # Record in trade log and shared state
            self._trade_log.open_trade(direction, entry_price, size, stop_loss, take_profit)
            self._state.position = {
                "direction": direction,
                "size":      size,
                "entry":     entry_price,
                "sl":        stop_loss,
                "tp":        take_profit,
            }
            self._had_position = True

            emoji = "🟢" if direction == "LONG" else "🔴"
            self._notify(
                f"{emoji} <b>ENTRY {direction}</b>\n"
                f"Size  : {size:.4f} XAUT\n"
                f"Entry : ${entry_price:,.2f}\n"
                f"SL    : ${stop_loss:,.2f}\n"
                f"TP    : ${take_profit:,.2f}\n"
                f"Risk  : {self._state.risk_percent*100:.2f}%"
            )
        except Exception as exc:
            log.error("Order placement failed: %s", exc)
            self._notify(f"⚠️ Order placement failed: {exc}")

    # ── exit detection (SL/TP hit by exchange) ────────────────────────────────

    def _check_position_closed(self, current_price: float) -> None:
        """Detect when exchange closed our position (SL or TP hit)."""
        if not self._had_position:
            return
        if not self._orders.is_flat():
            return

        # Position was open, now it's flat — SL or TP was triggered
        open_trade = self._trade_log.get_open()
        closed     = self._trade_log.close_trade(current_price, reason="SL/TP")
        self._had_position   = False
        self._state.position = None
        self._sm.reset()

        if closed:
            sign = self._sign_char(closed["pnl_usd"])
            self._notify(
                f"{'🟩' if closed['pnl_usd'] >= 0 else '🟥'} <b>TRADE CLOSED (SL/TP)</b>\n"
                f"Direction : {closed['direction']}\n"
                f"Exit ~    : ${current_price:,.2f}\n"
                f"PnL       : {sign}${closed['pnl_usd']:,.2f}"
            )

    @staticmethod
    def _sign_char(val: float) -> str:
        return "+" if val >= 0 else ""

    # ── paper-mode exit detection (candle-based SL/TP) ────────────────────────

    def _check_paper_position_closed(self, bar: dict) -> None:
        """Paper mode: detect if virtual SL or TP was touched by this candle."""
        pos = self._state.position
        if not pos:
            return
        direction = pos['direction']
        sl, tp    = pos['sl'], pos['tp']
        hit = exit_price = None
        if direction == 'LONG':
            if bar['low'] <= sl:
                hit, exit_price = 'SL', sl
            elif bar['high'] >= tp:
                hit, exit_price = 'TP', tp
        else:
            if bar['high'] >= sl:
                hit, exit_price = 'SL', sl
            elif bar['low'] <= tp:
                hit, exit_price = 'TP', tp
        if hit is None:
            return
        closed = self._trade_log.close_trade(exit_price, reason=f"PAPER_{hit}")
        self._had_position   = False
        self._state.position = None
        self._sm.reset()
        if closed:
            sign  = self._sign_char(closed['pnl_usd'])
            emoji = '🟩' if closed['pnl_usd'] >= 0 else '🟥'
            self._notify(
                f"{emoji} <b>PAPER CLOSED ({hit})</b>\n"
                f"Direction : {closed['direction']}\n"
                f"Exit      : ${exit_price:,.2f}\n"
                f"PnL       : {sign}${closed['pnl_usd']:,.2f}"
            )

    def _session_end_paper_close(self, dt_str: str, current_price: float) -> None:
        """Paper mode: close virtual position at session end (no exchange call)."""
        log.info("PAPER SESSION END [%s] — closing virtual position.", dt_str)
        closed = self._trade_log.close_trade(current_price, reason="SESSION_END")
        self._had_position   = False
        self._state.position = None
        self._sm.reset()
        if closed:
            sign = self._sign_char(closed['pnl_usd'])
            self._notify(
                f"🕐 <b>PAPER SESSION END — position closed</b>\n"
                f"Direction : {closed['direction']}\n"
                f"Exit ~    : ${current_price:,.2f}\n"
                f"PnL       : {sign}${closed['pnl_usd']:,.2f}"
            )
        else:
            self._notify(f"🕐 Session ended [{dt_str}] — no open paper trade.")

    def _handle_paper_force_close(self, current_price: float) -> None:
        """Paper mode: manual close from Telegram /close (no exchange call)."""
        log.info("PAPER force-close requested via Telegram.")
        closed = self._trade_log.close_trade(current_price, reason="MANUAL_CLOSE")
        self._had_position   = False
        self._state.position = None
        self._sm.reset()
        if closed:
            sign = self._sign_char(closed['pnl_usd'])
            self._notify(
                f"🛑 <b>PAPER MANUAL CLOSE</b>\n"
                f"Direction : {closed['direction']}\n"
                f"Exit ~    : ${current_price:,.2f}\n"
                f"PnL       : {sign}${closed['pnl_usd']:,.2f}"
            )

    # ── session end close ─────────────────────────────────────────────────────

    def _session_end_close(self, dt_str: str, current_price: float) -> None:
        log.info("SESSION END [%s] — cancelling orders and closing position.", dt_str)
        try:
            self._orders.cancel_all_orders()
            self._orders.close_position(reason='SESSION_END')
        except Exception as exc:
            log.error("Session-end close failed: %s", exc)
            self._notify(f"⚠️ Session-end close failed: {exc}")
            return

        closed = self._trade_log.close_trade(current_price, reason="SESSION_END")
        self._had_position   = False
        self._state.position = None
        self._sm.reset()

        if closed:
            sign = self._sign_char(closed["pnl_usd"])
            self._notify(
                f"🕐 <b>SESSION END — position closed</b>\n"
                f"Direction : {closed['direction']}\n"
                f"Exit ~    : ${current_price:,.2f}\n"
                f"PnL       : {sign}${closed['pnl_usd']:,.2f}"
            )
        else:
            self._notify(f"🕐 Session ended [{dt_str}] — no open trade to log.")

    # ── force-close from Telegram /close ─────────────────────────────────────

    def _handle_force_close(self, current_price: float) -> None:
        log.info("Force-close requested via Telegram.")
        try:
            self._orders.cancel_all_orders()
            self._orders.close_position(reason='MANUAL_CLOSE')
        except Exception as exc:
            log.error("Force-close failed: %s", exc)
            self._notify(f"⚠️ Force-close failed: {exc}")
            return

        closed = self._trade_log.close_trade(current_price, reason="MANUAL_CLOSE")
        self._had_position   = False
        self._state.position = None
        self._sm.reset()

        if closed:
            sign = self._sign_char(closed["pnl_usd"])
            self._notify(
                f"🛑 <b>MANUAL CLOSE</b>\n"
                f"Direction : {closed['direction']}\n"
                f"Exit ~    : ${current_price:,.2f}\n"
                f"PnL       : {sign}${closed['pnl_usd']:,.2f}"
            )

    # ── strategy hot-swap ─────────────────────────────────────────────────────

    def _switch_strategy(self, n: int) -> None:
        """Replace the active state machine with strategy *n*."""
        try:
            self._sm = get_strategy(
                n,
                long_pullback_max=C.LONG_PULLBACK_MAX,
                short_pullback_max=C.SHORT_PULLBACK_MAX,
                long_window_periods=C.LONG_WINDOW_PERIODS,
                short_window_periods=C.SHORT_WINDOW_PERIODS,
                window_price_offset=C.WINDOW_PRICE_OFFSET,
                enable_long=C.ENABLE_LONG,
                enable_short=C.ENABLE_SHORT,
                trend_filter=C.TREND_FILTER,
                trend_filter_bars=C.TREND_FILTER_BARS,
            )
            self._state.active_strategy = n
            name = strategy_name(n)
            log.info("Strategy switched to #%d: %s", n, name)
            self._notify(f"🔄 <b>Strategy #{n} active</b>\n{name}")
        except ValueError as exc:
            log.error("Strategy switch failed: %s", exc)
            self._notify(f"⚠️ Strategy switch failed: {exc}")

    # ── WebSocket callback ────────────────────────────────────────────────────

    def _on_kline(self, msg: dict) -> None:
        try:
            data = msg.get('data', [])
            if not data:
                return

            candle = data[0]
            if not candle.get('confirm', False):
                return   # candle still forming

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
                "Bar [%s] O=%.4f H=%.4f L=%.4f C=%.4f",
                dt_str, bar['open'], bar['high'], bar['low'], bar['close'],
            )

            if not self._engine.is_warm:
                log.debug("Indicators not warm — skipping.")
                return

            ind = self._engine.compute()
            if ind is None:
                return

            current_price = bar['close']
            in_position   = (self._state.position is not None) if C.PAPER_MODE else (not self._orders.is_flat())

            # ── 1. Detect SL/TP exit ──────────────────────────────────────────
            if C.PAPER_MODE:
                self._check_paper_position_closed(bar)
            else:
                self._check_position_closed(current_price)
            # Re-read in_position after potential close above
            in_position = (self._state.position is not None) if C.PAPER_MODE else (not self._orders.is_flat())

            # ── 2. Strategy switch from Telegram /set ─────────────────────────
            pending_strategy = self._state.consume_strategy_switch()
            if pending_strategy and not in_position:
                self._switch_strategy(pending_strategy)
            elif pending_strategy and in_position:
                # Requeue — can't switch while a position is open
                self._state.request_strategy_switch(pending_strategy)
                log.debug("Strategy switch deferred — position open.")

            # ── 3. Handle /close from Telegram ───────────────────────────────
            if self._state.consume_close_request():
                if C.PAPER_MODE:
                    if self._state.position:
                        self._handle_paper_force_close(current_price)
                else:
                    if not self._orders.is_flat():
                        self._handle_force_close(current_price)
                return

            # ── 4. Session end: close any open position ───────────────────────
            if self._past_session_end(ts_ms):
                if C.PAPER_MODE:
                    if self._state.position:
                        self._session_end_paper_close(dt_str, current_price)
                else:
                    if not self._orders.is_flat():
                        self._session_end_close(dt_str, current_price)
                return

            # ── 5. Session filter: no new entries outside window ──────────────
            if not self._in_session(ts_ms):
                log.debug("Outside session [%s] — skip.", dt_str)
                return

            # ── 6. Already in a position: waiting for SL/TP ──────────────────
            if in_position:
                self._state.phase = self._sm.state
                log.debug("In position — holding.")
                return

            # ── 7. Paused via Telegram ────────────────────────────────────────
            if self._state.is_paused:
                log.debug("Bot paused — no new entries.")
                return

            # ── 8. Volatility regime pre-filter ──────────────────────────────
            is_vol_expanding = (
                ind['atr'] > ind['atr_regime'] if C.USE_VOLATILITY_REGIME else True
            )
            if not is_vol_expanding:
                log.debug("Vol contracting (ATR %.4f < regime %.4f) — skip.", ind['atr'], ind['atr_regime'])

            # ── 9. State machine ──────────────────────────────────────────────
            signal = self._sm.process_bar(ind, self._bar_index, is_vol_expanding)
            self._state.phase = self._sm.state
            log.debug("SM: state=%s  signal=%s", self._sm.state, signal)

            if signal:
                self._execute_entry(signal, ind)

        except Exception as exc:
            log.exception("Error processing kline: %s", exc)
            if self._tg:
                self._notify(f"⚠️ Bot error: {exc}")

    # ── main run loop ─────────────────────────────────────────────────────────

    def run(self) -> None:
        # Start Telegram bot first so it's ready before we announce startup
        if self._tg:
            self._tg.start()
            net = "TESTNET" if C.TESTNET else "MAINNET"
            mode_tag = f" · PAPER" if C.PAPER_MODE else ""
            self._notify(
                f"🤖 <b>XAUT Bot started [{net}{mode_tag}]</b>\n"
                f"Session : {C.SESSION_START_HOUR:02d}:00–{C.SESSION_END_HOUR:02d}:00 UTC\n"
                f"Risk    : {C.RISK_PERCENT*100:.2f}%\n"
                f"Use /status for live info."
            )

        log.info("Starting WebSocket kline stream (5-min, %s)…", C.SYMBOL)
        ws = WebSocket(testnet=C.TESTNET, channel_type='linear')
        ws.kline_stream(callback=self._on_kline, symbol=C.SYMBOL, interval=5)

        log.info("Bot is live.  Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down…")
            ws.exit()
            if self._tg:
                self._notify("🔴 Bot stopped (manual shutdown).")
            log.info("Bot stopped.")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    bot = XAUTBot()
    bot.run()
