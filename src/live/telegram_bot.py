"""
telegram_bot.py — Telegram control panel for the XAUT live bot.
================================================================
Commands
--------
  /start         — welcome + live status summary
  /status        — position, phase, bot state
  /pnl           — today's closed PnL
  /pnl_month     — this month's PnL
  /pnl_overall   — all-time PnL + win rate + avg win/loss
  /risk <n>      — change risk % on the fly  (e.g. /risk 1.5 → 1.5%)
  /pause         — stop taking new entries (open position is untouched)
  /resume        — resume normal entry scanning
  /close         — force-close the current position on next bar
  /settings      — current strategy config
  /help          — full command list

Architecture
------------
TelegramNotifier runs in a background daemon thread with its own asyncio
event loop so it never blocks the synchronous main trading loop.

Call notifier.send("text") from any thread to push a message to Telegram.

BotState is the thread-safe bridge; the trading loop reads controls
(risk_percent, is_paused, force_close) and writes position snapshots.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

log = logging.getLogger(__name__)


# ── Shared state bridge ───────────────────────────────────────────────────────

class BotState:
    """
    Thread-safe shared state between the trading loop and Telegram bot.

    Trading loop writes: phase, position snapshot.
    Telegram commands write: risk_percent, is_paused, force_close flag.
    """

    def __init__(self, risk_percent: float = 0.01, active_strategy: int = 1) -> None:
        self._lock = threading.Lock()

        # Controls — Telegram → trading loop
        self.risk_percent:    float        = risk_percent
        self.is_paused:       bool         = False
        self._force_close:    bool         = False
        self._strategy_switch: Optional[int] = None   # pending /set request

        # Snapshots — trading loop → Telegram
        self.phase:           str            = "SCANNING"
        self.position:        Optional[dict] = None   # {direction, size, entry, sl, tp}
        self.active_strategy: int            = active_strategy

    # ── controls ──────────────────────────────────────────────────────────────

    def set_risk(self, pct: float) -> None:
        with self._lock:
            self.risk_percent = pct

    def pause(self) -> None:
        with self._lock:
            self.is_paused = True

    def resume(self) -> None:
        with self._lock:
            self.is_paused = False

    def request_close(self) -> None:
        with self._lock:
            self._force_close = True

    def consume_close_request(self) -> bool:
        """Returns True once then resets the flag."""
        with self._lock:
            if self._force_close:
                self._force_close = False
                return True
            return False

    def request_strategy_switch(self, n: int) -> None:
        with self._lock:
            self._strategy_switch = n

    def consume_strategy_switch(self) -> Optional[int]:
        """Returns the pending strategy number once, then resets."""
        with self._lock:
            n = self._strategy_switch
            self._strategy_switch = None
            return n

    # ── snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "risk_percent":    self.risk_percent,
                "is_paused":       self.is_paused,
                "phase":           self.phase,
                "position":        dict(self.position) if self.position else None,
                "active_strategy": self.active_strategy,
            }


# ── Telegram notifier ─────────────────────────────────────────────────────────

class TelegramNotifier:
    """
    Runs python-telegram-bot Application in a background daemon thread.

    Usage:
        notifier = TelegramNotifier(token, chat_id, state, trade_log)
        notifier.start()               # call once at startup
        notifier.send("<b>Hello</b>")  # call from any thread
    """

    def __init__(
        self,
        token:      str,
        chat_id:    str,
        state:      BotState,
        trade_log,
        testnet:    bool = True,
        paper_mode: bool = False,
    ) -> None:
        self._token      = token
        self._chat_id    = str(chat_id)
        self.state       = state
        self._log        = trade_log
        self._testnet    = testnet
        self._paper_mode = paper_mode
        self._loop:    Optional[asyncio.AbstractEventLoop] = None
        self._app:     Optional[Application]               = None

    # ── public: push a message from any thread ────────────────────────────────

    def send(self, text: str) -> None:
        if self._loop is None or self._app is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            ),
            self._loop,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        t = threading.Thread(target=self._thread_main, daemon=True, name="tg-bot")
        t.start()
        # Log a masked chat_id — never print the full ID or token to logs
        masked = self._chat_id[-4:].rjust(len(self._chat_id), '*')
        log.info("Telegram bot started (chat_id=%s)", masked)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_run())

    async def _async_run(self) -> None:
        self._app = Application.builder().token(self._token).build()

        handlers = [
            ("start",       self._cmd_start),
            ("status",      self._cmd_status),
            ("pnl",         self._cmd_pnl),
            ("pnl_month",   self._cmd_pnl_month),
            ("pnl_overall", self._cmd_pnl_overall),
            ("risk",        self._cmd_risk),
            ("set",         self._cmd_set),
            ("pause",       self._cmd_pause),
            ("resume",      self._cmd_resume),
            ("close",       self._cmd_close),
            ("settings",    self._cmd_settings),
            ("help",        self._cmd_help),
        ]
        for name, fn in handlers:
            self._app.add_handler(CommandHandler(name, fn))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        # Block forever; daemon thread dies when main process exits
        await asyncio.Event().wait()

    # ── auth ──────────────────────────────────────────────────────────────────

    def _is_owner(self, update: Update) -> bool:
        return str(update.effective_chat.id) == self._chat_id

    async def _deny(self, update: Update) -> None:
        await update.message.reply_text("Unauthorized.")

    # ── formatting helpers ────────────────────────────────────────────────────

    @staticmethod
    def _sign(val: float) -> str:
        return "+" if val >= 0 else ""

    # ── commands ──────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        s    = self.state.snapshot()
        mode = "PAUSED" if s["is_paused"] else "ACTIVE"
        net  = "TESTNET" if self._testnet else "MAINNET"
        net  = f"{net} · PAPER" if self._paper_mode else net
        await update.message.reply_text(
            f"<b>XAUT Live Bot  [{net}]</b>\n\n"
            f"Status : {mode}\n"
            f"Phase  : {s['phase']}\n"
            f"Risk   : {s['risk_percent']*100:.2f}% per trade\n\n"
            f"Use /help for all commands.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        s    = self.state.snapshot()
        pos  = s["position"]
        mode = "PAUSED" if s["is_paused"] else "ACTIVE"

        if pos:
            pos_str = (
                f"\n\n<b>Open Position</b>\n"
                f"Direction : {pos['direction']}\n"
                f"Size      : {pos['size']:.4f} XAUT\n"
                f"Entry     : ${pos.get('entry', 0):,.2f}\n"
                f"SL        : ${pos.get('sl', 0):,.2f}\n"
                f"TP        : ${pos.get('tp', 0):,.2f}"
            )
        else:
            pos_str = "\n\nNo open position."

        await update.message.reply_text(
            f"<b>Status:</b> {mode}\n"
            f"<b>Phase :</b> {s['phase']}"
            f"{pos_str}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        d = self._log.pnl_today()
        s = self._sign(d["total_usd"])
        await update.message.reply_text(
            f"<b>PnL Today</b>\n"
            f"Trades : {d['trades']}\n"
            f"PnL    : {s}${d['total_usd']:,.2f}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_pnl_month(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        now = datetime.now(timezone.utc)
        d   = self._log.pnl_month()
        s   = self._sign(d["total_usd"])
        await update.message.reply_text(
            f"<b>PnL — {now.strftime('%B %Y')}</b>\n"
            f"Trades : {d['trades']}\n"
            f"PnL    : {s}${d['total_usd']:,.2f}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_pnl_overall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        d = self._log.pnl_overall()
        s = self._sign(d["total_usd"])
        await update.message.reply_text(
            f"<b>Overall PnL</b>\n"
            f"Total Trades : {d['trades']}\n"
            f"Win Rate     : {d['win_rate']}%  ({d['wins']}W / {d['losses']}L)\n"
            f"Avg Win      : +${d['avg_win']:,.2f}\n"
            f"Avg Loss     : ${d['avg_loss']:,.2f}\n"
            f"Net PnL      : {s}${d['total_usd']:,.2f}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_risk(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        args = ctx.args
        if not args:
            s = self.state.snapshot()
            await update.message.reply_text(
                f"Current risk: {s['risk_percent']*100:.2f}%\n"
                f"Usage: /risk 1.5   (sets to 1.5%)"
            )
            return
        try:
            val = float(args[0])
            if not (0 < val <= 10):
                raise ValueError
            self.state.set_risk(val / 100)
            await update.message.reply_text(f"Risk updated to <b>{val:.2f}%</b> per trade.", parse_mode=ParseMode.HTML)
        except ValueError:
            await update.message.reply_text("Invalid. Use: /risk 1.5  (must be between 0 and 10)")

    async def _cmd_set(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        from strategies import REGISTRY, strategy_name, available_strategies
        args = ctx.args
        if not args:
            lines = "\n".join(
                f"  {'▶' if n == self.state.active_strategy else ' '} /set {n}  —  {name}"
                for n, name in available_strategies()
            )
            await update.message.reply_text(
                f"<b>Strategy Selector</b>\n\n"
                f"Active: #{self.state.active_strategy}\n\n"
                f"{lines}\n\n"
                f"Usage: /set 1",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            n = int(args[0])
            if n not in REGISTRY:
                raise ValueError
            if n == self.state.active_strategy:
                await update.message.reply_text(
                    f"Strategy #{n} is already active."
                )
                return
            self.state.request_strategy_switch(n)
            await update.message.reply_text(
                f"Strategy switch to <b>#{n}: {strategy_name(n)}</b> queued.\n"
                f"Applies on next candle close (only if no open position).",
                parse_mode=ParseMode.HTML,
            )
        except ValueError:
            available = list(REGISTRY.keys())
            await update.message.reply_text(
                f"Invalid strategy number. Available: {available}\n"
                f"Usage: /set 1"
            )

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        self.state.pause()
        await update.message.reply_text(
            "Bot <b>PAUSED</b>.\nNo new entries will be taken. Open position is untouched.\nSend /resume to restart.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        self.state.resume()
        await update.message.reply_text(
            "Bot <b>RESUMED</b>. Scanning for entries normally.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        s = self.state.snapshot()
        if not s["position"]:
            await update.message.reply_text("No open position to close.")
            return
        self.state.request_close()
        await update.message.reply_text(
            "Close request sent. Position will be closed on the next bar check."
        )

    async def _cmd_settings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        s = self.state.snapshot()
        import config as C
        from strategies import strategy_name
        await update.message.reply_text(
            f"<b>Settings</b>\n\n"
            f"Strategy     : #{s['active_strategy']} — {strategy_name(s['active_strategy'])}\n"
            f"Risk/trade   : {s['risk_percent']*100:.2f}%\n"
            f"Session      : {C.SESSION_START_HOUR:02d}:00–{C.SESSION_END_HOUR:02d}:00 UTC\n"
            f"EMA periods  : {C.EMA_FAST}/{C.EMA_MEDIUM}/{C.EMA_SLOW}\n"
            f"ATR period   : {C.ATR_PERIOD}\n"
            f"Vol regime   : {'ON' if C.USE_VOLATILITY_REGIME else 'OFF'}\n"
            f"SL mult      : {C.LONG_ATR_SL_MULT}×ATR\n"
            f"TP L / S     : {C.LONG_ATR_TP_MULT}× / {C.SHORT_ATR_TP_MULT}×ATR\n"
            f"Longs        : {'ON' if C.ENABLE_LONG else 'OFF'}\n"
            f"Shorts       : {'ON' if C.ENABLE_SHORT else 'OFF'}\n"
            f"Network      : {'TESTNET' if self._testnet else 'MAINNET'}\n"
            f"Mode         : {'PAPER (no real orders)' if self._paper_mode else 'LIVE'}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        await update.message.reply_text(
            "<b>Commands</b>\n\n"
            "/start           — welcome + status\n"
            "/status          — live position + phase\n"
            "/pnl             — today's PnL\n"
            "/pnl_month       — this month's PnL\n"
            "/pnl_overall     — all-time stats\n"
            "/risk &lt;n&gt;         — set risk % (e.g. /risk 1.5)\n"
            "/set &lt;n&gt;          — switch strategy (e.g. /set 1 or /set 2)\n"
            "/pause           — stop new entries\n"
            "/resume          — resume entries\n"
            "/close           — force-close position\n"
            "/settings        — current config\n"
            "/help            — this message"
            + ("\n\n📡 <b>PAPER MODE</b> — signals tracked, no real orders placed" if self._paper_mode else ""),
            parse_mode=ParseMode.HTML,
        )
