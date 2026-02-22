"""Advanced Sunrise Strategy - XAUT/USDT BYBIT OPTIMIZED
=========================================================
BYBIT VERSION: Optimized for XAUT/USDT (Tether Gold perpetual) on Bybit.

Extends the production-grade SunriseOgle strategy with three Bybit-specific
upgrades:

  1. AI VOLATILITY REGIME FILTER
     --------------------------------
     A 20-period EMA is applied to the ATR.  A trade signal is only forwarded
     to the 4-phase state machine when the current ATR is **above** its EMA
     (i.e., volatility is expanding).  This eliminates low-momentum, choppy
     periods that produce many false pullback breakouts.

     Config flags: USE_VOLATILITY_REGIME, ATR_EMA_LOOKBACK

  2. INTRADAY SESSION CONTROL + FORCED EXIT
     -----------------------------------------
     Entries are restricted to the high-momentum London / New York overlap
     (13:00–18:00 UTC by default).  Any open position that is still active when
     the session boundary is reached is force-closed at market price with all
     protective OCA orders cancelled first.  This prevents overnight exposure on
     a perpetual contract.

     Config flags: USE_TIME_RANGE_FILTER, USE_SESSION_END_EXIT,
                   ENTRY_START_HOUR, ENTRY_END_HOUR

  3. BYBIT LOT-SIZE POSITION SIZING
     -----------------------------------
     Position size is floored to the exchange's minimum lot increment (0.01
     XAUT) rather than using integer contract multiples.  This is required
     because Bybit's XAUT/USDT contract allows fractional sizing whereas
     traditional forex brokers use 100-oz lots.

     Config flag: BYBIT_LOT_SIZE

ENTRY SYSTEM
------------
Fully inherits the 4-phase Volatility Expansion Channel state machine from
SunriseOgle (SCANNING → ARMED → WINDOW_OPEN → BREAKOUT).  Both LONG and
SHORT directions are enabled by default for Bybit dual-market opportunity.

PARAMETERS (XAUT Bybit defaults)
---------------------------------
  ema_fast_length   = 12   (faster EMA for crypto volatility)
  ema_medium_length = 18
  ema_slow_length   = 26
  atr_length        = 14
  long_atr_sl_multiplier  = 2.5   (tighter intraday stop)
  long_atr_tp_multiplier  = 9.0   (aggressive momentum target)
  short_atr_sl_multiplier = 2.5
  short_atr_tp_multiplier = 6.5
  long_pullback_max_candles = 2   (optimised for 5-min crypto cycles)
  risk_percent      = 0.01  (1% account risk per trade)
  contract_size     = 1     (1 XAUT token = 1 oz Tether Gold)

DISCLAIMER
----------
Educational and research purposes ONLY.  Not investment advice.
Trading perpetual contracts involves substantial risk of loss.
Past backtest performance does not guarantee future results.
"""
from __future__ import annotations

import math
from pathlib import Path

import backtrader as bt

# Import the production base strategy
from sunrise_ogle_xauusd import SunriseOgle   # noqa: E402  (relative import at runtime)

# =============================================================
# BYBIT XAUT CONFIGURATION
# =============================================================

# === INSTRUMENT ===
DATA_FILENAME = 'XAUTUSDT_5m_Bybit.csv'      # Bybit XAUT/USDT 5-minute data

# === BACKTEST SETTINGS ===
FROMDATE = '2020-07-10'
TODATE   = '2025-07-25'
STARTING_CASH = 100_000.0
QUICK_TEST    = False
ENABLE_PLOT   = True

# === BYBIT EXCHANGE SPECIFICATIONS ===
BYBIT_TICK_SIZE = 0.01                        # Minimum price increment
BYBIT_LOT_SIZE  = 0.01                        # Minimum trade size increment

# === TRADING DIRECTION ===
ENABLE_LONG_TRADES  = True
ENABLE_SHORT_TRADES = True                    # Both directions for dual-market edge

# === INTRADAY SESSION FILTER (London / NY overlap, UTC) ===
USE_TIME_RANGE_FILTER = True
USE_SESSION_END_EXIT  = True                  # Force-close at session end
ENTRY_START_HOUR   = 13
ENTRY_START_MINUTE = 0
ENTRY_END_HOUR     = 18
ENTRY_END_MINUTE   = 0

# === AI VOLATILITY REGIME FILTER ===
USE_VOLATILITY_REGIME = True                  # Only trade expanding-volatility regimes
ATR_EMA_LOOKBACK      = 20                    # EMA period for ATR regime baseline

# === DEBUG ===
VERBOSE_DEBUG  = False
PRINT_SIGNALS  = False


# =============================================================
# BYBIT XAUT STRATEGY  (extends SunriseOgle)
# =============================================================

class SunriseOgleXAUT(SunriseOgle):
    """Bybit XAUT/USDT optimised strategy.

    Inherits the complete 4-phase Volatility Expansion Channel entry system
    from SunriseOgle and enables:
      - ATR volatility regime pre-filter
      - Session-end forced position exit
      - Bybit fractional lot-size position sizing
    All three features are toggled via params so they can be disabled for
    A/B testing against the baseline XAUUSD configuration.
    """

    # Override default params for Bybit XAUT.
    # Backtrader merges child params with parent params; only overrides shown here.
    params = dict(
        # --- XAUT-optimised EMA periods (faster response for crypto) ---
        ema_fast_length=12,
        ema_medium_length=18,
        ema_slow_length=26,

        # --- ATR period ---
        atr_length=14,

        # --- Risk management (tighter intraday stop, aggressive TP) ---
        long_atr_sl_multiplier=2.5,
        long_atr_tp_multiplier=9.0,
        short_atr_sl_multiplier=2.5,
        short_atr_tp_multiplier=6.5,

        # --- Pullback tuning (2-candle pullback for 5-min crypto cycles) ---
        long_pullback_max_candles=2,

        # --- Risk sizing ---
        risk_percent=0.01,

        # --- Contract spec (1 XAUT = 1 oz of Tether Gold) ---
        contract_size=1,

        # --- Trading direction ---
        enable_long_trades=ENABLE_LONG_TRADES,
        enable_short_trades=ENABLE_SHORT_TRADES,

        # --- Session filter ---
        use_time_range_filter=USE_TIME_RANGE_FILTER,
        use_session_end_exit=USE_SESSION_END_EXIT,
        entry_start_hour=ENTRY_START_HOUR,
        entry_start_minute=ENTRY_START_MINUTE,
        entry_end_hour=ENTRY_END_HOUR,
        entry_end_minute=ENTRY_END_MINUTE,

        # --- AI Volatility Regime ---
        use_volatility_regime=USE_VOLATILITY_REGIME,
        atr_regime_lookback=ATR_EMA_LOOKBACK,

        # --- Bybit lot sizing ---
        bybit_lot_size=BYBIT_LOT_SIZE,

        # --- Disable forex position calc (not applicable for Bybit perpetual) ---
        use_forex_position_calc=False,

        # --- Debug ---
        verbose_debug=VERBOSE_DEBUG,
        print_signals=PRINT_SIGNALS,
    )


# =============================================================
# RUNNER
# =============================================================

if __name__ == '__main__':
    from datetime import datetime, timedelta

    if QUICK_TEST:
        try:
            td_obj = datetime.strptime(TODATE, '%Y-%m-%d')
            FROMDATE = (td_obj - timedelta(days=10)).strftime('%Y-%m-%d')
        except Exception:
            pass

    BASE      = Path(__file__).resolve().parent.parent.parent
    DATA_FILE = BASE / 'data' / DATA_FILENAME

    if not DATA_FILE.exists():
        print(f"Data file not found: {DATA_FILE}")
        print("Place XAUTUSDT_5m_Bybit.csv in the data/ directory and retry.")
        raise SystemExit(1)

    def parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, '%Y-%m-%d')
        except Exception:
            return None

    feed_kwargs = dict(
        dataname=str(DATA_FILE),
        dtformat='%Y%m%d',
        tmformat='%H:%M:%S',
        datetime=0, time=1, open=2, high=3, low=4, close=5, volume=6,
        timeframe=bt.TimeFrame.Minutes,
        compression=5,
    )
    fd = parse_date(FROMDATE)
    td = parse_date(TODATE)
    if fd:
        feed_kwargs['fromdate'] = fd
    if td:
        feed_kwargs['todate'] = td

    data = bt.feeds.GenericCSVData(**feed_kwargs)

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(data)
    cerebro.broker.setcash(STARTING_CASH)
    # Bybit XAUT maker/taker fee (~0.055% per side)
    cerebro.broker.setcommission(commission=0.00055)

    cerebro.addstrategy(SunriseOgleXAUT)

    # Performance analysers
    cerebro.addanalyzer(bt.analyzers.SharpeRatio,  _name='sharpe',
                        timeframe=bt.TimeFrame.Days, riskfreerate=0.0)
    cerebro.addanalyzer(bt.analyzers.DrawDown,      _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.Returns,       _name='returns')

    # Visual observers
    try:
        cerebro.addobserver(
            bt.observers.BuySell,
            barplot=False,
            plotdist=SunriseOgleXAUT.params.buy_sell_plotdist,
        )
    except Exception:
        pass

    try:
        cerebro.addobserver(bt.observers.Value)
    except Exception:
        pass

    print(f"=== SUNRISE OGLE XAUT (Bybit) ===")
    print(f"   Instrument : XAUT/USDT")
    print(f"   Period     : {FROMDATE}  →  {TODATE}")
    print(f"   Session    : {ENTRY_START_HOUR:02d}:00 – {ENTRY_END_HOUR:02d}:00 UTC")
    print(f"   Vol Regime : {'ENABLED' if USE_VOLATILITY_REGIME else 'DISABLED'} "
          f"(ATR EMA-{ATR_EMA_LOOKBACK})")
    print(f"   Lot Size   : {BYBIT_LOT_SIZE} XAUT")
    print(f"   Direction  : {'LONG+SHORT' if ENABLE_LONG_TRADES and ENABLE_SHORT_TRADES else 'LONG' if ENABLE_LONG_TRADES else 'SHORT'}")
    print()

    results    = cerebro.run()
    final_val  = cerebro.broker.getvalue()
    strat      = results[0]

    # ── Summary ──────────────────────────────────────────────────────────────
    pnl        = final_val - STARTING_CASH
    ret_pct    = pnl / STARTING_CASH * 100

    sharpe_val = strat.analyzers.sharpe.get_analysis().get('sharperatio', None)
    dd_info    = strat.analyzers.drawdown.get_analysis()
    max_dd     = dd_info.get('max', {}).get('drawdown', 0.0)
    ta         = strat.analyzers.trades.get_analysis()
    total_t    = ta.get('total', {}).get('total', 0)
    won_t      = ta.get('won',   {}).get('total', 0)
    gross_p    = ta.get('won',   {}).get('pnl', {}).get('total', 0.0)
    gross_l    = abs(ta.get('lost', {}).get('pnl', {}).get('total', 0.0))
    pf         = gross_p / gross_l if gross_l > 0 else float('inf')
    wr         = won_t / total_t * 100 if total_t > 0 else 0.0

    print(f"\n{'='*60}")
    print(f"XAUT BYBIT STRATEGY RESULTS")
    print(f"{'='*60}")
    print(f"Starting Cash : ${STARTING_CASH:>12,.2f}")
    print(f"Final Value   : ${final_val:>12,.2f}  ({ret_pct:+.2f}%)")
    print(f"Total PnL     : ${pnl:>+12,.2f}")
    print(f"{'─'*60}")
    print(f"Total Trades  : {total_t}")
    print(f"Win Rate      : {wr:.2f}%  ({won_t}W / {total_t - won_t}L)")
    print(f"Profit Factor : {pf:.2f}")
    print(f"Max Drawdown  : {max_dd:.2f}%")
    print(f"Sharpe Ratio  : {sharpe_val:.3f}" if sharpe_val is not None else "Sharpe Ratio  : N/A")
    print(f"{'='*60}\n")

    if ENABLE_PLOT:
        cerebro.plot(style='candlestick', volume=False)
