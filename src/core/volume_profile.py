"""Volume Profile Analyzer: VAH/POC/VAL calculation for market extremes.

Calculates Volume Profile from recent candles to identify market extremes
and value areas. Used to filter entries near VAH/VAL extremes and adjust
stop losses/targets based on VP levels.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import structlog

from src.models import (
    OHLCV,
    VolumeProfileConfig,
    VolumeProfileResult,
)

logger = structlog.get_logger(__name__)


class VolumeProfileAnalyzer:
    """Volume Profile analyzer for identifying market extremes and value areas.

    Calculates POC (Point of Control), VAH (Value Area High), and VAL (Value Area Low)
    from volume distribution across price bins. Used to identify extreme price levels
    where volume activity was highest.
    """

    def __init__(self, config: VolumeProfileConfig) -> None:
        """Initialize volume profile analyzer.

        Args:
            config: Volume profile configuration parameters.
        """
        self.config = config
        self._logger = logger.bind(component="VolumeProfileAnalyzer")
        self._cache: dict[str, VolumeProfileResult] = {}

    async def calculate_volume_profile(
        self,
        symbol: str,
        timeframe: str,
        candles: list[OHLCV],
        current_price: float,
    ) -> VolumeProfileResult:
        """Calculate volume profile for given candles and current price.

        Args:
            symbol: Trading symbol.
            timeframe: Timeframe for analysis.
            candles: List of OHLCV candles.
            current_price: Current market price.

        Returns:
            VolumeProfileResult with VAH/POC/VAL and distance calculations.
        """
        if len(candles) < 10:
            self._logger.warning("insufficient_candles_for_vp", symbol=symbol, count=len(candles))
            return self._create_empty_result(symbol, timeframe, current_price)

        # Calculate bin size dynamically
        bin_size = self._calculate_bin_size(candles)
        volume_profile = {}
        total_volume = 0.0

        # Build volume profile
        for candle in candles:
            high = candle.high
            low = candle.low
            volume = candle.volume

            # Distribute volume across price levels within candle
            for i in range(self.config.steps_per_candle):
                price = low + (i / self.config.steps_per_candle) * (high - low)
                vol_portion = volume / self.config.steps_per_candle

                bin_key = round(price / bin_size) * bin_size

                if bin_key not in volume_profile:
                    volume_profile[bin_key] = 0.0
                volume_profile[bin_key] += vol_portion

            total_volume += volume

        # Sort by price and find POC
        vp_array = sorted(
            [(price, vol) for price, vol in volume_profile.items()],
            key=lambda x: x[0]
        )

        if not vp_array:
            return self._create_empty_result(symbol, timeframe, current_price)

        # POC (Point of Control) - highest volume level
        poc_price, poc_vol = max(vp_array, key=lambda x: x[1])

        # Value Area (70% of total volume by default)
        target_volume = total_volume * self.config.value_area_pct

        # Find VAH and VAL by expanding from POC
        vah = poc_price
        val = poc_price
        current_volume = poc_vol

        poc_index = next(i for i, (p, _) in enumerate(vp_array) if p == poc_price)
        left_idx = poc_index
        right_idx = poc_index

        while current_volume < target_volume and (left_idx > 0 or right_idx < len(vp_array) - 1):
            next_left = vp_array[left_idx - 1] if left_idx > 0 else None
            next_right = vp_array[right_idx + 1] if right_idx < len(vp_array) - 1 else None

            if not next_left and not next_right:
                break

            if not next_right or (next_left and next_left[1] > next_right[1]):
                left_idx -= 1
                current_volume += next_left[1]
                val = next_left[0]
            else:
                right_idx += 1
                current_volume += next_right[1]
                vah = next_right[0]

        # Calculate distances and percentages
        result = VolumeProfileResult(
            symbol=symbol,
            timeframe=timeframe,
            poc=poc_price,
            vah=vah,
            val=val,
            total_volume=total_volume,
            bins=len(vp_array),
            current_price=current_price,
            in_value_area=val <= current_price <= vah,
            near_val=self._is_near_extreme(current_price, val),
            near_vah=self._is_near_extreme(current_price, vah),
            near_poc=self._is_near_extreme(current_price, poc_price),
            distance_to_val_pct=abs(current_price - val) / current_price,
            distance_to_vah_pct=abs(current_price - vah) / current_price,
            distance_to_poc_pct=abs(current_price - poc_price) / current_price,
            val_to_poc_pct=abs(val - poc_price) / poc_price,
            poc_to_vah_pct=abs(poc_price - vah) / poc_price,
            val_to_vah_pct=abs(val - vah) / vah if vah != 0 else 0.0,
            calculated_at=datetime.utcnow()
        )

        # Cache result
        cache_key = f"{symbol}:{timeframe}"
        self._cache[cache_key] = result

        self._logger.info(
            "volume_profile_calculated",
            symbol=symbol,
            timeframe=timeframe,
            poc=poc_price,
            vah=vah,
            val=val,
            current_price=current_price,
            in_value_area=result.in_value_area,
            near_extremes=result.near_val or result.near_vah
        )

        return result

    def get_cached_result(self, symbol: str, timeframe: str) -> Optional[VolumeProfileResult]:
        """Get cached volume profile result.

        Args:
            symbol: Trading symbol.
            timeframe: Timeframe.

        Returns:
            Cached VolumeProfileResult or None if not cached.
        """
        cache_key = f"{symbol}:{timeframe}"
        return self._cache.get(cache_key)

    def _calculate_bin_size(self, candles: list[OHLCV]) -> float:
        """Calculate dynamic bin size based on price volatility.

        Args:
            candles: List of candles to analyze.

        Returns:
            Bin size for volume distribution.
        """
        if not candles:
            return 1.0

        # Use average true range as reference
        prices = [c.close for c in candles]
        if len(prices) < 2:
            return abs(prices[0] * self.config.bin_size_pct)

        # Simple ATR approximation
        ranges = [abs(high - low) for high, low in zip([c.high for c in candles], [c.low for c in candles])]
        avg_range = sum(ranges) / len(ranges) if ranges else 1.0

        # Bin size as percentage of average range
        bin_size = avg_range * self.config.bin_size_pct

        # Ensure minimum bin size
        min_bin_size = prices[-1] * 0.0001  # 0.01% of current price
        return max(bin_size, min_bin_size)

    def _is_near_extreme(self, current_price: float, extreme_price: float) -> bool:
        """Check if current price is near an extreme level.

        Args:
            current_price: Current market price.
            extreme_price: VAH/VAL/POC price level.

        Returns:
            True if within max_distance_from_extreme_pct.
        """
        distance_pct = abs(current_price - extreme_price) / current_price
        return distance_pct <= self.config.max_distance_from_extreme_pct

    def _create_empty_result(self, symbol: str, timeframe: str, current_price: float) -> VolumeProfileResult:
        """Create empty volume profile result for error cases.

        Args:
            symbol: Trading symbol.
            timeframe: Timeframe.
            current_price: Current price.

        Returns:
            Empty VolumeProfileResult with default values.
        """
        return VolumeProfileResult(
            symbol=symbol,
            timeframe=timeframe,
            poc=current_price,
            vah=current_price,
            val=current_price,
            total_volume=0.0,
            bins=0,
            current_price=current_price,
            in_value_area=False,
            near_val=False,
            near_vah=False,
            near_poc=False,
            distance_to_val_pct=0.0,
            distance_to_vah_pct=0.0,
            distance_to_poc_pct=0.0,
            val_to_poc_pct=0.0,
            poc_to_vah_pct=0.0,
            val_to_vah_pct=0.0,
            calculated_at=datetime.utcnow()
        )

    def should_trade_extremes_only(self, vp_result: VolumeProfileResult) -> bool:
        """Check if current setup allows trading only near extremes.

        Args:
            vp_result: Volume profile analysis result.

        Returns:
            True if position is near VAH, VAL, or POC.
        """
        if not self.config.extremes_only:
            return True

        return vp_result.near_val or vp_result.near_vah or vp_result.near_poc

    def calculate_dynamic_sl_tp(
        self,
        vp_result: VolumeProfileResult,
        entry_price: float,
        side: str
    ) -> tuple[float, float]:
        """Calculate dynamic stop loss and take profit based on VP levels.

        Args:
            vp_result: Volume profile result.
            entry_price: Entry price.
            side: 'long' or 'short'.

        Returns:
            Tuple of (stop_loss, take_profit) prices.
        """
        if side.lower() == "long":
            # Stop below VAL or use percentage-based SL
            sl = min(vp_result.val, entry_price * 0.98)
            # Target VAH or use risk multiple
            tp = max(vp_result.vah, entry_price * (1 + vp_result.val_to_vah_pct * 2))
        else:  # short
            # Stop above VAH or use percentage-based SL
            sl = max(vp_result.vah, entry_price * 1.02)
            # Target VAL or use risk multiple
            tp = min(vp_result.val, entry_price * (1 - vp_result.val_to_vah_pct * 2))

        return round(sl, 8), round(tp, 8)