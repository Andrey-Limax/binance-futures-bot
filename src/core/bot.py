"""Trading Bot: Main orchestration loop.

Coordinates all components using asyncio.TaskGroup to monitor
multiple symbols concurrently. Handles MTF Ichimoku strategy logic.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timedelta
from typing import Optional

import structlog

from src.models import (
    AppConfig,
    DailyBiasDirection,
    OrderRequest,
    OrderType,
    Side,
    Signal,
    StrategySignal,
    Ticker,
    PositionSide,
)
from src.core.config import ConfigManager
from src.core.exchange import ExchangeInterface
from src.core.sentiment import SentimentAnalyzer
from src.core.risk import RiskController
from src.core.position import PositionTracker
from src.core.circuit_breaker import CircuitBreaker
from src.core.daily_bias import DailyBiasManager
from src.strategies.strategy import StrategyEngine

logger = structlog.get_logger(__name__)


class TradingBot:
    """Main trading bot orchestrator for MTF Ichimoku (Binance Futures)."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._logger = logger.bind(component="TradingBot")
        self._running: bool = False
        self._tasks: list[asyncio.Task] = []
        self._ticker_counters: dict[str, int] = {}
        self._last_entry_time: dict[str, datetime] = {}
        self.dry_run = config.dry_run

        if self.dry_run:
            self._logger.warning("DRY_RUN mode enabled")

        self.exchange = ExchangeInterface(api_key=config.binance_api_key, api_secret=config.binance_api_secret)
        self.sentiment = SentimentAnalyzer(config.sentiment)
        self.risk = RiskController(config.risk, config.strategy)
        self.tracker = PositionTracker()
        self.circuit_breaker = CircuitBreaker(config.circuit_breaker)
        self.strategy = StrategyEngine(config.strategy)
        self.daily_bias_mgr = DailyBiasManager(config.daily_bias, self.exchange)

    async def start(self) -> None:
        """Start the bot lifecycle."""
        self._logger.info("bot_starting", symbols=[s.symbol for s in self.config.trading_symbols])

        try:
            await self.exchange.connect()
            enabled_symbols = [s for s in self.config.trading_symbols if s.enabled]
            for sym_cfg in enabled_symbols:
                await self.exchange.load_precision(sym_cfg.symbol)

            self.tracker.open()
            
            # 1. Initial bias sync for all symbols
            for sym_cfg in enabled_symbols:
                await self.daily_bias_mgr.calculate_bias(sym_cfg.symbol)

            # 2. Synchronize Positions with Exchange
            await self._sync_positions_with_exchange()

            # 3. Strategy Warmup (Load history for Ichimoku)
            warmup_candles = self.config.strategy.warmup_candles
            timeframe = self.config.timeframe
            for sym_cfg in enabled_symbols:
                self._logger.info("strategy_warmup_started", symbol=sym_cfg.symbol, count=warmup_candles)
                candles = await self.exchange.fetch_ohlcv(sym_cfg.symbol, timeframe=timeframe, limit=warmup_candles)
                await self.strategy.warm_up(sym_cfg.symbol, candles)
                self._logger.info("strategy_warmed_up", symbol=sym_cfg.symbol)

            self._setup_signal_handlers()
            self._running = True
            await self._main_loop()

        except asyncio.CancelledError:
            self._logger.info("bot_shutdown_requested")
        except Exception:
            self._logger.exception("bot_fatal_error")
            raise
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self._running = False
        for task in self._tasks:
            if not task.done(): task.cancel()
        if self._tasks: await asyncio.gather(*self._tasks, return_exceptions=True)
        
        for sym_cfg in self.config.trading_symbols:
            try: await self.exchange.cancel_all_orders(sym_cfg.symbol)
            except: pass
            
        self.tracker.close()
        await self.exchange.disconnect()
        self._logger.info("shutdown_complete")

    async def _main_loop(self) -> None:
        enabled_symbols = [s.symbol for s in self.config.trading_symbols if s.enabled]
        
        # Start each symbol monitor as an independent task
        for symbol in enabled_symbols:
            task = asyncio.create_task(
                self._safe_monitor_symbol(symbol), 
                name=f"monitor:{symbol}"
            )
            self._tasks.append(task)
        
        # Keep the main loop alive while running
        while self._running:
            await asyncio.sleep(1)

    async def _safe_monitor_symbol(self, symbol: str) -> None:
        """Wrapper to provide retry logic for symbol monitoring."""
        retry_delay = 30
        while self._running:
            try:
                await self._monitor_symbol(symbol)
            except asyncio.CancelledError:
                break
            except Exception:
                self._logger.exception("symbol_monitor_failed", symbol=symbol, next_retry_in=retry_delay)
                await asyncio.sleep(retry_delay)

    async def _monitor_symbol(self, symbol: str) -> None:
        self._logger.info("monitor_started", symbol=symbol)
        last_bias_update = datetime.utcnow() - timedelta(hours=1) # Force immediate update
        last_heartbeat = datetime.utcnow()
        ticks_received = 0

        async for ticker in self.exchange.watch_ticker(symbol):
            if not self._running: break
            ticks_received += 1
            now = datetime.utcnow()

            # Heartbeat (Every 100 ticks or 1 minute)
            if ticks_received % 100 == 0 or (now - last_heartbeat).total_seconds() > 60:
                self._logger.info("monitor_heartbeat", symbol=symbol, ticks=ticks_received)
                last_heartbeat = now

            # 1. Hourly Daily Bias Update
            now = datetime.utcnow()
            if now - last_bias_update > timedelta(hours=1):
                try:
                    await self.daily_bias_mgr.calculate_bias(symbol)
                    last_bias_update = now
                except Exception as e:
                    self._logger.warning("bias_update_failed_using_cached", symbol=symbol, error=str(e))

            # 2. Daily Bias Check
            bias = self.daily_bias_mgr.get_last_bias(symbol)
            if not bias:
                bias = await self.daily_bias_mgr.calculate_bias(symbol)

            # 3. Handle Open Positions (BIAS FLIP CLOSE)
            position = self.tracker.get_position(symbol)
            if position:
                # Close if bias is contrary
                should_close = False
                if position.side == PositionSide.LONG and bias.direction == DailyBiasDirection.BEARISH:
                    should_close = True
                elif position.side == PositionSide.SHORT and bias.direction == DailyBiasDirection.BULLISH:
                    should_close = True
                
                if should_close:
                    self._logger.info("bias_flip_closing", symbol=symbol, side=position.side.value, bias=bias.direction.value)
                    await self._close_position(symbol, reason="bias_flip")
                    continue

                # Regular management (SL/TP/Trailing)
                await self._manage_position(symbol, ticker)
                continue

            # 4. Entry Logic (Only if no position)
            # Circuit Breaker Check
            if not await self._is_trading_allowed(symbol): continue

            # Strategy Trigger (15m TK Cross)
            from src.models import OHLCV
            candle = OHLCV(ticker.timestamp, ticker.last, ticker.last, ticker.last, ticker.last, ticker.volume_24h)
            self.strategy.update_candle(symbol, candle, self.config.timeframe)
            
            signal = self.strategy.generate_signal(symbol)
            if signal.signal != Signal.NEUTRAL:
                await self._handle_mtf_signal(symbol, signal, ticker, bias)

    async def _sync_positions_with_exchange(self) -> None:
        """Startup sync: Ensure local PositionTracker matches Binance active positions."""
        self._logger.info("startup_position_sync_started")
        try:
            exchange_positions = await self.exchange.fetch_positions()
            local_positions = self.tracker.get_all_positions()
            local_symbols = {p.symbol for p in local_positions}
            exchange_symbols = {p["symbol"] for p in exchange_positions}

            # 1. Remove local positions that no longer exist on exchange
            for symbol in local_symbols:
                if symbol not in exchange_symbols:
                    self._logger.warning("sync_removing_ghost_position", symbol=symbol)
                    self.tracker.remove_position(symbol)

            # 2. Add/Update positions from exchange to local tracker
            # DISABLED: To prevent the bot from hijacking manual trades placed by the
            # user, we no longer adopt unknown positions. The bot will only manage
            # positions that it has explicitly opened and saved to its local SQLite db.
            """
            for p in exchange_positions:
                symbol = p["symbol"]
                side = PositionSide.LONG if float(p.get("entryPrice", 0)) > 0 and p.get("side") == "long" else PositionSide.SHORT
                if symbol not in local_symbols:
                    self._logger.info("sync_adding_found_position", symbol=symbol)
                    from src.models import Position
                    new_pos = Position(
                        symbol=symbol,
                        side=side,
                        entry_price=float(p.get("entryPrice", 0)),
                        amount=float(p.get("contracts", 0)),
                        leverage=int(p.get("leverage", 1)),
                        stop_loss=0.0,
                        trailing_stop=0.0,
                        take_profit=0.0,
                        opened_at=datetime.utcnow(),
                        updated_at=datetime.utcnow()
                    )
                    self.tracker.save_position(new_pos)
            """

            self._logger.info("startup_position_sync_completed", active=len(exchange_positions))
        except Exception as e:
            self._logger.error("startup_position_sync_failed", error=str(e))

    async def _handle_mtf_signal(self, symbol: str, signal: StrategySignal, ticker: Ticker, bias: DailyBiasResult) -> None:
        # Check Cooldown (at least 15m since last entry)
        last_entry = self._last_entry_time.get(symbol)
        if last_entry and (datetime.utcnow() - last_entry) < timedelta(minutes=15):
            return

        # Check Bias Direction Alignment
        if signal.signal in (Signal.BUY, Signal.STRONG_BUY) and bias.direction != DailyBiasDirection.BULLISH:
            return
        if signal.signal in (Signal.SELL, Signal.STRONG_SELL) and bias.direction != DailyBiasDirection.BEARISH:
            return

        # Sentiment check
        if self.config.sentiment.enabled:
            sentiment = await self.sentiment.get_sentiment()
            if sentiment.blocks_new_entries: return

        self._logger.info("mtf_signal_accepted", symbol=symbol, signal=signal.signal.value, bias=bias.direction.value)
        await self._open_mtf_position(symbol, signal, ticker, bias)

    async def _open_mtf_position(self, symbol: str, signal: StrategySignal, ticker: Ticker, bias: DailyBiasResult) -> None:
        try:
            # Safety: check if exchange already has a position for this symbol
            try:
                exchange_positions = await self.exchange.fetch_positions(symbol)
                if exchange_positions:
                    self._logger.warning("position_already_exists_on_exchange", symbol=symbol, count=len(exchange_positions))
                    return
            except Exception:
                pass  # If we can't check, proceed cautiously

            side = Side.BUY if signal.signal in (Signal.BUY, Signal.STRONG_BUY) else Side.SELL
            position_side = "long" if side == Side.BUY else "short"
            
            # Leverage & Margin Mode
            leverage = self.config.default_leverage
            await self.exchange.set_leverage(symbol, leverage, dry_run=self.dry_run, position_side=position_side)

            # Position Sizing
            balance = await self.exchange.fetch_balance()
            usdt = float(balance.get("USDT", {}).get("free", 0.0))
            
            size_result = self.risk.calculate_position_size(symbol, signal.entry_price, signal.stop_loss, usdt, leverage)
            
            final_amount = size_result.recommended_amount * bias.size_multiplier
            
            # Validate final amount against exchange min_notional before execution
            precision = await self.exchange.load_precision(symbol)
            if final_amount < precision.min_amount:
                self._logger.info(
                    "position_size_below_min",
                    symbol=symbol,
                    final_amount=final_amount,
                    min_amount=precision.min_amount,
                    reason="size_too_small_for_exchange"
                )
                return

            # --- LIMIT ENTRY (maker order, ~0.02% fee) ---
            # Offset 0.05% from mid-price to ensure we're maker
            offset_pct = 0.0005
            if side == Side.BUY:
                limit_price = round(signal.entry_price * (1 - offset_pct), precision.price_precision)
            else:
                limit_price = round(signal.entry_price * (1 + offset_pct), precision.price_precision)

            entry_order = OrderRequest(
                symbol=symbol,
                side=side,
                order_type=OrderType.LIMIT,
                amount=final_amount,
                price=limit_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                leverage=leverage,
            )
            result = await self.exchange.safe_order_execution(entry_order, dry_run=self.dry_run)

            # After successful LIMIT fill, post OCO TP+SL on Binance
            if result.filled and not self.dry_run and result.order_id:
                try:
                    oco_result = await self.exchange.create_oco_order(
                        symbol=symbol,
                        side=side,
                        amount=final_amount,
                        price=round(signal.take_profit, precision.price_precision),
                        stop_price=round(signal.stop_loss, precision.price_precision),
                    )
                    self._logger.info("oco_posted", symbol=symbol, oco_id=oco_result.order_id)
                except Exception as oco_err:
                    self._logger.warning("oco_failed_will_manage_locally", symbol=symbol, error=str(oco_err))

            # Tracking
            ts = self.risk.calculate_trailing_stop(signal.entry_price, ticker.last, position_side)
            from src.models import Position
            pos = Position(symbol, PositionSide.LONG if side == Side.BUY else PositionSide.SHORT, signal.entry_price, final_amount, leverage, signal.stop_loss, ts, signal.take_profit)
            self.tracker.save_position(pos)
            self._last_entry_time[symbol] = datetime.utcnow()
            
            self._logger.info("position_opened_mtf", symbol=symbol, amount=final_amount, multiplier=bias.size_multiplier, limit_price=limit_price)

        except Exception:
            self._logger.exception("mtf_open_failed", symbol=symbol)

    async def _is_trading_allowed(self, symbol: str) -> bool:
        balance_data = await self.exchange.fetch_balance()
        usdt_balance = float(balance_data.get("USDT", {}).get("free", 0.0))
        daily_pnl = self.tracker.get_daily_pnl()
        if not daily_pnl: return True
        return self.circuit_breaker.check(daily_pnl, usdt_balance, self.config.risk.max_daily_loss_pct)

    async def _manage_position(self, symbol: str, ticker: Ticker) -> None:
        position = self.tracker.get_position(symbol)
        if not position: return
        
        current_price = ticker.last
        new_ts = self.risk.calculate_trailing_stop(position.entry_price, current_price, position.side.value)
        
        # PnL update
        pnl_factor = (current_price - position.entry_price) / position.entry_price if position.side == PositionSide.LONG else (position.entry_price - current_price) / position.entry_price
        unrealized = pnl_factor * position.amount * position.leverage
        
        # SL/TP Check
        if (position.side == PositionSide.LONG and current_price <= position.trailing_stop) or (position.side == PositionSide.SHORT and current_price >= position.trailing_stop):
            await self._close_position(symbol, reason="stop_loss")
        elif (position.side == PositionSide.LONG and current_price >= position.take_profit) or (position.side == PositionSide.SHORT and current_price <= position.take_profit):
            await self._close_position(symbol, reason="take_profit")

    async def _close_position(self, symbol: str, reason: str) -> None:
        position = self.tracker.get_position(symbol)
        if not position: return
        close_side = Side.SELL if position.side == PositionSide.LONG else Side.BUY
        try:
            order = OrderRequest(symbol, close_side, OrderType.MARKET, position.amount, reduce_only=True)
            await self.exchange.safe_order_execution(order, dry_run=self.dry_run)
            self.tracker.remove_position(symbol)
            self._logger.info("position_closed", symbol=symbol, reason=reason)
        except Exception as e:
            error_msg = str(e).lower()
            if "2009" in error_msg or "nonexistent" in error_msg:
                self._logger.warning("ghost_position_cleared", symbol=symbol, reason="not_on_exchange")
                self.tracker.remove_position(symbol)
            else:
                self._logger.exception("close_failed", symbol=symbol)

    def _setup_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        def _h(s): self._running = False
        if sys.platform != "win32":
            for s in (signal.SIGTERM, signal.SIGINT): loop.add_signal_handler(s, _h, s)
        else: signal.signal(signal.SIGINT, lambda s, f: _h(signal.SIGINT))

async def run_bot() -> None:
    structlog.configure(processors=[structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()])
    try:
        config = ConfigManager.load().to_app_config()
        bot = TradingBot(config)
        await bot.start()
    except Exception:
        structlog.get_logger("run_bot").exception("fatal")
        raise
