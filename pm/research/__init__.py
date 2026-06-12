"""Research-grade statistical analysis of the signal_log.

Kept separate from `pm.signals` and `pm.backtest` because this module is
post-hoc analysis on persisted data, not part of the live engine. Pure
stdlib — no numpy / scipy — for the same reason `pm.core` is: the engine
runtime stays light. The cost is some loops where vectorised would be
nicer; the data is hundreds-to-thousands of points, not millions.
"""
