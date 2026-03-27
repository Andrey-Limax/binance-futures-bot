# CONTEXT.md — MEXC Futures Bot

## Status Geral
**Fase atual:** Esqueleto de classes completo
**Última atualização:** 2025-03-27

---

## O que já foi feito
- [x] ConfigManager — estrutura Pydantic + .env loading + validação
- [x] ExchangeInterface — ccxt.pro wrapper + WebSocket tickers + format_amount/price + safe_order_execution com retry
- [x] StrategyEngine — Ichimoku + MACD/RSI + warm-up 100 velas + composite signal (STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL)
- [x] SentimentAnalyzer — Fear & Greed (Alternative.me) + cache 1h + fallback neutral=50 + blocking (score < 20 ou > 85)
- [x] RiskController — position size (1% rule) + dynamic leverage (6x-12x por ATR) + trailing stop
- [x] PositionTracker — persistência TinyDB (positions.json) + daily PnL tracking
- [x] CircuitBreaker — kill switch diário + consecutive losses monitor + cooldown
- [x] TradingBot — orquestração com asyncio.TaskGroup + graceful shutdown SIGTERM/SIGINT
- [x] models.py — enums (Side, Signal, PositionSide...) + Pydantic AppConfig + dataclasses (Ticker, OHLCV, Position, OrderResult...)
- [x] main.py — entry point + structlog configuração JSON
- [x] Dockerfile — multi-stage (builder + runtime) + non-root user + healthcheck
- [x] docker-compose.yaml — volumes para state/logs + resource limits + restart policy
- [x] requirements.txt — dependências pinadas
- [x] .env.example — todas as variáveis de configuração documentadas
- [x] ARCHITECTURE.md — diagramas ASCII do fluxo de dados
- [x] .gitignore — exclusões Python/IDE/data/.env
- [x] README.md — documentação do projeto

## Decisões Técnicas Tomadas
- Stack: Python 3.11+, asyncio, ccxt.pro, pandas-ta-classic (fork), Pydantic v2
- Sentimento: bloqueia se score < 20 ou > 85, fallback neutral=50
- Trailing Stop: lógica manual (MEXC não suporta nativo via API)
- Alavancagem: 6x–12x dinâmico por ATR (% do preço)
- Risco por trade: máx 1% do saldo
- Signal composite: 3 indicadores votam, 3/3 = STRONG, 2/3 = BUY/SELL, else NEUTRAL
- Confiança mínima para entrada: 0.67 (2/3 indicadores concordam)
- TinyDB para persistência leve (sem necessidade de banco externo)

## Arquitetura
```
src/
├── main.py                    # Entry point (asyncio.run)
├── models.py                  # Enums, Pydantic models, dataclasses
├── core/
│   ├── bot.py                 # TradingBot — orquestração com TaskGroup
│   ├── config.py              # ConfigManager — Pydantic Settings + .env
│   ├── exchange.py            # ExchangeInterface — ccxt.pro + WebSocket
│   ├── sentiment.py           # SentimentAnalyzer — Fear & Greed + cache
│   ├── risk.py                # RiskController — sizing + leverage
│   ├── position.py            # PositionTracker — TinyDB persistência
│   └── circuit_breaker.py     # CircuitBreaker — kill switch
├── strategies/
│   └── strategy.py            # StrategyEngine — Ichimoku + MACD/RSI
├── data/
└── utils/
```

## Contexto Pendente / Próximos Passos
- [ ] Testar na MEXC Testnet (verificar conectividade WebSocket)
- [ ] Implementar testes unitários (tests/)
- [ ] Configurar CI/CD no Docker
- [ ] Adicionar métricas de performance (win rate, sharpe ratio)
- [ ] Implementar alertas (Telegram/Discord) para trades executados
- [ ] Backtesting com dados históricos

## Notas Importantes
- NUNCA hardcodar API keys — sempre via .env
- Trailing stop é manual, não ordem nativa da MEXC
- Warm-up obrigatório: 100 velas antes de operar
- pandas-ta-classic (fork) usado em vez de pandas-ta original (compatibilidade Python 3.11)
