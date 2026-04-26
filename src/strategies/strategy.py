"""Strategy Engine: MultiTFScalper."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import pandas_ta_classic as ta
import structlog

from src.models import OHLCV, Signal, StrategyConfig, StrategySignal

logger = structlog.get_logger(__name__)


class StrategyEngine:
    """Multi-timeframe scalper strategy engine.

    Public interface is intentionally kept compatible with bot.py usage:
    - async warm_up(...)
    - sync update_candle(...)
    - async generate_signal(...)
    """

    def __init__(self, config: StrategyConfig, _vp_config: Any = None) -> None:
        self.config = config
        self._logger = logger.bind(component="StrategyEngine")
        self._candle_cache: dict[str, pd.DataFrame] = {}
        self._candle_cache_15m: dict[str, pd.DataFrame] = {}
        self._candle_cache_1h: dict[str, pd.DataFrame] = {}
        self._is_warmed_up: dict[str, bool] = {}
        self._max_cache_len = max(self.config.warmup_candles + 300, 400)

    async def warm_up(self, symbol: str, candles: list[OHLCV]) -> bool:
        """Warm up with 5m candles and derive higher timeframes by resampling."""
        if len(candles) < self.config.warmup_candles:
            self._logger.warning(
                "insufficient_candles_for_warmup",
                symbol=symbol,
                provided=len(candles),
                required=self.config.warmup_candles,
            )
            self._is_warmed_up[symbol] = False
            return False

        df_5m = self._ohlcv_list_to_df(candles)
        if df_5m.empty:
            self._is_warmed_up[symbol] = False
            return False

        self._candle_cache[symbol] = df_5m.tail(self._max_cache_len).reset_index(drop=True)
        self._rebuild_derived_caches(symbol)
        self._is_warmed_up[symbol] = True
        return True

    def update_candle(self, symbol: str, candle: OHLCV, timeframe: str) -> None:
        """Update 5m cache and rebuild derived 15m/1h caches."""
        _ = timeframe  # Kept for interface compatibility.

        candle_ts = pd.to_datetime(candle.timestamp)
        candle_bucket = candle_ts.floor("5min")

        if symbol not in self._candle_cache:
            self._candle_cache[symbol] = self._ohlcv_list_to_df([candle])
            self._rebuild_derived_caches(symbol)
            return

        df = self._candle_cache[symbol]
        if df.empty:
            self._candle_cache[symbol] = self._ohlcv_list_to_df([candle])
            self._rebuild_derived_caches(symbol)
            return

        last_bucket = pd.to_datetime(df["timestamp"].iloc[-1]).floor("5min")
        if candle_bucket == last_bucket:
            idx = df.index[-1]
            df.at[idx, "high"] = max(float(df.at[idx, "high"]), float(candle.high))
            df.at[idx, "low"] = min(float(df.at[idx, "low"]), float(candle.low))
            df.at[idx, "close"] = float(candle.close)
            df.at[idx, "volume"] = float(df.at[idx, "volume"]) + float(candle.volume)
        else:
            new_row = pd.DataFrame(
                [
                    {
                        "timestamp": candle_bucket,
                        "open": float(candle.open),
                        "high": float(candle.high),
                        "low": float(candle.low),
                        "close": float(candle.close),
                        "volume": float(candle.volume),
                    }
                ]
            )
            df = pd.concat([df, new_row], ignore_index=True)
            self._candle_cache[symbol] = df

        self._candle_cache[symbol] = self._candle_cache[symbol].tail(self._max_cache_len).reset_index(drop=True)
        self._rebuild_derived_caches(symbol)

    async def generate_signal(self, symbol: str) -> StrategySignal:
        """Generate strategy signal from 3-layer MultiTFScalper logic."""
        now = datetime.utcnow()
        df_5m = self._candle_cache.get(symbol)
        price = float(df_5m["close"].iloc[-1]) if df_5m is not None and not df_5m.empty else 0.0

        if not self._is_warmed_up.get(symbol, False) or df_5m is None or len(df_5m) < 25:
            return self._neutral_signal(symbol, now, price, {"reason": "not_warmed_or_insufficient_5m"})

        try:
            blocked, breaker_reason = self._check_internal_circuit_breakers(df_5m)
            if blocked:
                return self._neutral_signal(symbol, now, price, {"reason": breaker_reason})

            bias, bias_meta = self._evaluate_bias(symbol)
            if bias == "neutral":
                return self._neutral_signal(symbol, now, price, {"bias": "neutral", **bias_meta})

            momentum_score, momentum_meta = self._evaluate_momentum(symbol, bias)
            if momentum_score < self.config.momentum_min_score:
                return self._neutral_signal(
                    symbol,
                    now,
                    price,
                    {
                        "bias": bias,
                        "momentum_score": momentum_score,
                        "momentum_min_score": self.config.momentum_min_score,
                        **bias_meta,
                        **momentum_meta,
                    },
                )

            trigger_ok, trigger_meta = self._evaluate_entry_trigger(df_5m, bias)
            if not trigger_ok:
                return self._neutral_signal(
                    symbol,
                    now,
                    price,
                    {"bias": bias, "momentum_score": momentum_score, **bias_meta, **momentum_meta, **trigger_meta},
                )

            side_signal = Signal.BUY if bias == "long" else Signal.SELL
            sl, tp1, tp2 = self._compute_risk_targets(df_5m, price, bias)
            confidence = min(1.0, max(0.0, momentum_score / 3.0))
            metadata = {
                "bias": bias,
                "momentum_score": momentum_score,
                "momentum_min_score": self.config.momentum_min_score,
                "tp1_price": tp1,
                "tp2_price": tp2,
                "trailing_active": False,
                **bias_meta,
                **momentum_meta,
                **trigger_meta,
            }
            return StrategySignal(
                symbol=symbol,
                signal=side_signal,
                confidence=confidence,
                entry_price=price,
                stop_loss=sl,
                take_profit=tp2,
                timestamp=now,
                metadata=metadata,
            )
        except Exception as exc:
            self._logger.exception("generate_signal_failed", symbol=symbol, error=str(exc))
            return self._neutral_signal(symbol, now, price, {"reason": "strategy_error", "error": str(exc)})

    def _check_internal_circuit_breakers(self, df_5m: pd.DataFrame) -> tuple[bool, str]:
        df = df_5m.copy()
        bb = df.ta.bbands(length=20, std=2.0)
        if bb is not None and not bb.empty:
            df = pd.concat([df, bb], axis=1)
            lower_col = self._first_col(df, "BBL_")
            upper_col = self._first_col(df, "BBU_")
            if lower_col and upper_col:
                close = float(df["close"].iloc[-1])
                lower = float(df[lower_col].iloc[-1])
                upper = float(df[upper_col].iloc[-1])
                if close > 0:
                    bb_width = (upper - lower) / close
                    if bb_width < 0.01:
                        return True, "bb_squeeze"

        vol_avg20 = df["volume"].rolling(20).mean()
        if pd.notna(vol_avg20.iloc[-1]):
            if float(df["volume"].iloc[-1]) < float(vol_avg20.iloc[-1]) * 0.3:
                return True, "volume_floor"
        return False, ""

    def _evaluate_bias(self, symbol: str) -> tuple[str, dict[str, Any]]:
        df = self._candle_cache_1h.get(symbol)
        if df is None or len(df) < 205:
            return "neutral", {"bias_reason": "insufficient_1h"}

        work = df.copy()
        ema50_result = work.ta.ema(length=50)
        work["ema50"] = ema50_result.iloc[:, 0] if isinstance(ema50_result, pd.DataFrame) else ema50_result

        ema200_result = work.ta.ema(length=200)
        work["ema200"] = ema200_result.iloc[:, 0] if isinstance(ema200_result, pd.DataFrame) else ema200_result

        rsi14_result = work.ta.rsi(length=14)
        work["rsi14"] = rsi14_result.iloc[:, 0] if isinstance(rsi14_result, pd.DataFrame) else rsi14_result
        work["vwap_day"] = self._daily_vwap(work)

        last = work.iloc[-1]
        price = float(last["close"])
        ema50 = float(last["ema50"]) if pd.notna(last["ema50"]) else price
        ema200 = float(last["ema200"]) if pd.notna(last["ema200"]) else price
        vwap_day = float(last["vwap_day"]) if pd.notna(last["vwap_day"]) else price
        rsi = float(last["rsi14"]) if pd.notna(last["rsi14"]) else 50.0

        long_score = 0
        short_score = 0

        if price > ema50 > ema200:
            long_score += 1
        elif price < ema50 < ema200:
            short_score += 1

        if price > vwap_day:
            long_score += 1
        elif price < vwap_day:
            short_score += 1

        block_longs = rsi > 70.0
        block_shorts = rsi < 30.0

        if block_longs:
            long_score = 0
        if block_shorts:
            short_score = 0

        if long_score >= 2:
            bias = "long"
        elif short_score >= 2:
            bias = "short"
        else:
            bias = "neutral"

        return bias, {
            "bias_long_score": long_score,
            "bias_short_score": short_score,
            "bias_rsi14": rsi,
            "bias_block_longs": block_longs,
            "bias_block_shorts": block_shorts,
        }

    def _evaluate_momentum(self, symbol: str, bias: str) -> tuple[int, dict[str, Any]]:
        df = self._candle_cache_15m.get(symbol)
        if df is None or len(df) < 30:
            return 0, {"momentum_reason": "insufficient_15m"}

        work = df.copy()
        work["ema9"] = work.ta.ema(length=9)
        work["ema21"] = work.ta.ema(length=21)
        macd = work.ta.macd(
            fast=self.config.macd_fast,
            slow=self.config.macd_slow,
            signal=self.config.macd_signal,
        )
        if macd is not None and not macd.empty:
            work = pd.concat([work, macd], axis=1)

        score = 0

        crossover_hit = self._ema_crossover_within_window(work, bias, 3)
        if crossover_hit:
            score += 1

        hist_col = self._first_col(work, "MACDh_")
        hist_expanding = False
        if hist_col and len(work) >= 2:
            prev_hist = float(work[hist_col].iloc[-2])
            curr_hist = float(work[hist_col].iloc[-1])
            if bias == "long":
                hist_expanding = curr_hist > prev_hist and curr_hist > 0.0
            else:
                hist_expanding = curr_hist < prev_hist and curr_hist < 0.0
            if hist_expanding:
                score += 1

        vol_avg20 = work["volume"].rolling(20).mean()
        volume_spike = False
        if pd.notna(vol_avg20.iloc[-1]):
            volume_spike = float(work["volume"].iloc[-1]) > float(vol_avg20.iloc[-1]) * 1.5
            if volume_spike:
                score += 1

        if score < self.config.momentum_min_score:
            return 0, {
                "momentum_crossover": crossover_hit,
                "momentum_macd_hist_expanding": hist_expanding,
                "momentum_volume_spike": volume_spike,
            }

        return score, {
            "momentum_crossover": crossover_hit,
            "momentum_macd_hist_expanding": hist_expanding,
            "momentum_volume_spike": volume_spike,
        }

    def _evaluate_entry_trigger(self, df_5m: pd.DataFrame, bias: str) -> tuple[bool, dict[str, Any]]:
        if len(df_5m) < 25:
            return False, {"trigger_reason": "insufficient_5m"}

        work = df_5m.copy()
        work["rsi_5m"] = work.ta.rsi(length=self.config.rsi_period_5m)
        work["vwap_day"] = self._daily_vwap(work)

        curr = work.iloc[-1]
        prev = work.iloc[-2]

        rsi_val = float(curr["rsi_5m"]) if pd.notna(curr["rsi_5m"]) else 50.0
        if bias == "long":
            rsi_ok = rsi_val < 30.0
            vwap_bounce = float(prev["close"]) < float(prev["vwap_day"]) and float(curr["close"]) > float(curr["vwap_day"])
            pattern_ok = self._is_bullish_engulfing(prev, curr) or self._has_wick_signal(curr, "long")
        else:
            rsi_ok = rsi_val > 70.0
            vwap_bounce = float(prev["close"]) > float(prev["vwap_day"]) and float(curr["close"]) < float(curr["vwap_day"])
            pattern_ok = self._is_bearish_engulfing(prev, curr) or self._has_wick_signal(curr, "short")

        trigger_ok = bool(rsi_ok and vwap_bounce and pattern_ok)
        return trigger_ok, {
            "trigger_rsi_ok": rsi_ok,
            "trigger_vwap_bounce": vwap_bounce,
            "trigger_pattern_ok": pattern_ok,
            "trigger_rsi_value": rsi_val,
        }

    def _compute_risk_targets(self, df_5m: pd.DataFrame, entry_price: float, bias: str) -> tuple[float, float, float]:
        atr_series = df_5m.ta.atr(length=self.config.atr_period)
        atr_val = float(atr_series.iloc[-1]) if atr_series is not None and pd.notna(atr_series.iloc[-1]) else 0.0

        sl_distance = atr_val * 1.5
        max_distance = entry_price * 0.005
        if sl_distance <= 0:
            sl_distance = max_distance
        sl_distance = min(sl_distance, max_distance)

        if bias == "long":
            sl = entry_price - sl_distance
            tp1 = entry_price + sl_distance * 1.5
            tp2 = entry_price + sl_distance * 2.5
        else:
            sl = entry_price + sl_distance
            tp1 = entry_price - sl_distance * 1.5
            tp2 = entry_price - sl_distance * 2.5

        return round(sl, 8), round(tp1, 8), round(tp2, 8)

    def _ema_crossover_within_window(self, df: pd.DataFrame, bias: str, window: int) -> bool:
        if len(df) < window + 1:
            return False
        segment = df.tail(window + 1).reset_index(drop=True)
        for i in range(1, len(segment)):
            prev_ema9 = float(segment["ema9"].iloc[i - 1])
            prev_ema21 = float(segment["ema21"].iloc[i - 1])
            curr_ema9 = float(segment["ema9"].iloc[i])
            curr_ema21 = float(segment["ema21"].iloc[i])
            if bias == "long" and curr_ema9 > curr_ema21 and prev_ema9 <= prev_ema21:
                return True
            if bias == "short" and curr_ema9 < curr_ema21 and prev_ema9 >= prev_ema21:
                return True
        return False

    def _has_wick_signal(self, candle: pd.Series, side: str) -> bool:
        body = abs(float(candle["close"]) - float(candle["open"]))
        body = max(body, 1e-12)
        upper_wick = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))
        lower_wick = min(float(candle["open"]), float(candle["close"])) - float(candle["low"])
        if side == "long":
            return lower_wick >= body * 2.0
        return upper_wick >= body * 2.0

    def _is_bullish_engulfing(self, prev: pd.Series, curr: pd.Series) -> bool:
        prev_bear = float(prev["close"]) < float(prev["open"])
        curr_bull = float(curr["close"]) > float(curr["open"])
        engulf = float(curr["open"]) <= float(prev["close"]) and float(curr["close"]) >= float(prev["open"])
        return bool(prev_bear and curr_bull and engulf)

    def _is_bearish_engulfing(self, prev: pd.Series, curr: pd.Series) -> bool:
        prev_bull = float(prev["close"]) > float(prev["open"])
        curr_bear = float(curr["close"]) < float(curr["open"])
        engulf = float(curr["open"]) >= float(prev["close"]) and float(curr["close"]) <= float(prev["open"])
        return bool(prev_bull and curr_bear and engulf)

    def _daily_vwap(self, df: pd.DataFrame) -> pd.Series:
        try:
            vwap_series = ta.vwap(
                high=df["high"],
                low=df["low"],
                close=df["close"],
                volume=df["volume"],
                anchor="D",
            )
            if vwap_series is not None:
                return vwap_series
        except Exception:
            pass
        return pd.Series(index=df.index, dtype="float64")

    def _rebuild_derived_caches(self, symbol: str) -> None:
        df_5m = self._candle_cache.get(symbol)
        if df_5m is None or df_5m.empty:
            self._candle_cache_15m[symbol] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            self._candle_cache_1h[symbol] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            return
        self._candle_cache_15m[symbol] = self._resample_ohlcv(df_5m, "15min").tail(self._max_cache_len).reset_index(drop=True)
        self._candle_cache_1h[symbol] = self._resample_ohlcv(df_5m, "1h").tail(self._max_cache_len).reset_index(drop=True)

    def _resample_ohlcv(self, df: pd.DataFrame, freq: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        work = df.copy()
        work["timestamp"] = pd.to_datetime(work["timestamp"])
        work = work.sort_values("timestamp")
        work = work.set_index("timestamp")
        out = work.resample(freq).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        out = out.dropna(subset=["open", "high", "low", "close"])
        out = out.reset_index()
        return out

    def _ohlcv_list_to_df(self, candles: list[OHLCV]) -> pd.DataFrame:
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.to_datetime(c.timestamp).floor("5min"),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": float(c.volume),
                }
                for c in candles
            ]
        )
        if df.empty:
            return df
        df = df.sort_values("timestamp")
        df = df.drop_duplicates(subset=["timestamp"], keep="last")
        return df.reset_index(drop=True)

    def _first_col(self, df: pd.DataFrame, prefix: str) -> str | None:
        for col in df.columns:
            if str(col).startswith(prefix):
                return str(col)
        return None

    def _neutral_signal(self, symbol: str, ts: datetime, price: float, metadata: dict[str, Any] | None = None) -> StrategySignal:
        return StrategySignal(
            symbol=symbol,
            signal=Signal.NEUTRAL,
            confidence=0.0,
            entry_price=price,
            stop_loss=0.0,
            take_profit=0.0,
            timestamp=ts,
            metadata=metadata or {},
        )
