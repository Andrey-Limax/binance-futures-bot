# MEXC Futures Trading Bot

Async-native trading bot for MEXC USDT-M Futures (BTC/ETH) with Ichimoku + MACD/RSI strategy.

## Features

- **Async Native**: Python 3.11+ with asyncio and ccxt.pro WebSocket support
- **Strategy**: Ichimoku Cloud + MACD/RSI composite signal with 100-candle warm-up
- **Sentiment**: Fear & Greed Index blocking during extreme conditions
- **Risk Management**: Dynamic leverage (6x-12x), 1% risk per trade, trailing stops
- **Circuit Breaker**: Daily loss kill switch with consecutive loss monitoring
- **Persistence**: Position state survives container restarts via TinyDB
- **Docker**: Multi-stage build with resource limits and volume mounts

## Quick Start

```bash
# Clone repository
git clone https://github.com/YOUR_USERNAME/mexc-futures-bot.git
cd mexc-futures-bot

# Configure environment
cp .env.example .env
# Edit .env with your MEXC API credentials

# Docker (recommended)
docker-compose up -d

# Or run locally
pip install -r requirements.txt
python -m src.main
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for data flow diagrams and component details.

## Project Structure

```
src/
├── main.py                 # Entry point
├── core/
│   ├── bot.py              # Main orchestration (TradingBot)
│   ├── config.py           # ConfigManager (Pydantic Settings)
│   ├── exchange.py         # ExchangeInterface (ccxt.pro wrapper)
│   ├── sentiment.py        # SentimentAnalyzer (Fear & Greed)
│   ├── risk.py             # RiskController (position sizing)
│   ├── position.py         # PositionTracker (TinyDB persistence)
│   └── circuit_breaker.py  # CircuitBreaker (kill switch)
├── strategies/
│   └── strategy.py         # StrategyEngine (Ichimoku + MACD/RSI)
├── models.py               # Pydantic models & dataclasses
└── utils/                  # Shared utilities
```

## Configuration

All configuration is via environment variables. See `.env.example` for full list.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEXC_API_KEY` | *required* | MEXC API key |
| `MEXC_API_SECRET` | *required* | MEXC API secret |
| `TRADING_SYMBOLS` | `BTC/USDT:USDT,ETH/USDT:USDT` | Symbols to trade |
| `DEFAULT_LEVERAGE` | `6` | Default leverage |
| `MAX_LEVERAGE` | `12` | Maximum leverage |
| `RISK_PER_TRADE_PCT` | `1.0` | Risk per trade (%) |

## Safety

- **NEVER** hardcode API keys
- Trailing stops are manual (MEXC doesn't support native trailing stop via API)
- Circuit breaker halts trading after daily loss threshold
- Sentiment blocks entries during extreme fear/greed

