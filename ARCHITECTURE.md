# Binance Futures Bot — Architecture & Data Flow

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BINANCE FUTURES TRADING BOT                          │
│                      Python 3.11+ / asyncio / ccxt.pro                      │
└─────────────────────────────────────────────────────────────────────────────┘

                                    ┌─────────┐
                                    │ .env    │
                                    │ Config  │
                                    └────┬────┘
                                         │
                                    ┌────▼────┐
                                    │ Config  │
                                    │ Manager │
                                    └────┬────┘
                                         │
                    ┌────────────────────┬─┴───────────────────────┐
                    │                    │                         │
               ┌────▼────┐        ┌─────▼──────┐           ┌──────▼──────┐
               │ Strategy │        │  Exchange  │           │  Sentiment  │
               │ Config   │        │  Config    │           │  Config     │
               └────┬────┘        └─────┬──────┘           └──────┬──────┘
                    │                   │                         │
               ┌────▼────┐        ┌─────▼──────┐           ┌──────▼──────┐
               │Strategy │        │  Exchange   │           │ Sentiment   │
               │ Engine  │        │  Interface  │           │ Analyzer    │
               │Ichimoku │        │  (ccxt.pro) │           │(Fear&Greed) │
               │MACD/RSI │        └─────┬──────┘           └──────┬──────┘
               └────┬────┘              │                         │
                    │              ┌────▼────┐                    │
                    │              │ BINANCE API│                    │
                    │              │WebSocket│                    │
                    │              └────┬────┘                    │
                    │                   │                         │
                    │              ┌────▼────┐              ┌─────▼─────┐
                    │              │ Ticker  │              │Alternative│
                    │              │ Stream  │              │  .me API  │
                    │              └────┬────┘              └───────────┘
                    │                   │
                    └─────────┬─────────┘
                              │
                        ┌─────▼──────┐
                        │    MAIN    │
                        │   LOOP     │
                        │(TradingBot)│
                        └─────┬──────┘
                              │
            ┌─────────────────┼─────────────────┐
            │                 │                 │
      ┌─────▼──────┐   ┌─────▼──────┐   ┌──────▼──────┐
      │    Risk    │   │  Position  │   │  Circuit    │
      │ Controller │   │  Tracker   │   │  Breaker    │
      │ Position   │   │  (TinyDB)  │   │ Kill Switch │
      │ Sizing     │   └─────┬──────┘   └──────┬──────┘
      │ Leverage   │         │                  │
      └─────┬──────┘    ┌────▼─────┐      ┌─────▼─────┐
            │           │positions │      │ daily_pnl │
            │           │  .json   │      │  .json    │
            └───────────┴──────────┘      └───────────┘
```

## Data Flow — Trade Lifecycle

```
  ┌──────────────────────────────────────────────────────────────────┐
  │                      SYMBOL MONITORING LOOP                      │
  │  (one asyncio.Task per symbol via TaskGroup)                     │
  └──────────────────────────────────────────────────────────────────┘

   ┌─────────────┐    ┌──────────────┐    ┌───────────────┐
   │  WebSocket   │    │   Strategy   │    │  Sentiment    │
   │  Ticker      │───▶│  Engine      │    │  Analyzer     │
   │  Stream      │    │  (Ichimoku   │    │  (Fear&Greed) │
   │  (prices)    │    │   +MACD/RSI) │    └───────┬───────┘
   └─────────────┘    └──────┬───────┘            │
                             │                    │
                             ▼                    │
                    ┌────────────────┐            │
                    │ StrategySignal │◀───────────┘
                    │ (BUY/SELL/     │
                    │  NEUTRAL)      │
                    └───────┬────────┘
                            │
                            ▼
                   ┌─────────────────┐
                   │  CIRCUIT        │    ┌────────────────┐
                   │  BREAKER        │───▶│  BLOCKED?      │
                   │  Check Daily    │    │  YES → skip    │
                   │  Loss/Losses    │    │  NO  → continue│
                   └─────────────────┘    └───────┬────────┘
                                                  │
                                                  ▼
                                        ┌──────────────────┐
                                        │  SENTIMENT       │
                                        │  BLOCK?          │
                                        │  score < 20?     │──── YES ──▶ SKIP ENTRY
                                        │  score > 85?     │
                                        └───────┬──────────┘
                                                │ NO
                                                ▼
                                      ┌─────────────────────┐
                                      │  RISK CONTROLLER    │
                                      │  • Dynamic Leverage │
                                      │  • Position Size    │
                                      │  • Validate Limits  │
                                      └───────┬─────────────┘
                                              │
                                              ▼
                                    ┌───────────────────────┐
                                    │  EXCHANGE INTERFACE   │
                                    │  • Set Leverage       │
                                    │  • Format Amount      │
                                    │  • Execute Order      │
                                    │  • Retry on Network   │
                                    └───────┬───────────────┘
                                            │
                                            ▼
                                  ┌─────────────────────────┐
                                  │  POSITION TRACKER       │
                                  │  • Save Position        │
                                  │  • Update Trailing Stop │
                                  │  • Update Daily PnL     │
                                  └─────────────────────────┘
```

## Graceful Shutdown Flow

```
   ┌─────────────┐
   │ SIGTERM /   │
   │ SIGINT      │
   └──────┬──────┘
          │
          ▼
   ┌──────────────────┐
   │ Set _running =   │
   │ False            │
   └──────┬───────────┘
          │
          ▼
   ┌──────────────────┐
   │ Cancel all       │
   │ asyncio tasks    │
   └──────┬───────────┘
          │
          ▼
   ┌──────────────────┐
   │ Cancel open      │
   │ orders on BINANCE│
   └──────┬───────────┘
          │
          ▼
   ┌──────────────────┐
   │ Log final        │
   │ position states  │
   └──────┬───────────┘
          │
          ▼
   ┌──────────────────┐
   │ Close Position   │
   │ Tracker DB       │
   └──────┬───────────┘
          │
          ▼
   ┌──────────────────┐
   │ Disconnect       │
   │ Exchange         │
   └──────────────────┘
```

## Component Responsibilities

| Component          | File               | Responsibility                                         |
|-------------------|--------------------|--------------------------------------------------------|
| ConfigManager     | `src/core/config.py`     | Load & validate .env, produce AppConfig               |
| ExchangeInterface | `src/core/exchange.py`   | WebSocket tickers, order execution, precision rules   |
| StrategyEngine    | `src/strategies/strategy.py` | Ichimoku + MACD/RSI signal generation              |
| SentimentAnalyzer | `src/core/sentiment.py`  | Fear & Greed with cache + fallback                    |
| RiskController    | `src/core/risk.py`       | Position sizing, dynamic leverage (6x-12x)           |
| PositionTracker   | `src/core/position.py`   | TinyDB persistence, daily PnL tracking                |
| CircuitBreaker    | `src/core/circuit_breaker.py` | Daily loss kill switch, consecutive loss monitor |
| TradingBot        | `src/core/bot.py`        | Main orchestration, TaskGroup concurrency             |

## Technology Stack

```
Python 3.11+          ─── Core language (async/await native)
asyncio               ─── Async event loop
ccxt.pro              ─── Exchange connectivity + WebSocket
pandas + pandas-ta    ─── OHLCV processing + technical indicators
Pydantic v2           ─── Data validation + config management
structlog             ─── Structured JSON logging
TinyDB                ─── Lightweight JSON persistence
aiohttp               ─── Async HTTP for sentiment API
Docker                ─── Containerized deployment
```
