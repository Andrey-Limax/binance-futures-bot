"""Strategy Engine: Ichimoku Cloud + MACD/RSI Composite Signal.

Combines Ichimoku Cloud trend analysis with MACD momentum and RSI
overbought/oversold filtering to generate trading signals.

Warm-up: The engine requires 100+ historical candles to populate
Ichimoku's Senkou Span B (52-period). Until warm-up is complete,
the engine returns NEUTRAL signals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd
import pandas_ta_classic as ta
import structlog

from src.models import (
    CandleSeries,
    OHLCV,
    Signal,
    StrategyConfig,
    StrategySignal,
)

logger = structlog.get_logger(__name__)


class StrategyEngine:
    """Ichimoku + MACD/RSI composite strategy engine.

    Processes OHLCV data to calculate technical indicators and
    generates composite trading signals.

    Signal Logic:
        1. Ichimoku Cloud: Primary trend direction
           - Price above cloud = bullish bias
           - Price below cloud = bearish bias
           - Tenkan > Kijun = bullish momentum
           - Tenkan < Kijun = bearish momentum

        2. MACD: Momentum confirmation
           - MACD line above signal = bullish momentum
           - MACD line below signal = bearish momentum
           - Histogram increasing = strengthening trend

        3. RSI: Overbought/Oversold filter
           - RSI < 30 = oversold (potential buy)
           - RSI > 70 = overbought (potential sell)
           - RSI 30-70 = neutral zone

        4. Composite Signal:
           - All 3 bullish = STRONG_BUY
           - 2/3 bullish = BUY
           - All 3 bearish = STRONG_SELL
           - 2/3 bearish = SELL
           - Mixed/insufficient = NEUTRAL

    Attributes:
        config: Strategy parameters (periods, thresholds).
        _candle_cache: In-memory OHLCV data per symbol.
        _is_warmed_up: Whether warm-up candles have been loaded.
    """

    # Column name prefixes produced by pandas-ta-classic
    _ICHIMOKU_PREFIX = "ICHIMOKU"
    _MACD_PREFIX = "MACD"
    _RSI_PREFIX = "RSI"
    _ATR_PREFIX = "ATR"

    def __init__(self, config: StrategyConfig) -> None:
        """Initialize strategy engine with indicator parameters.

        Args:
            config: Strategy configuration with indicator periods.
        """
        self.config = config
        self._logger = logger.bind(component="StrategyEngine")
        self._candle_cache: dict[str, pd.DataFrame] = {}
        self._is_warmed_up: dict[str, bool] = {}

    # ── Public API ─────────────────────────────────────────────────────────

    async def warm_up(
        self,
        symbol: str,
        candles: list[OHLCV],
    ) -> bool:
        """Populate indicators with historical candle data.

        Downloads and stores historical OHLCV data to ensure indicators
        (especially Ichimoku Senkou Span B) have enough data points.

        Args:
            symbol: Trading symbol to warm up.
            candles: List of OHLCV candles (oldest to newest).

        Returns:
            True if warm-up was successful (enough data), False otherwise.
        """
        if len(candles) < self.config.warmup_candles:
            self._logger.warning(
                "insufficient_candles_for_warmup",
                symbol=symbol,
                provided=len(candles),
                required=self.config.warmup_candles,
            )
            self._is_warmed_up[symbol] = False
            return False

        df = self._ohlcv_list_to_df(candles)
        self._candle_cache[symbol] = df
        self._is_warmed_up[symbol] = True

        self._logger.info(
            "warmup_complete",
            symbol=symbol,
            candles_loaded=len(df),
        )
        return True

    def update_candle(self, symbol: str, candle: OHLCV, timeframe: str) -> None:
        """Update the last candle or append a new one based on timeframe.

        If the incoming candle belongs to the same time bucket as the last
        cached candle, it updates the High, Low, Close, and Volume of that
        candle. Otherwise, it appends a new candle bucket.

        Args:
            symbol: Trading symbol.
            candle: New OHLCV data point (ticker).
            timeframe: CCXT timeframe string (e.g., '1m', '15m', '1h').
        """
        if symbol not in self._candle_cache:
            self._logger.warning("update_before_warmup", symbol=symbol)
            return

        df = self._candle_cache[symbol]
        if df.empty:
            return

        # Convert CCXT timeframe (1m, 1h) to pandas frequency (1min, 1H)
        freq = timeframe.replace("m", "min").replace("h", "H").replace("d", "D")
        
        # Determine buckets
        last_ts = pd.to_datetime(df["timestamp"].iloc[-1])
        new_ts = pd.to_datetime(candle.timestamp)
        
        # Floor timestamps to the start of their timeframe bucket
        last_bucket = last_ts.floor(freq)
        new_bucket = new_ts.floor(freq)

        if new_bucket == last_bucket:
            # Update current candle (same bucket)
            idx = df.index[-1]
            df.at[idx, "high"] = max(df.at[idx, "high"], candle.high)
            df.at[idx, "low"] = min(df.at[idx, "low"], candle.low)
            df.at[idx, "close"] = candle.close
            df.at[idx, "volume"] = df.at[idx, "volume"] + candle.volume # Simplified
        else:
            # Append new candle (new bucket)
            new_row = self._ohlcv_list_to_df([candle])
            # Ensure the new row's timestamp is floored to the bucket start
            new_row.at[0, "timestamp"] = new_bucket
            
            self._candle_cache[symbol] = pd.concat(
                [df, new_row],
                ignore_index=True,
            )

            # Keep a rolling window: warmup + 50 candle buffer
            max_len = self.config.warmup_candles + 50
            if len(self._candle_cache[symbol]) > max_len:
                self._candle_cache[symbol] = self._candle_cache[symbol].iloc[-max_len:].reset_index(drop=True)

    def generate_signal(self, symbol: str) -> StrategySignal:
        """Generate a composite trading signal for a symbol.

        Calculates Ichimoku, MACD, and RSI on the cached candle data
        and combines them into a single StrategySignal.

        Returns NEUTRAL with confidence=0 if:
            - Warm-up is not complete
            - Insufficient data for indicators
            - Indicators produce NaN values

        Args:
            symbol: Trading symbol to analyze.

        Returns:
            StrategySignal with direction, confidence, and price levels.
        """
        now = datetime.utcnow()

        # Guard: warm-up must be complete
        if not self._is_warmed_up.get(symbol, False):
            self._logger.warning("signal_before_warmup", symbol=symbol)
            return self._neutral_signal(symbol, now)

        df = self._candle_cache.get(symbol)
        if df is None or len(df) < self.config.warmup_candles:
            return self._neutral_signal(symbol, now)

        # Work on a copy to avoid polluting the cache
        df = df.copy()

        # Calculate all indicators
        df = self._calculate_ichimoku(df)
        df = self._calculate_macd(df)
        df = self._calculate_rsi(df)
        df = self._calculate_atr(df)

        # Drop rows where any indicator is NaN (warm-up rows)
        indicator_cols = [c for c in df.columns if any(
            c.startswith(p) for p in (
                self._ICHIMOKU_PREFIX, self._MACD_PREFIX,
                self._RSI_PREFIX, self._ATR_PREFIX,
            )
        )]
        df_valid = df.dropna(subset=indicator_cols)

        if df_valid.empty:
            return self._neutral_signal(symbol, now)

        # Interpret each indicator
        ichimoku_signal = self._interpret_ichimoku(df_valid)
        macd_signal = self._interpret_macd(df_valid)
        rsi_signal = self._interpret_rsi(df_valid)

        # Composite signal
        signal, confidence = self._compose_signal(
            ichimoku_signal, macd_signal, rsi_signal,
        )

        last_row = df_valid.iloc[-1]
        entry_price = float(last_row["close"])

        if signal == Signal.NEUTRAL:
            return StrategySignal(
                symbol=symbol,
                signal=Signal.NEUTRAL,
                confidence=confidence,
                entry_price=entry_price,
                stop_loss=0.0,
                take_profit=0.0,
                timestamp=now,
                metadata={
                    "ichimoku": ichimoku_signal.value,
                    "macd": macd_signal.value,
                    "rsi": rsi_signal.value,
                },
            )

        stop_loss = self._calculate_stop_loss(df_valid, signal)
        take_profit = self._calculate_take_profit(entry_price, stop_loss)

        return StrategySignal(
            symbol=symbol,
            signal=signal,
            confidence=confidence,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            timestamp=now,
            metadata={
                "ichimoku": ichimoku_signal.value,
                "macd": macd_signal.value,
                "rsi": rsi_signal.value,
            },
        )

    # ── Indicator Calculations ─────────────────────────────────────────────

    def _calculate_ichimoku(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Ichimoku Cloud components.

        Adds columns to DataFrame:
            - ICHIMOKU_TENKAN_{tenkan}: Conversion line
            - ICHIMOKU_KIJUN_{kijun}: Base line
            - ICHIMOKU_SENKOU_A_{tenkan}: Leading span A
            - ICHIMOKU_SENKOU_B_{senkou}: Leading span B
            - ICHIMOKU_CHIKOU_{kijun}: Lagging span

        Args:
            df: OHLCV DataFrame with 'high', 'low', 'close' columns.

        Returns:
            DataFrame with Ichimoku columns added.
        """
        tenkan = self.config.ichimoku_tenkan
        kijun = self.config.ichimoku_kijun
        senkou = self.config.ichimoku_senkou

        ichimoku_df, span_ahead_df = df.ta.ichimoku(
            tenkan=tenkan,
            kijun=kijun,
            senkou=senkou,
        )

        # ichimoku_df contains: ITS_{tenkan}, IKS_{kijun}, ISA_{tenkan}, ISB_{senkou}, ICS_{kijun}
        # Rename to a consistent prefix for downstream logic
        rename_map = {}
        for col in ichimoku_df.columns:
            if col.startswith("ITS_"):
                rename_map[col] = f"{self._ICHIMOKU_PREFIX}_TENKAN_{tenkan}"
            elif col.startswith("IKS_"):
                rename_map[col] = f"{self._ICHIMOKU_PREFIX}_KIJUN_{kijun}"
            elif col.startswith("ISA_"):
                rename_map[col] = f"{self._ICHIMOKU_PREFIX}_SENKOU_A_{tenkan}"
            elif col.startswith("ISB_"):
                rename_map[col] = f"{self._ICHIMOKU_PREFIX}_SENKOU_B_{senkou}"
            elif col.startswith("ICS_"):
                rename_map[col] = f"{self._ICHIMOKU_PREFIX}_CHIKOU_{kijun}"

        ichimoku_df = ichimoku_df.rename(columns=rename_map)
        df = pd.concat([df, ichimoku_df], axis=1)
        return df

    def _calculate_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate MACD histogram and signal line.

        Adds columns to DataFrame:
            - MACD_{fast}_{slow}_{signal}: MACD line
            - MACDh_{fast}_{slow}_{signal}: Histogram
            - MACDs_{fast}_{slow}_{signal}: Signal line

        Args:
            df: OHLCV DataFrame with 'close' column.

        Returns:
            DataFrame with MACD columns added.
        """
        fast = self.config.macd_fast
        slow = self.config.macd_slow
        signal = self.config.macd_signal

        macd_df = df.ta.macd(fast=fast, slow=slow, signal=signal)

        if macd_df is not None and not macd_df.empty:
            # pandas-ta returns: MACD_{f}_{s}_{sig}, MACDh_{f}_{s}_{sig}, MACDs_{f}_{s}_{sig}
            rename_map = {}
            for col in macd_df.columns:
                if col.startswith("MACD_") and "h_" not in col and "s_" not in col:
                    rename_map[col] = f"{self._MACD_PREFIX}_LINE"
                elif col.startswith("MACDh_"):
                    rename_map[col] = f"{self._MACD_PREFIX}_HISTOGRAM"
                elif col.startswith("MACDs_"):
                    rename_map[col] = f"{self._MACD_PREFIX}_SIGNAL"

            macd_df = macd_df.rename(columns=rename_map)
            df = pd.concat([df, macd_df], axis=1)

        return df

    def _calculate_rsi(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Relative Strength Index.

        Adds column to DataFrame:
            - RSI_{period}: RSI value (0-100)

        Args:
            df: OHLCV DataFrame with 'close' column.

        Returns:
            DataFrame with RSI column added.
        """
        period = self.config.rsi_period
        rsi_series = df.ta.rsi(length=period)

        if rsi_series is not None:
            col_name = f"{self._RSI_PREFIX}_{period}"
            df[col_name] = rsi_series

        return df

    def _calculate_atr(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Average True Range for volatility measurement.

        Adds column to DataFrame:
            - ATR_{period}: ATR value

        Args:
            df: OHLCV DataFrame with 'high', 'low', 'close' columns.

        Returns:
            DataFrame with ATR column added.
        """
        period = self.config.atr_period
        atr_series = df.ta.atr(length=period)

        if atr_series is not None:
            col_name = f"{self._ATR_PREFIX}_{period}"
            df[col_name] = atr_series

        return df

    # ── Signal Interpretation ──────────────────────────────────────────────

    def _interpret_ichimoku(self, df: pd.DataFrame) -> Signal:
        """Interpret Ichimoku Cloud for trend direction.

        Logic:
            - Price above cloud + Tenkan > Kijun → BUY
            - Price below cloud + Tenkan < Kijun → SELL
            - Price inside cloud → NEUTRAL

        Args:
            df: DataFrame with Ichimoku columns.

        Returns:
            Signal based on Ichimoku analysis.
        """
        tenkan = self.config.ichimoku_tenkan
        kijun = self.config.ichimoku_kijun
        senkou = self.config.ichimoku_senkou

        tenkan_col = f"{self._ICHIMOKU_PREFIX}_TENKAN_{tenkan}"
        kijun_col = f"{self._ICHIMOKU_PREFIX}_KIJUN_{kijun}"
        span_a_col = f"{self._ICHIMOKU_PREFIX}_SENKOU_A_{tenkan}"
        span_b_col = f"{self._ICHIMOKU_PREFIX}_SENKOU_B_{senkou}"

        last = df.iloc[-1]
        close = float(last["close"])
        tenkan_val = float(last[tenkan_col])
        kijun_val = float(last[kijun_col])
        span_a_val = float(last[span_a_col])
        span_b_val = float(last[span_b_col])

        cloud_top = max(span_a_val, span_b_val)
        cloud_bottom = min(span_a_val, span_b_val)

        if pd.isna(tenkan_val) or pd.isna(kijun_val) or pd.isna(span_a_val) or pd.isna(span_b_val):
            return Signal.NEUTRAL

        # Price above cloud and tenkan > kijun
        if close > cloud_top and tenkan_val > kijun_val:
            return Signal.BUY

        # Price below cloud and tenkan < kijun
        if close < cloud_bottom and tenkan_val < kijun_val:
            return Signal.SELL

        return Signal.NEUTRAL

    def _interpret_macd(self, df: pd.DataFrame) -> Signal:
        """Interpret MACD for momentum direction.

        Logic:
            - MACD > Signal + Histogram > 0 → BUY
            - MACD < Signal + Histogram < 0 → SELL
            - Otherwise → NEUTRAL

        Args:
            df: DataFrame with MACD columns.

        Returns:
            Signal based on MACD analysis.
        """
        last = df.iloc[-1]
        macd_line = float(last[f"{self._MACD_PREFIX}_LINE"])
        macd_signal = float(last[f"{self._MACD_PREFIX}_SIGNAL"])
        histogram = float(last[f"{self._MACD_PREFIX}_HISTOGRAM"])

        if pd.isna(macd_line) or pd.isna(macd_signal) or pd.isna(histogram):
            return Signal.NEUTRAL

        if macd_line > macd_signal and histogram > 0:
            return Signal.BUY

        if macd_line < macd_signal and histogram < 0:
            return Signal.SELL

        return Signal.NEUTRAL

    def _interpret_rsi(self, df: pd.DataFrame) -> Signal:
        """Interpret RSI for trend confirmation (Option A).

        Logic:
            - 50 < RSI < overbought_threshold → BUY (bullish momentum confirmed)
            - oversold_threshold < RSI < 50 → SELL (bearish momentum confirmed)
            - Otherwise → NEUTRAL

        Args:
            df: DataFrame with RSI column.

        Returns:
            Signal based on RSI analysis.
        """
        rsi_col = f"{self._RSI_PREFIX}_{self.config.rsi_period}"
        last = df.iloc[-1]
        rsi_val = float(last[rsi_col])

        if pd.isna(rsi_val):
            return Signal.NEUTRAL

        # Confirmação de tendência com limite para sobrecomprado/sobrevendido
        if 50 < rsi_val < self.config.rsi_overbought:
            return Signal.BUY

        if self.config.rsi_oversold < rsi_val < 50:
            return Signal.SELL

        return Signal.NEUTRAL

    def _calculate_stop_loss(
        self,
        df: pd.DataFrame,
        side: Signal,
    ) -> float:
        """Calculate stop loss level based on Kijun-sen or ATR.

        Uses the Kijun-sen (base line) as dynamic support/resistance,
        with ATR-based buffer for additional protection.

        Args:
            df: DataFrame with Ichimoku and ATR columns.
            side: Trade direction (BUY/SELL).

        Returns:
            Stop loss price level.
        """
        last = df.iloc[-1]
        kijun_col = f"{self._ICHIMOKU_PREFIX}_KIJUN_{self.config.ichimoku_kijun}"
        atr_col = f"{self._ATR_PREFIX}_{self.config.atr_period}"

        kijun_val = float(last[kijun_col])
        atr_val = float(last[atr_col])

        if pd.isna(kijun_val) or pd.isna(atr_val):
            # Fallback to close price +/- ATR if kijun is unavailable
            close = float(last["close"])
            if side == Signal.BUY or side == Signal.STRONG_BUY:
                return round(close - atr_val * 1.5, 8)
            else:
                return round(close + atr_val * 1.5, 8)

        if side in (Signal.BUY, Signal.STRONG_BUY):
            # Stop below kijun by 1x ATR
            return round(kijun_val - atr_val, 8)
        else:
            # Stop above kijun by 1x ATR
            return round(kijun_val + atr_val, 8)

    def _calculate_take_profit(
        self,
        entry_price: float,
        stop_loss: float,
        risk_reward_ratio: float = 2.0,
    ) -> float:
        """Calculate take profit based on risk/reward ratio.

        Args:
            entry_price: Expected entry price.
            stop_loss: Stop loss level.
            risk_reward_ratio: Target R:R ratio (default 2.0).

        Returns:
            Take profit price level.
        """
        risk = abs(entry_price - stop_loss)
        reward = risk * risk_reward_ratio

        if stop_loss < entry_price:
            # Long position: TP above entry
            return round(entry_price + reward, 8)
        else:
            # Short position: TP below entry
            return round(entry_price - reward, 8)

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _ohlcv_list_to_df(candles: list[OHLCV]) -> pd.DataFrame:
        """Convert a list of OHLCV dataclasses to a pandas DataFrame.

        Args:
            candles: List of OHLCV candles.

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume.
        """
        return pd.DataFrame([
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ])

    @staticmethod
    def _compose_signal(
        ichimoku: Signal,
        macd: Signal,
        rsi: Signal,
    ) -> tuple[Signal, float]:
        """Combine individual indicator signals into a composite signal.

        Scoring:
            - BUY = +1, SELL = -1, NEUTRAL = 0
            - Sum >= 3 → STRONG_BUY / STRONG_SELL
            - Sum >= 2 → BUY / SELL
            - Otherwise → NEUTRAL

        Confidence is based on how many indicators agree (0.0 - 1.0).

        Args:
            ichimoku: Ichimoku signal.
            macd: MACD signal.
            rsi: RSI signal.

        Returns:
            Tuple of (composite_signal, confidence).
        """
        buy_signals = {Signal.BUY, Signal.STRONG_BUY}
        sell_signals = {Signal.SELL, Signal.STRONG_SELL}

        bullish_count = sum(1 for s in (ichimoku, macd, rsi) if s in buy_signals)
        bearish_count = sum(1 for s in (ichimoku, macd, rsi) if s in sell_signals)

        total = 3
        if bullish_count == total:
            return Signal.STRONG_BUY, 1.0
        if bullish_count >= 2:
            return Signal.BUY, bullish_count / total
        if bearish_count == total:
            return Signal.STRONG_SELL, 1.0
        if bearish_count >= 2:
            return Signal.SELL, bearish_count / total

        return Signal.NEUTRAL, 0.0

    def _neutral_signal(self, symbol: str, timestamp: datetime) -> StrategySignal:
        """Build a neutral StrategySignal with zero confidence.

        Args:
            symbol: Trading symbol.
            timestamp: Signal timestamp.

        Returns:
            NEUTRAL StrategySignal.
        """
        return StrategySignal(
            symbol=symbol,
            signal=Signal.NEUTRAL,
            confidence=0.0,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            timestamp=timestamp,
        )
