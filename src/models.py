"""Data models for the MEXC Futures Trading Bot.

Defines all Pydantic models, dataclasses, and enums used across the system.
All models enforce strict typing for mission-critical trading operations.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ─── Enums ────────────────────────────────────────────────────────────────────

class Side(str, enum.Enum):
    """Order side."""
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    """Order type."""
    MARKET = "market"
    LIMIT = "limit"


class PositionSide(str, enum.Enum):
    """Position side for hedge mode."""
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


class OrderStatus(str, enum.Enum):
    """Order lifecycle status."""
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    FAILED = "failed"


class Signal(str, enum.Enum):
    """Trading signal from strategy engine."""
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    NEUTRAL = "neutral"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


# ─── Config Models (Pydantic) ────────────────────────────────────────────────

class TradingSymbolConfig(BaseModel):
    """Configuration for a single trading symbol."""
    symbol: str = Field(..., description="Symbol in CCXT format, e.g. 'BTC/USDT:USDT'")
    enabled: bool = Field(default=True, description="Whether this symbol is actively traded")
    min_leverage: int = Field(default=6, ge=1, le=20)
    max_leverage: int = Field(default=12, ge=1, le=20)
    default_leverage: int = Field(default=6, ge=1, le=20)

    @field_validator("max_leverage")
    @classmethod
    def max_gte_min(cls, v: int, info) -> int:
        """Validate max_leverage >= min_leverage."""
        return v


class RiskConfig(BaseModel):
    """Risk management parameters."""
    max_daily_loss_pct: float = Field(default=5.0, gt=0, le=100)
    max_position_pct: float = Field(default=10.0, gt=0, le=100)
    stop_loss_pct: float = Field(default=2.0, gt=0, le=100)
    trailing_stop_pct: float = Field(default=1.5, gt=0, le=100)
    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=100)


class StrategyConfig(BaseModel):
    """Strategy indicator parameters."""
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


class SentimentConfig(BaseModel):
    """Sentiment analysis configuration."""
    extreme_fear_threshold: int = Field(default=20, ge=0, le=100)
    extreme_greed_threshold: int = Field(default=85, ge=0, le=100)
    cache_ttl_seconds: int = Field(default=3600, ge=60)


class CircuitBreakerConfig(BaseModel):
    """Circuit breaker / kill switch configuration."""
    max_consecutive_losses: int = Field(default=3, ge=1)
    cooldown_minutes: int = Field(default=30, ge=1)


class AppConfig(BaseModel):
    """Root application configuration aggregating all sub-configs."""
    # MEXC credentials
    mexc_api_key: str = Field(..., min_length=1)
    mexc_api_secret: str = Field(..., min_length=1)

    # Trading
    trading_symbols: list[TradingSymbolConfig] = Field(default_factory=list)

    # Sub-configs
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    sentiment: SentimentConfig = Field(default_factory=SentimentConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

    # App
    environment: str = Field(default="production")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")


# ─── Market Data Models ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ticker:
    """Real-time ticker snapshot from WebSocket."""
    symbol: str
    bid: float
    ask: float
    last: float
    volume_24h: float
    timestamp: datetime


@dataclass(frozen=True)
class OHLCV:
    """Single OHLCV candle."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class CandleSeries:
    """Series of OHLCV candles for a symbol."""
    symbol: str
    timeframe: str
    candles: list[OHLCV] = field(default_factory=list)

    def last(self, n: int = 1) -> list[OHLCV]:
        """Return last N candles."""
        return self.candles[-n:]


# ─── Signal / Strategy Models ────────────────────────────────────────────────

@dataclass(frozen=True)
class StrategySignal:
    """Output of the strategy engine for a single symbol."""
    symbol: str
    signal: Signal
    confidence: float  # 0.0 - 1.0
    entry_price: float
    stop_loss: float
    take_profit: float
    timestamp: datetime
    metadata: dict = field(default_factory=dict)


# ─── Sentiment Models ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SentimentResult:
    """Fear & Greed Index result with blocking logic."""
    score: int  # 0-100
    label: str  # e.g. "fear", "greed", "neutral"
    is_extreme_fear: bool
    is_extreme_greed: bool
    fetched_at: datetime
    source: str = "alternative.me"
    is_fallback: bool = False

    @property
    def blocks_new_entries(self) -> bool:
        """New entries should be blocked in extreme conditions."""
        return self.is_extreme_fear or self.is_extreme_greed


# ─── Order / Position Models ─────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderRequest:
    """Request to place an order."""
    symbol: str
    side: Side
    order_type: OrderType
    amount: float
    price: Optional[float] = None  # Required for limit orders
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: int = 1
    reduce_only: bool = False
    client_order_id: Optional[str] = None


@dataclass
class OrderResult:
    """Result of an order execution."""
    order_id: str
    client_order_id: Optional[str]
    symbol: str
    side: Side
    order_type: OrderType
    status: OrderStatus
    amount: float
    filled: float
    remaining: float
    price: Optional[float]
    average_fill_price: Optional[float]
    fee: Optional[float]
    timestamp: datetime


@dataclass
class Position:
    """Open position state for persistence."""
    symbol: str
    side: PositionSide
    entry_price: float
    amount: float
    leverage: int
    stop_loss: float
    trailing_stop: float
    take_profit: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Serialize to dict for JSON/TinyDB persistence."""
        pass

    @classmethod
    def from_dict(cls, data: dict) -> Position:
        """Deserialize from dict."""
        pass


# ─── Risk Models ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PositionSizeResult:
    """Output of position size calculation."""
    symbol: str
    recommended_amount: float
    max_amount: float
    risk_amount_usdt: float
    stop_distance_pct: float
    leverage: int


@dataclass(frozen=True)
class DailyPnLSnapshot:
    """Daily PnL tracking for circuit breaker."""
    date: str  # YYYY-MM-DD
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    trades_count: int
    winning_trades: int
    losing_trades: int
    consecutive_losses: int


# ─── Exchange Info Models ────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssetPrecision:
    """Tick size and lot size rules for a symbol."""
    symbol: str
    price_precision: int
    amount_precision: int
    tick_size: float  # Minimum price increment
    lot_size: float   # Minimum amount increment
    min_amount: float
    max_amount: float
    min_cost: float
