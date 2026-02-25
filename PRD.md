# Product Requirements Document: Gold Trading Bot
### Plain-English Guide — Anyone Can Read This

---

## What Does This Bot Do?

This bot trades **Gold (XAU/USD)** automatically. It watches the gold price all day, finds good moments to buy or sell, and manages the risk so you never lose more than 1% of your account on a single trade.

There are **two strategies** inside the bot. Think of them like two different games with different rules:

| | Strategy 1: State Machine | Strategy 2: ORB |
|---|---|---|
| Style | Momentum breakout | Opening range breakout |
| Trades/month | ~3 | ~18 |
| Win rate | ~55% | ~43% |
| Max drawdown | ~6% | ~15% |
| Best for | Low-stress, slow growth | More active, higher return |

---

# STRATEGY 1: The 4-Phase State Machine (Main Strategy)

## The Simple Idea

> Wait for the market to start moving in one direction. Let it pause briefly. Then ride the resumption of that move.

Imagine a runner who sprints, takes a breath, then sprints again. This strategy buys at the start of the second sprint.

---

## The 4 Phases — Step by Step

### Phase 1: SCANNING (Always watching)

The bot watches for a **crossover signal** — when a fast-moving average line crosses over a slow one.

**Think of it like:** A short-term trend line crossing above a long-term trend line = the market is warming up.

```
Price chart:
              /
    Fast EMA /
────────────X────────────  ← Crossover detected here!
  Slow EMA /
          /
```

The bot also checks: Is the market volatile enough? (ATR > average ATR). If the market is too quiet/flat, it ignores the signal.

---

### Phase 2: ARMED — Wait for the Pullback

After detecting a signal, the bot does NOT enter immediately. It waits for a small counter-move (pullback).

**Why?** Because jumping in right at the crossover often catches you at the worst price. Waiting for the pullback = better entry, less risk.

**For a LONG (buy) signal:**
- Count red (down) candles after the signal
- After 3 red candles → move to Phase 3

**For a SHORT (sell) signal:**
- Count green (up) candles after the signal
- After 2 green candles → move to Phase 3

```
LONG example:
                       ▲ BUY ENTRY (Phase 4)
              ↑ Signal │
              │  🔴    │
              │  🔴    │← 3 red candles = pullback confirmed
              │  🔴    │
──────────────┴────────┴──────────
```

**Kill switch:** If an opposing crossover appears while waiting (e.g. a SHORT signal appears while waiting for a LONG), the bot resets to Phase 1. It won't fight a fresh counter-signal.

---

### Phase 3: WINDOW_OPEN — Draw the Breakout Box

After the pullback, the bot draws an invisible "box" around the last pullback candle.

- **Upper line** = high of pullback candle + small offset
- **Lower line** = low of pullback candle - small offset

The box is only open for a limited number of candles (default: 1–10 bars = 5–50 minutes).

```
                 ┌──────────────┐  ← Upper line (breakout trigger for LONG)
     Pullback →  │   🕯️         │
     candle      └──────────────┘  ← Lower line (breakout trigger for SHORT)
```

If price doesn't break out of the box before the timer expires → reset to Phase 1.

---

### Phase 4: MONITORING — Wait for the Breakout

The bot now watches: does price break above the upper line (for LONG)?

- Price breaks above upper line → **BUY at market**
- Price breaks below lower line → **SELL at market**
- Timer runs out → reset, try again next signal

Once a trade is entered, the bot sets:
- **Stop Loss** = entry − (ATR × 2.5)  ← where it admits it was wrong
- **Take Profit** = entry + (ATR × 12.0)  ← the target reward

---

## Full LONG Trade Example

> **Date:** March 5, 2024 | **Gold price:** ~$2,080

**Step-by-step:**

```
10:00 AM  → Phase 1: EMA-1 crosses above EMA-14. Signal detected. ATR is elevated.
10:05 AM  → Phase 2: Red candle #1. Waiting...
10:10 AM  → Phase 2: Red candle #2. Waiting...
10:15 AM  → Phase 2: Red candle #3. Pullback confirmed! Draw the box.
10:20 AM  → Phase 3: Box is drawn at $2,077–$2,079. Window open (1 bar).
10:25 AM  → Phase 4: Gold ticks up to $2,079.50. Upper line broken!
            → BUY 0.48 oz at $2,079.50
            → Stop Loss set at $2,072.00 (loss = -$7.50/oz × 0.48 = -$3.60 = ~1%)
            → Take Profit set at $2,169.00 (gain = +$89.50/oz × 0.48 = +$43 = ~12%)
...
3 days later → Gold hits $2,169. Trade closes. +12% of risk earned.
```

**Risk/Reward:** Risk $1 to make $4.8. You only need to be right 17% of the time to break even (but this strategy is right ~55% of the time).

---

## Position Sizing — How Much to Trade

The bot always risks exactly **1% of the account** per trade, regardless of price.

```
Formula:
  position_size = (account × 0.01) / stop_distance

Example ($100,000 account, $7.50 stop distance):
  position_size = ($100,000 × 0.01) / $7.50
               = $1,000 / $7.50
               = 133 units

If trade hits stop loss: lose $1,000 = exactly 1% ✓
If trade hits take profit: gain $12,000 = exactly 12% ✓
```

---

---

# STRATEGY 2: ORB — Opening Range Breakout

## The Simple Idea

> Every trading day, gold "thinks" about where it wants to go during the first hour. After that, it commits. Trade the commitment.

---

## How It Works

**Step 1 — Build the Range (first 60 minutes)**

When the London trading session opens (1:00 PM UTC), the bot watches the first 12 candles (12 × 5 min = 60 min) and records:
- Highest price reached
- Lowest price reached

This forms the **Opening Range** (a price box for the morning).

**Step 2 — Wait for a Breakout**

After the first hour, if price moves **above** the range → BUY signal.
If price moves **below** the range → SELL signal.

**Step 3 — Set the Stops**

```
Range = High - Low  (e.g. $2,510 - $2,490 = $20 range)

For a LONG (breakout above $2,510):
  Entry       = $2,510
  Stop Loss   = Entry - (Range × 0.7) = $2,510 - $14 = $2,496
  Take Profit = Entry + (Range × 2.0) = $2,510 + $40 = $2,550
```

**Risk/Reward:** Risk $14 to make $40 = 1:2.86 ratio.

---

## Full ORB Example

> **Date:** January 14, 2025 | **Session:** London open

```
1:00 PM UTC  → Session starts. Start tracking 12 candles.
2:00 PM UTC  → Opening range complete.
               Range High = $2,665
               Range Low  = $2,648
               Range Size = $17

2:05 PM UTC  → Gold prints $2,666 — above range high!
               → BUY triggered
               → Entry = $2,666
               → Stop Loss = $2,666 - ($17 × 0.7) = $2,666 - $11.90 = $2,654.10
               → Take Profit = $2,666 + ($17 × 2.0) = $2,666 + $34 = $2,700

5:30 PM UTC  → Gold reaches $2,700. Trade closes. WIN!
               Profit = $34 × position size
```

---

## ORB vs State Machine — Which to Use?

| Situation | Use This |
|---|---|
| You want fewer trades, less stress | State Machine (Strategy 1) |
| You want more trades, more opportunity | ORB (Strategy 2) |
| You have small account (<$5k) | ORB (more trades = faster learning) |
| You have large account, want preservation | State Machine |
| Both running simultaneously | NOT recommended (same account, same market = over-exposed) |

---

---

# Risk Management Rules

These rules apply to BOTH strategies. They protect your account.

## 1. The 1% Rule
Never risk more than 1% of account per trade.
- $10,000 account → max $100 at risk per trade
- $100,000 account → max $1,000 at risk per trade

## 2. Stop Loss is Non-Negotiable
Every trade has a hard stop loss set on the exchange. The bot doesn't "hope" a losing trade comes back.

## 3. Session Filter
The bot only trades during the **London/NY overlap** (1:00 PM – 6:00 PM UTC). This is when gold moves the most and spreads are tightest.

```
Active hours: ██████████░░░░░░░░░░░░░░
              1pm  3pm  6pm   midnight  1pm
                     UTC
```

## 4. Testnet First
When you connect to Bybit, the bot starts in **TESTNET mode** (fake money). You must manually switch to MAINNET. This prevents accidental real trades.

---

---

# Configuration — The Key Dials

These are the main numbers you can change to tune the strategy.

## Strategy 1 (State Machine)

| Parameter | Default | Meaning | If you increase it... |
|---|---|---|---|
| `LONG_PULLBACK_MAX_CANDLES` | 3 | Red candles needed before box opens | Fewer signals, higher quality |
| `SHORT_PULLBACK_MAX_CANDLES` | 2 | Green candles needed | Fewer short signals |
| `LONG_ENTRY_WINDOW_PERIODS` | 1 | Bars to wait for breakout | More patience, more signals triggered |
| `LONG_ATR_SL_MULT` | 2.5 | Stop loss distance (× ATR) | Wider stop = less stopped out |
| `LONG_ATR_TP_MULT` | 12.0 | Take profit distance (× ATR) | Higher target = fewer wins but bigger |
| `RISK_PERCENT` | 0.01 | 1% risk per trade | More risk = more reward AND more drawdown |

## Strategy 2 (ORB)

| Parameter | Default | Meaning | If you increase it... |
|---|---|---|---|
| `ORB_RANGE_BARS` | 12 | Candles to build opening range (12×5m = 60 min) | Wider range = fewer breakouts |
| `ORB_SL_RANGE_FRAC` | 0.7 | Stop = range × this | Wider stop = survive more noise |
| `ORB_TP_RANGE_MULT` | 2.0 | Target = range × this | Higher target = less won but bigger wins |
| `RISK_PERCENT` | 0.007 | 0.7% risk per trade | Lower than S1 because more trades |

---

---

# Performance Summary

## Strategy 1 (State Machine) — 5-Year Backtest

```
Period:         Jul 2020 – Jul 2025 (5 years)
Starting:       $100,000
Ending:         $144,747
Total Return:   +44.75%
Annual Return:  ~8.95%

Trades:         175 total (~3/month)
Win Rate:       55.4%  (97 wins / 78 losses)
Avg Win:        +$492
Avg Loss:       -$251
Profit Factor:  1.64  (for every $1 lost → earn $1.64)

Max Drawdown:   -5.81%  ← very low for a 5-year period
Sharpe Ratio:   0.892   ← above 0.5 is generally acceptable
```

## Strategy 2 (ORB) — 5-Year Backtest

```
Period:         Jul 2020 – Jul 2025 (5 years)
Risk per trade: 0.7%

Trades:         1,083 total (~18/month)
Win Rate:       42.8%
Expectancy:     +0.051 R per trade
Max Drawdown:   ~15%  ← higher, but still manageable

5-Year Return:  ~+55 R total
                (~38.7% on account at 0.7% risk)
```

---

---

# Live Trading — How to Run the Bot

## Prerequisites
- Python 3.9+
- Bybit account (testnet first)
- Optional: Telegram bot for notifications

## Step 1 — Configure
Create a `.env` file:
```
BYBIT_API_KEY=your_key_here
BYBIT_API_SECRET=your_secret_here
BYBIT_TESTNET=true          ← KEEP THIS TRUE until you're confident
RISK_PERCENT=0.007
ACTIVE_STRATEGY=orb         ← or "state_machine"
TELEGRAM_TOKEN=optional
TELEGRAM_CHAT_ID=optional
```

## Step 2 — Start
```bash
python src/live/bot.py
```

## Step 3 — Monitor via Telegram
Once running, you can send these commands to the bot:
```
/status    → See current position, PnL, equity
/pause     → Stop taking new trades (keeps open positions)
/close     → Emergency close all positions
/set risk 0.005  → Change risk to 0.5% live
```

## Step 4 — Go Live (When Ready)
Change `BYBIT_TESTNET=false` in `.env`. Restart the bot. Real money is now at risk.

---

---

# Glossary — Terms Explained Simply

| Term | Simple Explanation |
|---|---|
| **EMA** | Moving average that reacts faster to recent prices |
| **ATR** | Measures how much price moves on average (volatility gauge) |
| **Crossover** | When a fast line crosses over a slow line on a chart |
| **Pullback** | A brief move in the opposite direction of the trend |
| **Breakout** | Price pushing through a defined level (the "box") |
| **Stop Loss** | Pre-set price where trade closes automatically to limit loss |
| **Take Profit** | Pre-set price where trade closes automatically to lock in gain |
| **R** | 1R = 1 unit of risk. If you risk $100, +3R means +$300 profit |
| **Drawdown** | How far account drops from its peak before recovering |
| **Sharpe Ratio** | Return divided by risk. Higher = better risk-adjusted returns |
| **Profit Factor** | Total wins / Total losses. Above 1.0 = profitable |
| **OCA** | "One Cancels All" — Stop Loss and Take Profit linked; one fills = other cancels |
| **ORB** | Opening Range Breakout — trade the breakout of the first hour's range |
| **Testnet** | Fake exchange for practice — no real money |

---

*Document generated: February 2026*
*Strategy version: ORB v2 (12-bar range, SL=0.7, TP=2.0) + State Machine v4*
