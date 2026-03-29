# CONTEXT.md — MEXC Futures Bot

## Status Geral
**Fase atual:** DRY_RUN mode implementado
**Última atualização:** 2026-03-28 (fix disconnect)

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
- [x] Fixes de bugs (2026-03-29):
  - [x] exchange.py — Validadas correções de assinatura (dry_run em ordens/alavancagem, remoção de sandbox_mode e ajustes em disconnect)
- [x] Fixes de bugs (2026-03-28):
  - [x] models.py — AppConfig: adicionados default_leverage, max_leverage, min_leverage
  - [x] models.py — Position.to_dict() e from_dict() implementados (estavam com pass)
  - [x] config.py — _parse_trading_symbols(): leverage= corrigido para default_leverage=
  - [x] sentiment.py — _classify_score(): adicionada label "neutral" (score 45-55)
  - [x] position.py — update_daily_pnl(): lógica de consecutive_losses simplificada
  - [x] exchange.py — __init__: lê TESTNET do env para sandbox mode
  - [x] exchange.py — connect(): removido set_sandbox_mode() (MEXC não suporta sandbox via ccxt); TESTNET flag agora loga warning ao invés de quebrar
  - [x] exchange.py — disconnect(): fix unclosed connector (try/except/finally + always set exchange=None)
  - [x] exchange.py — connect(): fecha instância anterior de exchange antes de criar nova (evita aiohttp connector leak)
- [x] DRY_RUN mode:
  - [x] .env.example — DRY_RUN=true adicionado
  - [x] models.py — AppConfig.dry_run: bool = True
  - [x] config.py — Settings.dry_run + to_app_config() + get_masked_config()
  - [x] exchange.py — safe_order_execution(dry_run) + set_leverage(dry_run): simula ordens com OrderResult sintético
  - [x] bot.py — self.dry_run armazenado, passado para set_leverage e safe_order_execution

## Decisões Técnicas Tomadas
- Stack: Python 3.11+, asyncio, ccxt.pro, pandas-ta-classic (fork), Pydantic v2
- Sentimento: bloqueia se score < 20 ou > 85, fallback neutral=50
- Trailing Stop: lógica manual (MEXC não suporta nativo via API)
- Alavancagem: 6x–12x dinâmico por ATR (% do preço)
- Risco por trade: máx 1% do saldo
- Signal composite: 3 indicadores votam, 3/3 = STRONG, 2/3 = BUY/SELL, else NEUTRAL
- Confiança mínima para entrada: 0.67 (2/3 indicadores concordam)
- TinyDB para persistência leve (sem necessidade de banco externo)
- DRY_RUN=true por padrão — bot conecta na MEXC e recebe preços reais, gera sinais, loga decisões, mas NÃO coloca ordens reais

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
- [ ] Testar na MEXC (verificar conectividade WebSocket — sem testnet, usar pequenas quantias)
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
- MEXC não suporta testnet/sandbox via ccxt — flag TESTNET é ignorado com warning, bot roda em LIVE
