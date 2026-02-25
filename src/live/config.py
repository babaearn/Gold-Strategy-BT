"""
config.py — XAUT Live Bot configuration
========================================
All values come from environment variables so nothing sensitive lives in code.

On Railway: set these in the service's "Variables" tab.
Locally:    create a .env file (see .env.example at repo root) and run the bot.
"""
import os
from dotenv import load_dotenv

# Load .env file when running locally (no-op on Railway where vars are injected)
load_dotenv()


# ── helpers ───────────────────────────────────────────────────────────────────

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Add it to your .env file (local) or Railway Variables (deploy)."
        )
    return val


def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ('1', 'true', 'yes')


def _float(key: str, default: float) -> float:
    val = os.getenv(key)
    return float(val) if val else default


def _int(key: str, default: int) -> int:
    val = os.getenv(key)
    return int(val) if val else default


# ── Bybit credentials ─────────────────────────────────────────────────────────
BYBIT_API_KEY    = _require('BYBIT_API_KEY')
BYBIT_API_SECRET = _require('BYBIT_API_SECRET')
# BYBIT_TESTNET=true  → Bybit Testnet (paper trading, safe to experiment)
# BYBIT_TESTNET=false → Bybit Mainnet (real money)
TESTNET = _bool('BYBIT_TESTNET', default=True)
# PAPER_MODE=true → virtual positions tracked candle-by-candle, no real orders placed
PAPER_MODE = _bool('PAPER_MODE', default=False)

# ── Instrument ────────────────────────────────────────────────────────────────
SYMBOL   = os.getenv('SYMBOL', 'XAUTUSDT')
CATEGORY = 'linear'                        # USDT-margined perpetual

# ── Strategy parameters ───────────────────────────────────────────────────────
# Mirror the SunriseOgleXAUT defaults; override via env var if needed.
EMA_FAST            = _int('EMA_FAST',   12)
EMA_MEDIUM          = _int('EMA_MEDIUM', 18)
EMA_SLOW            = _int('EMA_SLOW',   26)
EMA_CONFIRM         = _int('EMA_CONFIRM', 1)
ATR_PERIOD          = _int('ATR_PERIOD', 14)
ATR_REGIME_LOOKBACK = _int('ATR_REGIME_LOOKBACK', 20)

LONG_ATR_SL_MULT  = _float('LONG_ATR_SL_MULT',  2.5)
LONG_ATR_TP_MULT  = _float('LONG_ATR_TP_MULT',  9.0)
SHORT_ATR_SL_MULT = _float('SHORT_ATR_SL_MULT', 2.5)
SHORT_ATR_TP_MULT = _float('SHORT_ATR_TP_MULT', 6.5)

LONG_PULLBACK_MAX    = _int('LONG_PULLBACK_MAX',    2)
SHORT_PULLBACK_MAX   = _int('SHORT_PULLBACK_MAX',   2)
LONG_WINDOW_PERIODS  = _int('LONG_WINDOW_PERIODS',  5)
SHORT_WINDOW_PERIODS = _int('SHORT_WINDOW_PERIODS', 7)
WINDOW_PRICE_OFFSET  = _float('WINDOW_PRICE_OFFSET', 0.001)

# ── Risk / position sizing ────────────────────────────────────────────────────
RISK_PERCENT = _float('RISK_PERCENT', 0.01)   # 1% of equity per trade

# ── Session filter (UTC hours) ────────────────────────────────────────────────
USE_SESSION_FILTER = _bool('USE_SESSION_FILTER', True)
SESSION_START_HOUR = _int('SESSION_START_HOUR', 13)   # 13:00 UTC — London open
SESSION_END_HOUR   = _int('SESSION_END_HOUR',   18)   # 18:00 UTC — NY afternoon

# ── Volatility regime filter ──────────────────────────────────────────────────
USE_VOLATILITY_REGIME = _bool('USE_VOLATILITY_REGIME', True)

# ── Trade direction ───────────────────────────────────────────────────────────
ENABLE_LONG  = _bool('ENABLE_LONG',  True)
ENABLE_SHORT = _bool('ENABLE_SHORT', True)

# ── Trend filter (optional phase-1 quality filter) ───────────────────────────
# When TREND_FILTER=true a LONG signal is only accepted when ema_slow is higher
# now than it was TREND_FILTER_BARS ago (and vice-versa for SHORT).
# Recommended value: 15 bars (75 minutes on the 5-min chart).
# Best back-tested config: TREND_FILTER=true, TREND_FILTER_BARS=15,
#   LONG_ATR_TP_MULT=12.0, SHORT_ATR_TP_MULT=9.0  → +52% / 8.4% MaxDD (5yr)
TREND_FILTER      = _bool('TREND_FILTER',       default=False)
TREND_FILTER_BARS = _int( 'TREND_FILTER_BARS',  15)

# ── Strategy selection ────────────────────────────────────────────────────────
# Which strategy to run on startup.
# Switch at runtime via Telegram /set <n> without restarting the bot.
#   1 = Volatility Expansion Channel (4-Phase) — default
#   2 = (add your next strategy and set ACTIVE_STRATEGY=2)
ACTIVE_STRATEGY = _int('ACTIVE_STRATEGY', 1)

# ── Indicator warmup ──────────────────────────────────────────────────────────
# Minimum bars needed before any indicator is fully warmed up.
# Using 3× the longest period gives a generous buffer.
WARMUP_BARS = max(EMA_SLOW, ATR_PERIOD, ATR_REGIME_LOOKBACK) * 3

# ── Telegram bot (optional) ───────────────────────────────────────────────────
# Leave blank to run without Telegram; set both to enable.
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN',   '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
TELEGRAM_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
