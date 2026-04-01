"""Daily Timeframe Bias Manager.

Fetches 1-day candles and calculates the Ichimoku-based directional bias:
- BULLISH: Price > Daily Kijun-sen
- BEARISH: Price < Daily Kijun-sen
- IN_CLOUD: Price between Senkou Span A and Senkou Span B
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from src.models import DailyBiasDirection, DailyBiasResult

if TYPE_CHECKING:
    from src.core.exchange import ExchangeInterface
    from src.models import DailyBiasConfig


logger = structlog.get_logger(__name__)


class DailyBiasManager:
    """Manages daily timeframe (1d) bias detection for MTF strategy."""

    def __init__(
        self,
        config: DailyBiasConfig,
        exchange: ExchangeInterface,
    ) -> None:
        self._config = config
        self._exchange = exchange
        self._logger = logger.bind(component="DailyBiasManager")
        self._cache: dict[str, DailyBiasResult] = {}

    async def calculate_bias(self, symbol: str) -> DailyBiasResult:
        """Fetch daily OHLCV and calculate directional bias.

        Args:
            symbol: Symbol in CCXT format.

        Returns:
            DailyBiasResult containing direction and multipliers.
        """
        # 52 + 26 + 26 (lookback for cloud + displacement)
        limit = 120 
        
        try:
            candles = await self._exchange.fetch_ohlcv(
                symbol=symbol, timeframe="1d", limit=limit
            )
            
            if len(candles) < 78:
                self._logger.warning("insufficient_candles", symbol=symbol, count=len(candles))
                return self._get_neutral_bias()

            # Convert to lists for easier slicing
            closes = [c.close for c in candles]
            highs = [c.high for c in candles]
            lows = [c.low for c in candles]
            
            current_price = closes[-1]
            
            # 1. Kijun-sen (Base Line): (26-period high + 26-period low) / 2
            kj_period = self._config.kijun_period
            kj_high = max(highs[-kj_period:])
            kj_low = min(lows[-kj_period:])
            kijun = (kj_high + kj_low) / 2
            
            # 2. Daily Cloud (Kumo)
            # Senkou Span A = (9-period + 26-period) / 2, shifted 26 periods ahead.
            # Value AT CURRENT CANDLE (index -1) requires Tenkan/Kijun from 26 candles ago (index -27)
            
            idx_shifted = -27 # 26 periods ago
            
            # Tenkan at t-26
            t_high_26 = max(highs[idx_shifted-9 : idx_shifted])
            t_low_26 = min(lows[idx_shifted-9 : idx_shifted])
            tenkan_26 = (t_high_26 + t_low_26) / 2
            
            # Kijun at t-26
            k_high_26 = max(highs[idx_shifted-26 : idx_shifted])
            k_low_26 = min(lows[idx_shifted-26 : idx_shifted])
            kijun_26 = (k_high_26 + k_low_26) / 2
            
            senkou_a = (tenkan_26 + kijun_26) / 2
            
            # Senkou Span B at t-26: (52-period high + 52-period low) / 2, shifted 26 periods ahead.
            sb_high_26 = max(highs[idx_shifted-52 : idx_shifted])
            sb_low_26 = min(lows[idx_shifted-52 : idx_shifted])
            senkou_b = (sb_high_26 + sb_low_26) / 2
            
            cloud_top = max(senkou_a, senkou_b)
            cloud_bottom = min(senkou_a, senkou_b)
            
            # Determine Direction
            direction = DailyBiasDirection.NEUTRAL
            size_multiplier = 1.0
            
            # Kijun filter (Primary Bias)
            if current_price > kijun:
                direction = DailyBiasDirection.BULLISH
            elif current_price < kijun:
                direction = DailyBiasDirection.BEARISH
                
            # Cloud filter (Size Reducer)
            if cloud_bottom <= current_price <= cloud_top:
                # We are in the cloud. We don't flip neutral, just reduce the size
                # but let's keep the Kijun direction for entry side.
                size_multiplier = self._config.inside_cloud_size_factor
                
            result = DailyBiasResult(
                direction=direction,
                size_multiplier=size_multiplier,
                kijun=kijun,
                cloud_top=cloud_top,
                cloud_bottom=cloud_bottom,
                updated_at=datetime.utcnow(),
            )
            
            self._cache[symbol] = result
            self._logger.info(
                "bias_calculated",
                symbol=symbol,
                direction=direction.value,
                in_cloud=size_multiplier < 1.0,
                price=current_price,
                kijun=kijun,
            )
            return result
            
        except Exception as e:
            self._logger.error("bias_calculation_failed", symbol=symbol, error=str(e))
            # If we have a cached version, use it instead of returning neutral
            if symbol in self._cache:
                self._logger.info("using_cached_bias", symbol=symbol)
                return self._cache[symbol]
            return self._get_neutral_bias()

    def _get_neutral_bias(self) -> DailyBiasResult:
        return DailyBiasResult(
            direction=DailyBiasDirection.NEUTRAL,
            size_multiplier=0.0,
            kijun=0.0,
            cloud_top=0.0,
            cloud_bottom=0.0,
            updated_at=datetime.utcnow(),
        )

    def get_last_bias(self, symbol: str) -> Optional[DailyBiasResult]:
        """Get the last calculated bias from cache."""
        return self._cache.get(symbol)
