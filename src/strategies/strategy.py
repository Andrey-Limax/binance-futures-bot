"""Strategy Engine: MTF Ichimoku (TK Cross Trigger).

Focuses on the 15-minute timeframe for execution, detecting Tenkan-Kijun 
crosses as entry signals. This engine acts as the trigger for the 
Multi-Timeframe strategy where the 1-day timeframe remains the anchor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd
import pandas_ta_classic as ta
import structlog

from src.models import (
    OHLCV,
    Signal,
    StrategyConfig,
    StrategySignal,
    TKCrossDirection,
    TKCrossSignal,
)

logger = structlog.get_logger(__name__)


class StrategyEngine:
    """MTF Ichimoku trigger engine.

    Detects Tenkan-Kijun (TK) crosses on the execution timeframe (e.g. 15m).
    """

    _ICHIMOKU_PREFIX = "ICHIMOKU"
    _ATR_PREFIX = "ATR"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self._logger = logger.bind(component="StrategyEngine")
        self._candle_cache: dict[str, pd.DataFrame] = {}
        self._is_warmed_up: dict[str, bool] = {}

    async def warm_up(self, symbol: str, candles: list[OHLCV]) -> bool:
        """Populate indicators with historical data."""
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
        return True

    def update_candle(self, symbol: str, candle: OHLCV, timeframe: str) -> None:
        """Update last candle or append new one."""
        if symbol not in self._candle_cache:
            return

        df = self._candle_cache[symbol]
        freq = timeframe.replace("m", "min").replace("h", "H").replace("d", "D")
        
        last_ts = pd.to_datetime(df["timestamp"].iloc[-1])
        new_ts = pd.to_datetime(candle.timestamp)
        
        last_bucket = last_ts.floor(freq)
        new_bucket = new_ts.floor(freq)

        if new_bucket == last_bucket:
            idx = df.index[-1]
            df.at[idx, "high"] = max(df.at[idx, "high"], candle.high)
            df.at[idx, "low"] = min(df.at[idx, "low"], candle.low)
            df.at[idx, "close"] = candle.close
            df.at[idx, "volume"] = df.at[idx, "volume"] + candle.volume
        else:
            new_row = self._ohlcv_list_to_df([candle])
            new_row.at[0, "timestamp"] = new_bucket
            self._candle_cache[symbol] = pd.concat([df, new_row], ignore_index=True)

            max_len = self.config.warmup_candles + 50
            if len(self._candle_cache[symbol]) > max_len:
                self._candle_cache[symbol] = self._candle_cache[symbol].iloc[-max_len:].reset_index(drop=True)

    def detect_tk_cross(self, symbol: str) -> TKCrossSignal:
        """Detect if a Tenkan-Kijun cross occurred on the last completed candle."""
        df = self._candle_cache.get(symbol)
        if df is None or len(df) < self.config.ichimoku_kijun + 2:
            return TKCrossSignal(TKCrossDirection.NONE, datetime.utcnow())

        df = self._calculate_ichimoku(df.copy())
        
        tenkan_col = f"{self._ICHIMOKU_PREFIX}_TENKAN_{self.config.ichimoku_tenkan}"
        kijun_col = f"{self._ICHIMOKU_PREFIX}_KIJUN_{self.config.ichimoku_kijun}"
        
        # Check cross between last and previous candle
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        tk_curr = float(curr[tenkan_col])
        kj_curr = float(curr[kijun_col])
        tk_prev = float(prev[tenkan_col])
        kj_prev = float(prev[kijun_col])
        
        if pd.isna(tk_curr) or pd.isna(kj_curr) or pd.isna(tk_prev) or pd.isna(kj_prev):
            return TKCrossSignal(TKCrossDirection.NONE, datetime.utcnow())
            
        # Bullish: Tenkan was below/at Kijun, now it's above
        if tk_curr > kj_curr and tk_prev <= kj_prev:
            return TKCrossSignal(TKCrossDirection.BULLISH, datetime.utcnow())
            
        # Bearish: Tenkan was above/at Kijun, now it's below
        if tk_curr < kj_curr and tk_prev >= kj_prev:
            return TKCrossSignal(TKCrossDirection.BEARISH, datetime.utcnow())
            
        return TKCrossSignal(TKCrossDirection.NONE, datetime.utcnow())

    def generate_signal(self, symbol: str) -> StrategySignal:
        """Generate entry signal based on TK cross and daily bias (called by bot)."""
        now = datetime.utcnow()
        tk_cross = self.detect_tk_cross(symbol)
        
        df = self._candle_cache.get(symbol)
        entry_price = float(df["close"].iloc[-1]) if df is not None else 0.0
        
        if tk_cross.direction == TKCrossDirection.NONE:
            return self._neutral_signal(symbol, now, entry_price)
            
        signal = Signal.BUY if tk_cross.direction == TKCrossDirection.BULLISH else Signal.SELL
        
        # Calculate SL/TP
        if df is not None:
            df_with_ind = self._calculate_atr(self._calculate_ichimoku(df.copy()))
            stop_loss = self._calculate_stop_loss(df_with_ind, signal)
            take_profit = self._calculate_take_profit(entry_price, stop_loss)
        else:
            stop_loss = 0.0
            take_profit = 0.0
            
        return StrategySignal(
            symbol=symbol,
            signal=signal,
            confidence=1.0,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            timestamp=now,
            metadata={"tk_cross": tk_cross.direction.value}
        )

    def _calculate_ichimoku(self, df: pd.DataFrame) -> pd.DataFrame:
        tenkan = self.config.ichimoku_tenkan
        kijun = self.config.ichimoku_kijun
        senkou = self.config.ichimoku_senkou

        ichimoku_df, _ = df.ta.ichimoku(tenkan=tenkan, kijun=kijun, senkou=senkou)
        
        rename_map = {}
        for col in ichimoku_df.columns:
            if col.startswith("ITS_"): rename_map[col] = f"{self._ICHIMOKU_PREFIX}_TENKAN_{tenkan}"
            elif col.startswith("IKS_"): rename_map[col] = f"{self._ICHIMOKU_PREFIX}_KIJUN_{kijun}"
            elif col.startswith("ISA_"): rename_map[col] = f"{self._ICHIMOKU_PREFIX}_SENKOU_A_{tenkan}"
            elif col.startswith("ISB_"): rename_map[col] = f"{self._ICHIMOKU_PREFIX}_SENKOU_B_{senkou}"
            
        return pd.concat([df, ichimoku_df.rename(columns=rename_map)], axis=1)

    def _calculate_atr(self, df: pd.DataFrame) -> pd.DataFrame:
        period = self.config.atr_period
        atr_series = df.ta.atr(length=period)
        if atr_series is not None:
            df[f"{self._ATR_PREFIX}_{period}"] = atr_series
        return df

    def _calculate_stop_loss(self, df: pd.DataFrame, side: Signal) -> float:
        last = df.iloc[-1]
        close = float(last["close"])
        kijun_col = f"{self._ICHIMOKU_PREFIX}_KIJUN_{self.config.ichimoku_kijun}"
        atr_col = f"{self._ATR_PREFIX}_{self.config.atr_period}"
        
        kijun_val = float(last[kijun_col]) if kijun_col in last else close
        atr_val = float(last[atr_col]) if atr_col in last else (close * 0.01)
        
        if side == Signal.BUY:
            # Stop below kijun or 1.5 ATR
            return round(min(kijun_val, close - atr_val * 1.5), 8)
        else:
            # Stop above kijun or 1.5 ATR
            return round(max(kijun_val, close + atr_val * 1.5), 8)

    def _calculate_take_profit(self, entry: float, sl: float) -> float:
        risk = abs(entry - sl)
        if sl < entry: return round(entry + risk * 2, 8) # Long
        return round(entry - risk * 2, 8) # Short

    def _ohlcv_list_to_df(self, candles: list[OHLCV]) -> pd.DataFrame:
        return pd.DataFrame([
            {"timestamp": c.timestamp, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in candles
        ])

    def _neutral_signal(self, symbol: str, ts: datetime, price: float) -> StrategySignal:
        return StrategySignal(symbol, Signal.NEUTRAL, 0.0, price, 0.0, 0.0, ts)
