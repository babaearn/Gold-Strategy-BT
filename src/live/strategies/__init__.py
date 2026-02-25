"""
strategies/__init__.py — Strategy registry
==========================================
Maps strategy numbers to their state-machine classes.

Adding a new strategy
---------------------
1. Create  src/live/strategies/strategy_2.py  with a class that has the same
   interface as StateMachine (process_bar / reset / state).
2. Import it here and add it to REGISTRY and NAMES.
3. Restart the bot or send /set 2 via Telegram.

Switching strategies at runtime
--------------------------------
  /set 1   → Volatility Expansion Channel (current strategy)
  /set 2   → (add your next strategy here)
  /set n   → ...

The switch takes effect on the next closed candle, and only when there is no
open position.
"""

from state_machine import StateMachine   # Strategy 1 — 4-Phase Volatility Expansion Channel
from strategies.orb import ORBStrategy   # Strategy 2 — Opening Range Breakout

# ── Registry ──────────────────────────────────────────────────────────────────

#: Maps strategy number → state-machine class.
#: Classes must accept the same kwargs as StateMachine.__init__.
REGISTRY: dict[int, type] = {
    1: StateMachine,
    2: ORBStrategy,
}

#: Human-readable names shown in Telegram messages.
NAMES: dict[int, str] = {
    1: "Volatility Expansion Channel (4-Phase)",
    2: "Opening Range Breakout (London Session)",
}


# ── Factory ───────────────────────────────────────────────────────────────────

def get_strategy(n: int, **kwargs):
    """
    Instantiate strategy *n* with the given keyword arguments.

    Raises ValueError if *n* is not in the registry.
    """
    cls = REGISTRY.get(n)
    if cls is None:
        available = list(REGISTRY.keys())
        raise ValueError(f"Strategy {n} not in registry. Available: {available}")
    return cls(**kwargs)


def strategy_name(n: int) -> str:
    """Return the human-readable name for strategy *n*."""
    return NAMES.get(n, f"Strategy {n}")


def available_strategies() -> list[tuple[int, str]]:
    """Return sorted list of (number, name) tuples."""
    return sorted((k, NAMES.get(k, f"Strategy {k}")) for k in REGISTRY)
