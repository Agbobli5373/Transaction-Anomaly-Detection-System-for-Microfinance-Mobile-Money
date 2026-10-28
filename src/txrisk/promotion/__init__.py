"""The gates a Challenger model passes before an Admin may promote it (`config/promotion.toml`)."""

from txrisk.promotion.config import PromotionConfig, load_promotion_config
from txrisk.promotion.gates import (
    BacktestReport,
    GateReport,
    ShadowReport,
    backtest_gate,
    check_gates,
    matured_window,
    shadow_gate,
)

__all__ = [
    "BacktestReport",
    "GateReport",
    "PromotionConfig",
    "ShadowReport",
    "backtest_gate",
    "check_gates",
    "load_promotion_config",
    "matured_window",
    "shadow_gate",
]
