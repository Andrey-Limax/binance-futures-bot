"""Trading Bot: Main orchestration loop.

Coordinates all components using asyncio.TaskGroup to monitor
multiple symbols concurrently. Handles graceful shutdown on
SIGTERM/SIGINT by canceling open orders and logging final state.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime
from typing import Optional

import structlog

from src.models import (
    AppConfig,
    OrderRequest,
    OrderType,
    Side,
    Signal,
    StrategySignal,
    Ticker,
)
from src.core.config import ConfigManager
from src.core.exchange import ExchangeInterface
from src.core.sentiment import SentimentAnalyzer
from src.core.risk import RiskController
from src.core.position import PositionTracker
from src.core.circuit_breaker import CircuitBreaker
from src.strategies.strategy import StrategyEngine

logger = structlog.get_logger(__name__)


class TradingBot:
    """Main trading bot orchestrator.

    Lifecycle:
        1. __init__: Initialize all components from config
        2. start(): Connect to exchange, warm-up strategies, enter main loop
        3. _main_loop(): Monitor symbols concurrently via TaskGroup
        4. _monitor_symbol(): Per-symbol WebSocket + strategy + execution loop
        5. shutdown(): Graceful teardown on SIGTERM/SIGINT

    Attributes:
        config: Application configuration.
        exchange: MEXC Futures interface.
        sentiment: Fear & Greed analyzer.
        risk: Position sizing and leverage controller.
        tracker: Persistent position state.
        circuit_breaker: Daily loss kill switch.
        strategy: Ichimoku + MACD/RSI engine.
        _running: Flag to control main loop lifecycle.
        _tasks: Active asyncio tasks per symbol.
    """

    def __init__(self, config: AppConfig) -> None:
        """Initialize all bot components from configuration.

        Args:
            config: Validated application configuration.
        """
        self.config = config
        self._logger = logger.bind(component="TradingBot")
        self._running: bool = False
        self._tasks: list[asyncio.Task] = []
        self.dry_run = config.dry_run

        if self.dry_run:
            self._logger.warning(
                "DRY_RUN mode enabled — no real orders will be placed"
            )

        # Initialize components
        self.exchange = ExchangeInterface(
            api_key=config.mexc_api_key,
            api_secret=config.mexc_api_secret,
        )
        self.sentiment = SentimentAnalyzer(config.sentiment)
        self.risk = RiskController(config.risk, config.strategy)
        self.tracker = PositionTracker()
        self.circuit_breaker = CircuitBreaker(config.circuit_breaker)
        self.strategy = StrategyEngine(config.strategy)

    async def start(self) -> None:
        """Start the trading bot.

        Full lifecycle:
            1. Connect to MEXC Futures
            2. Load asset precision for all symbols
            3. Restore open positions from persistence
            4. Warm-up strategy indicators with historical candles
            5. Enter main monitoring loop

        Registers signal handlers for graceful shutdown.
        """
        self._logger.info(
            "bot_starting",
            symbols=[s.symbol for s in self.config.trading_symbols],
            environment=self.config.environment,
        )

        try:
            # 1. Connect to exchange
            await self.exchange.connect()

            # 2. Load precision for all enabled symbols
            enabled_symbols = [
                s for s in self.config.trading_symbols if s.enabled
            ]
            for sym_cfg in enabled_symbols:
                await self.exchange.load_precision(sym_cfg.symbol)

            # 3. Restore open positions from persistence
            self.tracker.open()
            restored = self.tracker.get_all_positions()
            if restored:
                self._logger.info(
                    "positions_restored",
                    count=len(restored),
                    symbols=[p.symbol for p in restored],
                )

            # 4. Warm-up strategy indicators with historical candles
            warmup_candles = self.config.strategy.warmup_candles
            for sym_cfg in enabled_symbols:
                candles = await self.exchange.fetch_ohlcv(
                    sym_cfg.symbol,
                    timeframe="1m",
                    limit=warmup_candles,
                )
                ok = await self.strategy.warm_up(sym_cfg.symbol, candles)
                if not ok:
                    self._logger.warning(
                        "warmup_failed",
                        symbol=sym_cfg.symbol,
                        candles_fetched=len(candles),
                    )

            # 5. Register signal handlers and enter main loop
            self._setup_signal_handlers()
            self._running = True
            self._logger.info("bot_started", symbol_count=len(enabled_symbols))

            await self._main_loop()

        except asyncio.CancelledError:
            self._logger.info("bot_shutdown_requested")
        except Exception:
            self._logger.exception("bot_fatal_error")
            raise
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown procedure.

        Steps:
            1. Set _running = False to stop main loop
            2. Cancel all pending orders for tracked symbols
            3. Log final position states
            4. Close tracker database
            5. Disconnect from exchange
            6. Log shutdown summary
        """
        self._logger.info("shutdown_initiated")
        self._running = False

        # Cancel all pending tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        # Cancel all open orders for every tracked symbol
        for sym_cfg in self.config.trading_symbols:
            try:
                await self.exchange.cancel_all_orders(sym_cfg.symbol)
            except Exception as exc:
                self._logger.warning(
                    "cancel_orders_failed",
                    symbol=sym_cfg.symbol,
                    error=str(exc),
                )

        # Log final position states
        open_positions = []
        try:
            open_positions = self.tracker.get_all_positions()
            for pos in open_positions:
                self._logger.info(
                    "final_position_state",
                    symbol=pos.symbol,
                    side=pos.side.value,
                    entry_price=pos.entry_price,
                    amount=pos.amount,
                    unrealized_pnl=pos.unrealized_pnl,
                )
        except RuntimeError:
            # Em caso de falha precoce na inicialização, o db pode não estar aberto
            pass
        except Exception as exc:
            self._logger.warning("failed_to_log_positions", error=str(exc))

        # Close tracker database
        try:
            self.tracker.close()
        except Exception:
            pass

        # Disconnect from exchange
        try:
            await self.exchange.disconnect()
        except Exception as exc:
            self._logger.error("failed_to_disconnect_exchange", error=str(exc))

        self._logger.info(
            "shutdown_complete",
            open_positions=len(open_positions),
        )

    async def _main_loop(self) -> None:
        """Main monitoring loop using asyncio.TaskGroup.

        Creates one task per trading symbol to monitor concurrently.
        Each task runs _monitor_symbol() which handles its own
        WebSocket connection and strategy evaluation.

        Uses asyncio.TaskGroup (Python 3.11+) for structured concurrency.
        All tasks are cancelled together if any one fails.
        """
        enabled_symbols = [
            s.symbol for s in self.config.trading_symbols if s.enabled
        ]

        async with asyncio.TaskGroup() as tg:
            for symbol in enabled_symbols:
                task = tg.create_task(
                    self._monitor_symbol(symbol),
                    name=f"monitor:{symbol}",
                )
                self._tasks.append(task)

    async def _monitor_symbol(self, symbol: str) -> None:
        """Per-symbol monitoring loop.

        For each symbol:
            1. Subscribe to WebSocket ticker stream
            2. Check circuit breaker status
            3. Fetch sentiment (cached)
            4. Update strategy with new price data
            5. Generate signal
            6. If signal triggers entry:
                a. Check sentiment blocks
                b. Calculate position size via RiskController
                c. Execute order via ExchangeInterface
                d. Save position to PositionTracker
            7. If position open:
                a. Update trailing stop
                b. Check if stop loss hit
                c. Update daily PnL

        Args:
            symbol: Trading symbol to monitor.
        """
        self._logger.info("monitor_started", symbol=symbol)

        try:
            async for ticker in self.exchange.watch_ticker(symbol):
                if not self._running:
                    break

                # 2. Check circuit breaker
                balance_data = await self.exchange.fetch_balance()
                usdt_balance = float(
                    balance_data.get("USDT", {}).get("free", 0.0)
                )
                daily_pnl = self.tracker.get_daily_pnl()
                if daily_pnl is None:
                    from src.models import DailyPnLSnapshot
                    daily_pnl = DailyPnLSnapshot(
                        date=datetime.utcnow().strftime("%Y-%m-%d"),
                        realized_pnl=0.0,
                        unrealized_pnl=0.0,
                        total_pnl=0.0,
                        trades_count=0,
                        winning_trades=0,
                        losing_trades=0,
                        consecutive_losses=0,
                    )

                trading_allowed = self.circuit_breaker.check(
                    daily_pnl=daily_pnl,
                    account_balance=usdt_balance,
                    max_daily_loss_pct=self.config.risk.max_daily_loss_pct,
                )

                if not trading_allowed:
                    self._logger.debug(
                        "trading_blocked_by_breaker",
                        symbol=symbol,
                    )
                    # Still manage existing positions even if breaker tripped
                    position = self.tracker.get_position(symbol)
                    if position is not None:
                        await self._manage_position(symbol, ticker)
                    continue

                # 3. Fetch sentiment only when enabled
                if self.config.sentiment.enabled:
                    sentiment_result = await self.sentiment.get_sentiment()

                # 4. Update strategy with ticker data
                from src.models import OHLCV
                candle = OHLCV(
                    timestamp=ticker.timestamp,
                    open=ticker.last,
                    high=ticker.last,
                    low=ticker.last,
                    close=ticker.last,
                    volume=ticker.volume_24h,
                )
                self.strategy.update_candle(symbol, candle)

                # 5. Generate signal
                signal = self.strategy.generate_signal(symbol)

                # 6. Handle signal for new entries
                position = self.tracker.get_position(symbol)
                if position is None:
                    if signal.signal in (Signal.BUY, Signal.STRONG_BUY,
                                         Signal.SELL, Signal.STRONG_SELL):
                        await self._handle_signal(symbol, signal, ticker)
                else:
                    # 7. Manage existing position
                    await self._manage_position(symbol, ticker)

        except asyncio.CancelledError:
            self._logger.info("monitor_cancelled", symbol=symbol)
            raise
        except Exception:
            self._logger.exception("monitor_error", symbol=symbol)
            raise

    async def _handle_signal(
        self,
        symbol: str,
        signal: StrategySignal,
        ticker: Ticker,
    ) -> None:
        """Process a trading signal and execute if conditions are met.

        Checks:
            1. No open position for this symbol (or close first)
            2. Circuit breaker allows trading
            3. Sentiment doesn't block new entries
            4. Risk controller validates position size

        If all checks pass, execute the order.

        Args:
            symbol: Trading symbol.
            signal: Strategy signal with direction and levels.
            ticker: Current ticker for price reference.
        """
        # 1. Ensure no open position for this symbol
        existing = self.tracker.get_position(symbol)
        if existing is not None:
            self._logger.debug(
                "signal_ignored_position_open",
                symbol=symbol,
                signal=signal.signal.value,
            )
            return

        # 2. Signal must be actionable (not NEUTRAL)
        if signal.signal == Signal.NEUTRAL:
            return

        # 3. Sentiment check
        if self.config.sentiment.enabled:
            sentiment_result = await self.sentiment.get_sentiment()
            if sentiment_result.blocks_new_entries:
                self._logger.info(
                    "signal_blocked_by_sentiment",
                    symbol=symbol,
                    sentiment_score=sentiment_result.score,
                    sentiment_label=sentiment_result.label,
                )
                return

        # 4. Confidence threshold — require at least 0.67 (2/3 indicators agree)
        if signal.confidence < 0.67:
            self._logger.debug(
                "signal_below_confidence_threshold",
                symbol=symbol,
                confidence=signal.confidence,
            )
            return

        # All checks passed — open position
        self._logger.info(
            "signal_accepted",
            symbol=symbol,
            signal=signal.signal.value,
            confidence=signal.confidence,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )
        await self._open_position(symbol, signal, ticker)

    async def _open_position(
        self,
        symbol: str,
        signal: StrategySignal,
        ticker: Ticker,
    ) -> None:
        """Open a new position based on strategy signal.

        Steps:
            1. Calculate dynamic leverage via RiskController
            2. Set leverage on exchange
            3. Calculate position size via RiskController
            4. Build OrderRequest with stop loss and take profit
            5. Execute via ExchangeInterface.safe_order_execution()
            6. Save to PositionTracker

        Args:
            symbol: Trading symbol.
            signal: Strategy signal.
            ticker: Current ticker.
        """
        try:
            # 1. Determine side from signal
            if signal.signal in (Signal.BUY, Signal.STRONG_BUY):
                side = Side.BUY
                position_side = "long"
            else:
                side = Side.SELL
                position_side = "short"

            # 2. Get ATR for dynamic leverage calculation
            atr_col = f"ATR_{self.config.strategy.atr_period}"
            df = self.strategy._candle_cache.get(symbol)
            if df is not None and atr_col in df.columns:
                atr_value = float(df[atr_col].iloc[-1])
            else:
                # Fallback: use stop distance as volatility proxy
                atr_value = abs(signal.entry_price - signal.stop_loss)

            leverage = self.risk.dynamic_leverage(atr_value, signal.entry_price)
            leverage = max(self.config.min_leverage,
                           min(leverage, self.config.max_leverage))

            # 3. Set leverage on exchange
            await self.exchange.set_leverage(symbol, leverage, dry_run=self.dry_run)

            # 4. Calculate position size
            balance_data = await self.exchange.fetch_balance()
            usdt_balance = float(
                balance_data.get("USDT", {}).get("free", 0.0)
            )

            size_result = self.risk.calculate_position_size(
                symbol=symbol,
                entry_price=signal.entry_price,
                stop_loss_price=signal.stop_loss,
                account_balance=usdt_balance,
                leverage=leverage,
            )

            if size_result.recommended_amount <= 0:
                self._logger.warning(
                    "position_size_zero",
                    symbol=symbol,
                )
                return

            # 5. Build and execute order
            order = OrderRequest(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                amount=size_result.recommended_amount,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                leverage=leverage,
            )

            result = await self.exchange.safe_order_execution(order, dry_run=self.dry_run)

            # 6. Calculate initial trailing stop
            trailing_stop = self.risk.calculate_trailing_stop(
                entry_price=signal.entry_price,
                current_price=ticker.last,
                side=position_side,
            )

            # 7. Save position to tracker
            from src.models import Position, PositionSide
            from datetime import datetime as dt

            position = Position(
                symbol=symbol,
                side=PositionSide.LONG if side == Side.BUY else PositionSide.SHORT,
                entry_price=signal.entry_price,
                amount=size_result.recommended_amount,
                leverage=leverage,
                stop_loss=signal.stop_loss,
                trailing_stop=trailing_stop,
                take_profit=signal.take_profit,
                opened_at=dt.utcnow(),
                updated_at=dt.utcnow(),
            )
            self.tracker.save_position(position)

            self._logger.info(
                "position_opened",
                symbol=symbol,
                side=side.value,
                amount=size_result.recommended_amount,
                entry_price=signal.entry_price,
                leverage=leverage,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                trailing_stop=trailing_stop,
                order_id=result.order_id,
            )

        except Exception:
            self._logger.exception("open_position_failed", symbol=symbol)

    async def _manage_position(self, symbol: str, ticker: Ticker) -> None:
        """Manage an existing open position.

        Updates trailing stop based on current price.
        Checks if stop loss has been hit and closes if so.

        Args:
            symbol: Trading symbol with open position.
            ticker: Current ticker.
        """
        position = self.tracker.get_position(symbol)
        if position is None:
            return

        side_str = position.side.value  # "long" or "short"
        current_price = ticker.last

        # Calculate updated trailing stop
        new_trailing_stop = self.risk.calculate_trailing_stop(
            entry_price=position.entry_price,
            current_price=current_price,
            side=side_str,
        )

        # Update unrealized PnL
        if side_str == "long":
            unrealized_pnl = (
                (current_price - position.entry_price) / position.entry_price
                * position.amount * position.leverage
            )
        else:
            unrealized_pnl = (
                (position.entry_price - current_price) / position.entry_price
                * position.amount * position.leverage
            )

        # Update position with new trailing stop and PnL
        from src.models import Position, PositionSide
        from datetime import datetime as dt

        updated_position = Position(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            amount=position.amount,
            leverage=position.leverage,
            stop_loss=position.stop_loss,
            trailing_stop=new_trailing_stop,
            take_profit=position.take_profit,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=position.realized_pnl,
            opened_at=position.opened_at,
            updated_at=dt.utcnow(),
        )
        self.tracker.save_position(updated_position)

        # Update daily PnL with unrealized
        self.tracker.update_daily_pnl(
            realized_pnl=position.realized_pnl,
            unrealized_pnl=unrealized_pnl,
        )

        # Check stop loss
        stop_hit = False
        if side_str == "long" and current_price <= position.trailing_stop:
            stop_hit = True
        elif side_str == "short" and current_price >= position.trailing_stop:
            stop_hit = True

        if stop_hit:
            self._logger.info(
                "stop_loss_hit",
                symbol=symbol,
                side=side_str,
                current_price=current_price,
                trailing_stop=position.trailing_stop,
                unrealized_pnl=unrealized_pnl,
            )
            await self._close_position(symbol, reason="stop_loss")

        # Check take profit
        tp_hit = False
        if side_str == "long" and current_price >= position.take_profit:
            tp_hit = True
        elif side_str == "short" and current_price <= position.take_profit:
            tp_hit = True

        if tp_hit:
            self._logger.info(
                "take_profit_hit",
                symbol=symbol,
                side=side_str,
                current_price=current_price,
                take_profit=position.take_profit,
                unrealized_pnl=unrealized_pnl,
            )
            await self._close_position(symbol, reason="take_profit")

    async def _close_position(self, symbol: str, reason: str) -> None:
        """Close an open position.

        Executes a reduce-only order in the opposite direction.
        Updates tracker and daily PnL.

        Args:
            symbol: Trading symbol to close.
            reason: Reason for closing (stop_loss, take_profit, manual, shutdown).
        """
        position = self.tracker.get_position(symbol)
        if position is None:
            self._logger.warning("close_no_position", symbol=symbol)
            return

        # Determine closing side (opposite of position)
        if position.side.value == "long":
            close_side = Side.SELL
        else:
            close_side = Side.BUY

        try:
            order = OrderRequest(
                symbol=symbol,
                side=close_side,
                order_type=OrderType.MARKET,
                amount=position.amount,
                reduce_only=True,
            )

            result = await self.exchange.safe_order_execution(order, dry_run=self.dry_run)

            # Determine if trade was winner
            is_winner = position.unrealized_pnl >= 0

            # Update daily PnL
            realized = position.realized_pnl + position.unrealized_pnl
            self.tracker.update_daily_pnl(
                realized_pnl=realized,
                unrealized_pnl=0.0,
                is_winner=is_winner,
            )

            # Remove from tracker
            self.tracker.remove_position(symbol)

            self._logger.info(
                "position_closed",
                symbol=symbol,
                reason=reason,
                side=position.side.value,
                amount=position.amount,
                unrealized_pnl=position.unrealized_pnl,
                is_winner=is_winner,
                order_id=result.order_id,
            )

        except Exception:
            self._logger.exception("close_position_failed", symbol=symbol)

    def _setup_signal_handlers(self) -> None:
        """Register SIGTERM and SIGINT handlers for graceful shutdown.

        On Unix: Uses signal.signal()
        On Windows: Uses signal.signal() for SIGINT only
                    (SIGTERM not supported on Windows)
        """
        loop = asyncio.get_running_loop()

        def _handle_shutdown(sig: signal.Signals) -> None:
            self._logger.info("shutdown_signal_received", signal=sig.name)
            self._running = False
            # Cancel all running tasks to break out of the event loop
            for task in self._tasks:
                if not task.done():
                    task.cancel()

        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(
                    sig,
                    _handle_shutdown,
                    sig,
                )
        else:
            # Windows only supports SIGINT
            signal.signal(signal.SIGINT, lambda s, f: _handle_shutdown(signal.SIGINT))


async def run_bot() -> None:
    """Entry point for the trading bot.

    Loads configuration, initializes the bot, and runs the main loop.
    Handles top-level exceptions and ensures cleanup.
    """
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

    _logger = structlog.get_logger("run_bot")

    try:
        config_manager = ConfigManager.load()
        config = config_manager.to_app_config()

        _logger.info(
            "config_loaded",
            symbols=[s.symbol for s in config.trading_symbols],
            environment=config.environment,
        )

        bot = TradingBot(config)
        await bot.start()

    except KeyboardInterrupt:
        _logger.info("bot_interrupted")
    except Exception:
        _logger.exception("bot_fatal_error")
        raise
