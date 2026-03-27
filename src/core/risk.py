"""Risk Controller: Position sizing and dynamic leverage management.

Enforces the 1% risk per trade rule and dynamically adjusts
leverage (6x-12x) based on ATR-derived volatility.
"""

from __future__ import annotations

import math
from typing import Optional

import structlog

from src.models import (
    PositionSizeResult,
    RiskConfig,
    StrategyConfig,
)

logger = structlog.get_logger(__name__)


class RiskController:
    """Position sizing and dynamic leverage calculator.

    Responsibilities:
        - Calculate position size based on account balance, stop loss distance,
          and risk per trade percentage.
        - Dynamically adjust leverage between 6x-12x based on ATR volatility.
        - Validate that position doesn't exceed max_position_pct of balance.

    Attributes:
        risk_config: Risk parameters (stop loss %, risk per trade, etc.).
        strategy_config: Strategy parameters (ATR period).
    """

    def __init__(
        self,
        risk_config: RiskConfig,
        strategy_config: StrategyConfig,
    ) -> None:
        """Initialize risk controller.

        Args:
            risk_config: Risk management parameters.
            strategy_config: Strategy parameters (used for ATR reference).
        """
        self.risk_config = risk_config
        self.strategy_config = strategy_config
        self._logger = logger.bind(component="RiskController")

    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss_price: float,
        account_balance: float,
        leverage: int = 1,
    ) -> PositionSizeResult:
        """Calculate optimal position size based on risk parameters.

        Formula:
            risk_amount = account_balance * (risk_per_trade_pct / 100)
            stop_distance = |entry_price - stop_loss_price| / entry_price
            position_value = risk_amount / stop_distance
            position_amount = position_value / entry_price

        Enforces:
            - Position value <= max_position_pct of account balance
            - Position amount is positive and finite

        Args:
            symbol: Trading symbol.
            entry_price: Expected entry price.
            stop_loss_price: Stop loss price.
            account_balance: Available USDT balance.
            leverage: Current leverage setting.

        Returns:
            PositionSizeResult with recommended and max amounts.
        """
        risk_amount = account_balance * (self.risk_config.risk_per_trade_pct / 100.0)

        stop_distance_pct = abs(entry_price - stop_loss_price) / entry_price

        if stop_distance_pct == 0:
            self._logger.warning(
                "stop_distance_is_zero",
                symbol=symbol,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
            )
            return PositionSizeResult(
                symbol=symbol,
                recommended_amount=0.0,
                max_amount=0.0,
                risk_amount_usdt=risk_amount,
                stop_distance_pct=0.0,
                leverage=leverage,
            )

        position_value = risk_amount / stop_distance_pct
        recommended_amount = position_value / entry_price

        max_position_value = account_balance * (self.risk_config.max_position_pct / 100.0)
        max_amount = max_position_value / entry_price

        if position_value > max_position_value:
            self._logger.info(
                "position_capped_by_max_pct",
                symbol=symbol,
                original_value=position_value,
                capped_value=max_position_value,
            )
            recommended_amount = max_amount

        if not math.isfinite(recommended_amount) or recommended_amount <= 0:
            self._logger.warning(
                "invalid_position_amount",
                symbol=symbol,
                recommended_amount=recommended_amount,
            )
            return PositionSizeResult(
                symbol=symbol,
                recommended_amount=0.0,
                max_amount=max_amount,
                risk_amount_usdt=risk_amount,
                stop_distance_pct=stop_distance_pct,
                leverage=leverage,
            )

        self._logger.info(
            "position_size_calculated",
            symbol=symbol,
            recommended_amount=recommended_amount,
            max_amount=max_amount,
            risk_amount=risk_amount,
            stop_distance_pct=stop_distance_pct,
        )

        return PositionSizeResult(
            symbol=symbol,
            recommended_amount=recommended_amount,
            max_amount=max_amount,
            risk_amount_usdt=risk_amount,
            stop_distance_pct=stop_distance_pct,
            leverage=leverage,
        )

    def dynamic_leverage(self, atr: float, current_price: float) -> int:
        """Calculate dynamic leverage based on ATR volatility.

        Higher volatility (larger ATR relative to price) → lower leverage.
        Lower volatility → higher leverage.

        Mapping (ATR as % of price):
            - ATR% >= 2.0%  → 6x  (high volatility)
            - ATR% >= 1.5%  → 7x
            - ATR% >= 1.0%  → 8x
            - ATR% >= 0.7%  → 9x
            - ATR% >= 0.5%  → 10x
            - ATR% >= 0.3%  → 11x
            - ATR% <  0.3%  → 12x (low volatility)

        Args:
            atr: Average True Range value.
            current_price: Current asset price.

        Returns:
            Leverage multiplier (6-12).
        """
        if current_price <= 0:
            self._logger.warning(
                "invalid_price_for_leverage",
                current_price=current_price,
            )
            return 6

        atr_pct = (atr / current_price) * 100.0

        if atr_pct >= 2.0:
            leverage = 6
        elif atr_pct >= 1.5:
            leverage = 7
        elif atr_pct >= 1.0:
            leverage = 8
        elif atr_pct >= 0.7:
            leverage = 9
        elif atr_pct >= 0.5:
            leverage = 10
        elif atr_pct >= 0.3:
            leverage = 11
        else:
            leverage = 12

        self._logger.debug(
            "dynamic_leverage_calculated",
            atr=atr,
            current_price=current_price,
            atr_pct=atr_pct,
            leverage=leverage,
        )

        return leverage

    def validate_position(
        self,
        position_value: float,
        account_balance: float,
        leverage: int,
    ) -> bool:
        """Validate that a position doesn't exceed risk limits.

        Checks:
            - Position margin (value/leverage) <= max_position_pct of balance
            - Position value is positive and finite

        Args:
            position_value: Total position value in USDT.
            account_balance: Available USDT balance.
            leverage: Current leverage.

        Returns:
            True if position is within risk limits.
        """
        if not math.isfinite(position_value) or position_value <= 0:
            self._logger.warning(
                "invalid_position_value",
                position_value=position_value,
            )
            return False

        if account_balance <= 0:
            self._logger.warning(
                "invalid_account_balance",
                account_balance=account_balance,
            )
            return False

        margin_required = position_value / leverage
        max_margin = account_balance * (self.risk_config.max_position_pct / 100.0)

        if margin_required > max_margin:
            self._logger.warning(
                "position_exceeds_max_pct",
                margin_required=margin_required,
                max_margin=max_margin,
                leverage=leverage,
            )
            return False

        return True

    def calculate_stop_distance(
        self,
        entry_price: float,
        stop_loss_pct: Optional[float] = None,
    ) -> float:
        """Calculate stop loss distance in absolute price terms.

        Uses stop_loss_pct from config if not provided.

        Args:
            entry_price: Entry price.
            stop_loss_pct: Optional override for stop loss percentage.

        Returns:
            Absolute price distance for stop loss.
        """
        pct = stop_loss_pct if stop_loss_pct is not None else self.risk_config.stop_loss_pct
        return entry_price * (pct / 100.0)

    def calculate_trailing_stop(
        self,
        entry_price: float,
        current_price: float,
        side: str,
        trailing_pct: Optional[float] = None,
    ) -> float:
        """Calculate trailing stop price.

        For LONG: trailing_stop = current_price * (1 - trailing_pct/100)
        For SHORT: trailing_stop = current_price * (1 + trailing_pct/100)

        The trailing stop only moves in the favorable direction.
        If current trailing stop is already better, keep it.

        Args:
            entry_price: Original entry price.
            current_price: Current market price.
            side: 'long' or 'short'.
            trailing_pct: Optional override for trailing stop percentage.

        Returns:
            New trailing stop price.
        """
        pct = trailing_pct if trailing_pct is not None else self.risk_config.trailing_stop_pct
        side_normalized = side.lower()

        if side_normalized == "long":
            # For long, trailing stop is below current price and should only move up
            new_stop = current_price * (1.0 - pct / 100.0)
            # Initial stop is based on entry price
            initial_stop = entry_price * (1.0 - self.risk_config.stop_loss_pct / 100.0)
            return max(new_stop, initial_stop)
        elif side_normalized == "short":
            # For short, trailing stop is above current price and should only move down
            new_stop = current_price * (1.0 + pct / 100.0)
            # Initial stop is based on entry price
            initial_stop = entry_price * (1.0 + self.risk_config.stop_loss_pct / 100.0)
            return min(new_stop, initial_stop)
        else:
            self._logger.warning("invalid_side_for_trailing_stop", side=side)
            return entry_price
