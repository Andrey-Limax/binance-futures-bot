"""Configuration manager using Pydantic Settings.

Loads environment variables from .env file and validates them
against the AppConfig model. Provides typed access to all
bot configuration parameters.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.models import (
    AppConfig,
    CircuitBreakerConfig,
    RiskConfig,
    SentimentConfig,
    StrategyConfig,
    TradingSymbolConfig,
)

logger = structlog.get_logger(__name__)


class Settings(BaseSettings):
    """Raw environment variable mapping with Pydantic Settings.

    All values are loaded from environment variables or .env file.
    Type coercion and validation are handled automatically.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── MEXC Credentials ──────────────────────────────────────────────────
    mexc_api_key: str = Field(..., description="MEXC API key")
    mexc_api_secret: str = Field(..., description="MEXC API secret")

    # ── Trading Symbols ───────────────────────────────────────────────────
    trading_symbols: str = Field(
        default="BTC/USDT:USDT,ETH/USDT:USDT",
        description="Comma-separated list of symbols in CCXT format",
    )

    # ── Leverage ──────────────────────────────────────────────────────────
    default_leverage: int = Field(default=6, ge=1, le=20)
    max_leverage: int = Field(default=12, ge=1, le=20)
    min_leverage: int = Field(default=6, ge=1, le=20)

    # ── Risk Management ───────────────────────────────────────────────────
    max_daily_loss_pct: float = Field(default=5.0, gt=0, le=100)
    max_position_pct: float = Field(default=10.0, gt=0, le=100)
    stop_loss_pct: float = Field(default=2.0, gt=0, le=100)
    trailing_stop_pct: float = Field(default=1.5, gt=0, le=100)
    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=100)

    # ── Sentiment ─────────────────────────────────────────────────────────
    sentiment_extreme_fear_threshold: int = Field(default=20, ge=0, le=100)
    sentiment_extreme_greed_threshold: int = Field(default=85, ge=0, le=100)
    sentiment_cache_ttl_seconds: int = Field(default=3600, ge=60)
    sentiment_enabled: bool = Field(default=False)

    # ── Strategy ──────────────────────────────────────────────────────────
    ichimoku_tenkan: int = Field(default=9, ge=1)
    ichimoku_kijun: int = Field(default=26, ge=1)
    ichimoku_senkou: int = Field(default=52, ge=1)
    macd_fast: int = Field(default=12, ge=1)
    macd_slow: int = Field(default=26, ge=1)
    macd_signal: int = Field(default=9, ge=1)
    rsi_period: int = Field(default=14, ge=1)
    rsi_oversold: float = Field(default=30.0, ge=0, le=100)
    rsi_overbought: float = Field(default=70.0, ge=0, le=100)
    atr_period: int = Field(default=14, ge=1)
    warmup_candles: int = Field(default=100, ge=50)

    # ── Circuit Breaker ───────────────────────────────────────────────────
    circuit_breaker_max_consecutive_losses: int = Field(default=3, ge=1)
    circuit_breaker_cooldown_minutes: int = Field(default=30, ge=1)

    # ── Dry Run ──────────────────────────────────────────────────────────
    dry_run: bool = Field(default=True, description="Simulate orders without executing")

    # ── Application ───────────────────────────────────────────────────────
    environment: str = Field(default="production")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")


class ConfigManager:
    """Centralized configuration manager.

    Loads environment variables via Pydantic Settings, parses them into
    typed AppConfig, and provides validated access to all bot parameters.

    Usage:
        config_mgr = ConfigManager.load()
        app_config = config_mgr.to_app_config()
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = logger.bind(component="ConfigManager")

    @classmethod
    def load(cls, env_file: Optional[str | Path] = None) -> ConfigManager:
        """Load configuration from environment and .env file.

        Args:
            env_file: Optional path to .env file. Defaults to '.env' in CWD.

        Returns:
            ConfigManager instance with validated settings.

        Raises:
            ValidationError: If required environment variables are missing
                             or have invalid values.
        """
        env_path = str(env_file) if env_file else ".env"
        settings = Settings(_env_file=env_path)  # type: ignore[call-arg]
        instance = cls(settings)
        instance._logger.info(
            "Configuration loaded",
            env_file=env_path,
            environment=settings.environment,
            symbols=settings.trading_symbols,
        )
        return instance

    def to_app_config(self) -> AppConfig:
        """Convert raw Settings into a validated AppConfig model.

        Parses the comma-separated trading_symbols string into
        TradingSymbolConfig objects and aggregates all sub-configs.

        Returns:
            Fully validated AppConfig ready for injection into components.
        """
        s = self._settings
        return AppConfig(
            mexc_api_key=s.mexc_api_key,
            mexc_api_secret=s.mexc_api_secret,
            trading_symbols=self._parse_trading_symbols(),
            default_leverage=s.default_leverage,
            max_leverage=s.max_leverage,
            min_leverage=s.min_leverage,
            risk=RiskConfig(
                max_daily_loss_pct=s.max_daily_loss_pct,
                max_position_pct=s.max_position_pct,
                stop_loss_pct=s.stop_loss_pct,
                trailing_stop_pct=s.trailing_stop_pct,
                risk_per_trade_pct=s.risk_per_trade_pct,
            ),
            sentiment=SentimentConfig(
                enabled=s.sentiment_enabled,
                extreme_fear_threshold=s.sentiment_extreme_fear_threshold,
                extreme_greed_threshold=s.sentiment_extreme_greed_threshold,
                cache_ttl_seconds=s.sentiment_cache_ttl_seconds,
            ),
            strategy=StrategyConfig(
                ichimoku_tenkan=s.ichimoku_tenkan,
                ichimoku_kijun=s.ichimoku_kijun,
                ichimoku_senkou=s.ichimoku_senkou,
                macd_fast=s.macd_fast,
                macd_slow=s.macd_slow,
                macd_signal=s.macd_signal,
                rsi_period=s.rsi_period,
                rsi_oversold=s.rsi_oversold,
                rsi_overbought=s.rsi_overbought,
                atr_period=s.atr_period,
                warmup_candles=s.warmup_candles,
            ),
            circuit_breaker=CircuitBreakerConfig(
                max_consecutive_losses=s.circuit_breaker_max_consecutive_losses,
                cooldown_minutes=s.circuit_breaker_cooldown_minutes,
            ),
            dry_run=s.dry_run,
            environment=s.environment,
            log_level=s.log_level,
            log_format=s.log_format,
        )

    def _parse_trading_symbols(self) -> list[TradingSymbolConfig]:
        """Parse TRADING_SYMBOLS env var into TradingSymbolConfig list.

        Format: 'BTC/USDT:USDT,ETH/USDT:USDT'

        Returns:
            List of TradingSymbolConfig with default leverage settings.
        """
        raw = self._settings.trading_symbols
        symbols: list[TradingSymbolConfig] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if entry:
                symbols.append(
                    TradingSymbolConfig(
                        symbol=entry,
                        default_leverage=self._settings.default_leverage,
                    )
                )
        return symbols

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self._settings.environment.lower() == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self._settings.environment.lower() == "development"

    def get_masked_config(self) -> dict:
        """Return config dict with sensitive values masked for logging.

        Returns:
            Dictionary with API keys replaced by '***MASKED***'.
        """
        s = self._settings
        return {
            "mexc_api_key": "***MASKED***",
            "mexc_api_secret": "***MASKED***",
            "trading_symbols": s.trading_symbols,
            "default_leverage": s.default_leverage,
            "max_leverage": s.max_leverage,
            "min_leverage": s.min_leverage,
            "max_daily_loss_pct": s.max_daily_loss_pct,
            "max_position_pct": s.max_position_pct,
            "stop_loss_pct": s.stop_loss_pct,
            "trailing_stop_pct": s.trailing_stop_pct,
            "risk_per_trade_pct": s.risk_per_trade_pct,
            "sentiment_extreme_fear_threshold": s.sentiment_extreme_fear_threshold,
            "sentiment_extreme_greed_threshold": s.sentiment_extreme_greed_threshold,
            "sentiment_cache_ttl_seconds": s.sentiment_cache_ttl_seconds,
            "ichimoku_tenkan": s.ichimoku_tenkan,
            "ichimoku_kijun": s.ichimoku_kijun,
            "ichimoku_senkou": s.ichimoku_senkou,
            "macd_fast": s.macd_fast,
            "macd_slow": s.macd_slow,
            "macd_signal": s.macd_signal,
            "rsi_period": s.rsi_period,
            "rsi_oversold": s.rsi_oversold,
            "rsi_overbought": s.rsi_overbought,
            "atr_period": s.atr_period,
            "warmup_candles": s.warmup_candles,
            "circuit_breaker_max_consecutive_losses": s.circuit_breaker_max_consecutive_losses,
            "circuit_breaker_cooldown_minutes": s.circuit_breaker_cooldown_minutes,
            "dry_run": s.dry_run,
            "environment": s.environment,
            "log_level": s.log_level,
            "log_format": s.log_format,
        }


@lru_cache(maxsize=1)
def get_config_manager(env_file: Optional[str] = None) -> ConfigManager:
    """Singleton factory for ConfigManager.

    Uses lru_cache to ensure only one instance exists per process.
    Call with different env_file paths to reload (cache will be invalidated).

    Args:
        env_file: Path to .env file. None uses default.

    Returns:
        Cached ConfigManager instance.
    """
    return ConfigManager.load(env_file)
