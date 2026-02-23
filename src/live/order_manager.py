"""
order_manager.py — Bybit v5 API wrapper
=========================================
Handles all exchange interaction:
  - Account equity query
  - Position query
  - Market order placement with attached SL / TP
  - Position close (reduce-only market order)
  - Cancel all open orders

Uses pybit's unified_trading.HTTP client.
All qty and price values are rounded to Bybit's XAUTUSDT instrument specs
before being sent to the API.

XAUTUSDT instrument specs (Bybit linear)
-----------------------------------------
  Minimum order qty : 0.001 XAUT
  Qty step          : 0.001 XAUT
  Price tick        : 0.01  USDT
"""
from __future__ import annotations

import logging
import time
from decimal import ROUND_DOWN, Decimal

from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)

# ── XAUTUSDT contract specs ───────────────────────────────────────────────────
_QTY_STEP   = Decimal('0.001')   # Minimum position size increment
_PRICE_TICK = Decimal('0.01')    # Minimum price increment
_MIN_QTY    = Decimal('0.001')   # Minimum order size


def _fmt_qty(qty: float) -> str:
    """Floor qty to nearest QTY_STEP and return as string for Bybit API."""
    d = Decimal(str(qty)).quantize(_QTY_STEP, rounding=ROUND_DOWN)
    return str(d)


def _fmt_price(price: float) -> str:
    """Round price to PRICE_TICK and return as string for Bybit API."""
    d = Decimal(str(price)).quantize(_PRICE_TICK, rounding=ROUND_DOWN)
    return str(d)


class OrderManager:
    """
    Thin, focused wrapper around the Bybit v5 REST API.
    Retries transient network errors up to MAX_RETRIES times.
    """

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0   # seconds between retries

    def __init__(
        self,
        api_key:    str,
        api_secret: str,
        testnet:    bool = True,
        symbol:     str  = 'XAUTUSDT',
    ) -> None:
        self._session = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
        )
        self._symbol   = symbol
        self._category = 'linear'
        log.info(
            f"OrderManager ready | symbol={symbol} | "
            f"{'TESTNET' if testnet else '*** MAINNET ***'}"
        )

    # ── account ───────────────────────────────────────────────────────────────

    def get_equity(self) -> float:
        """
        Return total USDT equity (wallet balance + unrealised PnL).
        Endpoint: GET /v5/account/wallet-balance
        """
        resp = self._call(
            self._session.get_wallet_balance,
            accountType='UNIFIED',
            coin='USDT',
        )
        for coin in resp['result']['list'][0]['coin']:
            if coin['coin'] == 'USDT':
                equity = float(coin['equity'])
                log.debug(f"Equity: {equity:.2f} USDT")
                return equity
        raise ValueError("USDT coin not found in wallet balance response")

    # ── position ──────────────────────────────────────────────────────────────

    def get_position(self) -> dict | None:
        """
        Return the current open position or None if flat.
        Endpoint: GET /v5/position/list

        Returned dict keys
        ------------------
        side          : 'Buy' or 'Sell'
        size          : float (qty in XAUT)
        entry_price   : float (average entry price)
        stop_loss     : float (0 if none)
        take_profit   : float (0 if none)
        unrealised_pnl: float
        """
        resp = self._call(
            self._session.get_positions,
            category=self._category,
            symbol=self._symbol,
        )
        for pos in resp['result']['list']:
            size = float(pos.get('size', 0) or 0)
            if size > 0:
                return {
                    'side':           pos['side'],
                    'size':           size,
                    'entry_price':    float(pos.get('avgPrice', 0) or 0),
                    'stop_loss':      float(pos.get('stopLoss', 0) or 0),
                    'take_profit':    float(pos.get('takeProfit', 0) or 0),
                    'unrealised_pnl': float(pos.get('unrealisedPnl', 0) or 0),
                }
        return None

    def is_flat(self) -> bool:
        return self.get_position() is None

    # ── orders ────────────────────────────────────────────────────────────────

    def place_entry(
        self,
        direction:   str,    # 'LONG' or 'SHORT'
        size:        float,
        stop_loss:   float,
        take_profit: float,
    ) -> dict:
        """
        Place a market entry order with SL and TP attached.
        Endpoint: POST /v5/order/create

        Bybit attaches SL/TP directly to the position when included in the
        order payload — no separate order needed.
        """
        side = 'Buy' if direction == 'LONG' else 'Sell'
        qty  = _fmt_qty(size)
        sl   = _fmt_price(stop_loss)
        tp   = _fmt_price(take_profit)

        # Safety check: qty must meet minimum
        if Decimal(qty) < _MIN_QTY:
            raise ValueError(
                f"Calculated qty {qty} is below minimum {_MIN_QTY}. "
                f"Increase account equity or reduce ATR SL multiplier."
            )

        log.info(
            f"ENTRY ORDER  {direction} | qty={qty} XAUT | "
            f"SL={sl}  TP={tp}"
        )

        resp = self._call(
            self._session.place_order,
            category=self._category,
            symbol=self._symbol,
            side=side,
            orderType='Market',
            qty=qty,
            stopLoss=sl,
            takeProfit=tp,
            slTriggerBy='MarkPrice',
            tpTriggerBy='MarkPrice',
            timeInForce='IOC',
            reduceOnly=False,
        )
        order_id = resp['result'].get('orderId', 'unknown')
        log.info(f"Order accepted | orderId={order_id}")
        return resp['result']

    def close_position(self, reason: str = 'MANUAL') -> dict | None:
        """
        Close the current position with a reduce-only market order.
        Endpoint: POST /v5/order/create (reduceOnly=True)
        """
        pos = self.get_position()
        if pos is None:
            log.info("close_position: no open position — nothing to do.")
            return None

        close_side = 'Sell' if pos['side'] == 'Buy' else 'Buy'
        qty        = _fmt_qty(pos['size'])

        log.info(
            f"CLOSE POSITION [{reason}] | {close_side} {qty} XAUT | "
            f"entry={pos['entry_price']:.4f}  upnl={pos['unrealised_pnl']:+.2f}"
        )

        resp = self._call(
            self._session.place_order,
            category=self._category,
            symbol=self._symbol,
            side=close_side,
            orderType='Market',
            qty=qty,
            timeInForce='IOC',
            reduceOnly=True,
        )
        log.info(f"Close order accepted | orderId={resp['result'].get('orderId')}")
        return resp['result']

    def cancel_all_orders(self) -> None:
        """
        Cancel all open orders for the symbol.
        Endpoint: POST /v5/order/cancel-all
        """
        try:
            self._call(
                self._session.cancel_all_orders,
                category=self._category,
                symbol=self._symbol,
            )
            log.info("All open orders cancelled.")
        except Exception as exc:
            log.warning(f"cancel_all_orders: {exc}")

    # ── retry wrapper ─────────────────────────────────────────────────────────

    def _call(self, fn, **kwargs):
        """Call a pybit API function with simple retry on transient errors."""
        last_exc = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = fn(**kwargs)
                # Bybit returns retCode=0 on success
                rc = resp.get('retCode', -1)
                if rc != 0:
                    raise RuntimeError(
                        f"Bybit API error retCode={rc}: {resp.get('retMsg', 'unknown')}"
                    )
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt < self.MAX_RETRIES:
                    log.warning(
                        f"API call failed (attempt {attempt}/{self.MAX_RETRIES}): {exc}. "
                        f"Retrying in {self.RETRY_DELAY}s…"
                    )
                    time.sleep(self.RETRY_DELAY)
        raise RuntimeError(
            f"API call failed after {self.MAX_RETRIES} attempts: {last_exc}"
        )
