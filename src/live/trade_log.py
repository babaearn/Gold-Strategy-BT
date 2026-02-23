"""
trade_log.py — Persistent trade journal for PnL tracking.
==========================================================
Trades are appended to a JSON file so history survives restarts.
All methods are thread-safe.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_DEFAULT_FILE = os.getenv("TRADE_LOG_FILE", "data/live_trades.json")


class TradeLog:
    """Thread-safe trade journal with JSON persistence."""

    def __init__(self, filepath: str = _DEFAULT_FILE) -> None:
        self._file = Path(filepath)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._trades: List[Dict] = self._load()
        self._open: Optional[Dict] = None

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> List[Dict]:
        if self._file.exists():
            try:
                with open(self._file) as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self) -> None:
        with open(self._file, "w") as f:
            json.dump(self._trades, f, indent=2, default=str)

    # ── trade lifecycle ───────────────────────────────────────────────────────

    def open_trade(
        self,
        direction: str,
        entry_price: float,
        size: float,
        sl: float,
        tp: float,
    ) -> None:
        with self._lock:
            self._open = {
                "direction":   direction,
                "entry_price": entry_price,
                "size":        size,
                "sl":          sl,
                "tp":          tp,
                "entry_time":  datetime.now(timezone.utc).isoformat(),
                "exit_price":  None,
                "exit_time":   None,
                "pnl_usd":     None,
                "reason":      None,
            }

    def close_trade(self, exit_price: float, reason: str = "") -> Optional[Dict]:
        """Record a closed trade.  Returns the completed trade dict, or None if no open trade."""
        with self._lock:
            if self._open is None:
                return None
            t    = self._open
            size = t["size"]
            ep   = t["entry_price"]
            pnl  = (exit_price - ep) * size if t["direction"] == "LONG" else (ep - exit_price) * size
            t["exit_price"] = exit_price
            t["exit_time"]  = datetime.now(timezone.utc).isoformat()
            t["pnl_usd"]    = round(pnl, 4)
            t["reason"]     = reason
            self._trades.append(t)
            self._save()
            self._open = None
            return dict(t)

    def get_open(self) -> Optional[Dict]:
        with self._lock:
            return dict(self._open) if self._open else None

    # ── PnL queries ───────────────────────────────────────────────────────────

    def _closed(self) -> List[Dict]:
        return [t for t in self._trades if t.get("pnl_usd") is not None]

    def pnl_today(self) -> Dict:
        today  = datetime.now(timezone.utc).date().isoformat()
        trades = [t for t in self._closed() if (t["exit_time"] or "")[:10] == today]
        total  = sum(t["pnl_usd"] for t in trades)
        return {"total_usd": round(total, 2), "trades": len(trades)}

    def pnl_month(self, year: int = None, month: int = None) -> Dict:
        now    = datetime.now(timezone.utc)
        prefix = f"{year or now.year}-{(month or now.month):02d}"
        trades = [t for t in self._closed() if (t["exit_time"] or "")[:7] == prefix]
        total  = sum(t["pnl_usd"] for t in trades)
        return {"total_usd": round(total, 2), "trades": len(trades)}

    def pnl_overall(self) -> Dict:
        trades = self._closed()
        if not trades:
            return {
                "total_usd": 0, "trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "avg_win": 0, "avg_loss": 0,
            }
        total  = sum(t["pnl_usd"] for t in trades)
        wins   = [t for t in trades if t["pnl_usd"] > 0]
        losses = [t for t in trades if t["pnl_usd"] <= 0]
        return {
            "total_usd": round(total, 2),
            "trades":    len(trades),
            "wins":      len(wins),
            "losses":    len(losses),
            "win_rate":  round(len(wins) / len(trades) * 100, 1),
            "avg_win":   round(sum(t["pnl_usd"] for t in wins)   / len(wins),   2) if wins   else 0,
            "avg_loss":  round(sum(t["pnl_usd"] for t in losses) / len(losses), 2) if losses else 0,
        }

    def recent(self, n: int = 5) -> List[Dict]:
        return self._closed()[-n:]
