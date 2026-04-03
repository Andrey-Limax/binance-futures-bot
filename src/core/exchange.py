"""Exchange interface wrapper for MEXC Futures via ccxt.pro.

Manages WebSocket connections for real-time market data,
order execution with retry logic, and asset precision handling.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, AsyncGenerator, Optional

import ccxt.pro as ccxt
import structlog

from src.models import (
    AssetPrecision,
    OHLCV,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Side,
    Ticker,
    OrderType,
)

logger = structlog.get_logger(__name__)


class ExchangeInterface:
    """Async wrapper around ccxt.pro for MEXC Futures.

    Responsibilities:
        - Manage authenticated connection to MEXC Futures
        - Subscribe to WebSocket ticker streams
        - Execute orders with retry logic for transient failures
        - Format amounts/prices according to exchange precision rules
        - Fetch historical OHLCV data for strategy warm-up
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._logger = logger.bind(component="ExchangeInterface")
        self._precision_cache: dict[str, AssetPrecision] = {}
        self._ws_running: bool = False
        self.exchange: Optional[ccxt.mexc] = None
        self._api_key = api_key
        self._api_secret = api_secret

    async def connect(self) -> None:
        """Establish connection to MEXC Futures and load markets."""
        if self.exchange is not None:
            try:
                await self.exchange.close()
            except Exception:
                pass
            self.exchange = None

        try:
            self.exchange = ccxt.mexc(
                {
                    "apiKey": self._api_key,
                    "secret": self._api_secret,
                    "options": {
                        "defaultType": "swap",
                    },
                }
            )
            
            # CCXT has a hardcoded block for major pairs (BTC/ETH/LTC) on MEXC.
            # We clear it explicitly to allow trading these pairs.
            self.exchange.options["unavailableContracts"] = {}

            # Load markets with retry for stability
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    await self.exchange.load_markets()
                    self._ws_running = True
                    self._logger.info(
                        "exchange_connected",
                        markets=len(self.exchange.markets),
                    )
                    return
                except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
                    if attempt == max_retries:
                        raise
                    self._logger.warning("exchange_load_markets_retry", attempt=attempt, error=str(exc))
                    await asyncio.sleep(2**attempt)
        except Exception as exc:
            self._logger.error("exchange_connection_failed", error=str(exc))
            if self.exchange is not None:
                await self.exchange.close()
                self.exchange = None
            raise

    async def disconnect(self) -> None:
        """Gracefully close all connections."""
        self._ws_running = False
        if self.exchange is not None:
            try:
                await self.exchange.close()
                self._logger.info("exchange_disconnected")
            except Exception as exc:
                self._logger.warning("disconnect_error", error=str(exc))
            finally:
                self.exchange = None

    def safe_float(self, value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    async def load_precision(self, symbol: str) -> AssetPrecision:
        """Load and cache tick size / lot size rules for a symbol."""
        if symbol in self._precision_cache:
            return self._precision_cache[symbol]

        if self.exchange is None:
            raise RuntimeError("Exchange not connected.")

        market = self.exchange.market(symbol)
        precision_info = market.get("precision", {})
        price_prec = precision_info.get("price", 8)
        amount_prec = precision_info.get("amount", 8)

        # Convert decimal-places integers to step-size floats
        if isinstance(price_prec, int):
            tick_size = float(Decimal("1").scaleb(-price_prec))
        else:
            tick_size = float(price_prec)

        if isinstance(amount_prec, int):
            lot_size = float(Decimal("1").scaleb(-amount_prec))
        else:
            lot_size = float(amount_prec)

        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}

        asset_precision = AssetPrecision(
            symbol=symbol,
            price_precision=price_prec if isinstance(price_prec, int) else 8,
            amount_precision=amount_prec if isinstance(amount_prec, int) else 8,
            tick_size=tick_size,
            lot_size=lot_size,
            min_amount=float(amount_limits.get("min") or 0.0),
            max_amount=float(amount_limits.get("max") or float("inf")),
            min_cost=float(cost_limits.get("min") or 0.0),
            contract_size=float(market.get("contractSize", 1.0)),
        )

        self._precision_cache[symbol] = asset_precision
        return asset_precision

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 100,
    ) -> list[OHLCV]:
        if self.exchange is None:
            raise RuntimeError("Exchange not connected.")
        raw_candles = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [
            OHLCV(
                timestamp=datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc),
                open=float(c[1]),
                high=float(c[2]),
                low=float(c[3]),
                close=float(c[4]),
                volume=float(c[5]),
            )
            for c in raw_candles
        ]

    async def watch_ticker(self, symbol: str) -> AsyncGenerator[Ticker, None]:
        if self.exchange is None:
            raise RuntimeError("Exchange not connected.")
        
        while self._ws_running:
            try:
                raw_ticker = await self.exchange.watch_ticker(symbol)
                yield Ticker(
                    symbol=symbol,
                    bid=self.safe_float(raw_ticker.get("bid")),
                    ask=self.safe_float(raw_ticker.get("ask")),
                    last=self.safe_float(raw_ticker.get("last")),
                    volume_24h=self.safe_float(raw_ticker.get("baseVolume")),
                    timestamp=datetime.fromtimestamp(raw_ticker["timestamp"] / 1000, tz=timezone.utc)
                    if raw_ticker.get("timestamp") else datetime.now(timezone.utc),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning("ticker_error", symbol=symbol, error=str(exc))
                await asyncio.sleep(1)

    async def safe_order_execution(
        self,
        request: OrderRequest,
        dry_run: bool = False,
    ) -> OrderResult:
        if dry_run:
            self._logger.info("dry_run_order", symbol=request.symbol, side=request.side.value, amount=request.amount)
            return OrderResult(
                order_id=f"DRY_RUN_{datetime.now(timezone.utc).timestamp()}",
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                status=OrderStatus.CLOSED,
                amount=request.amount,
                filled=request.amount,
                remaining=0.0,
                price=request.price,
                average_fill_price=request.price,
                fee=0.0,
                timestamp=datetime.now(timezone.utc),
            )

        if self.exchange is None:
            raise RuntimeError("Exchange not connected.")

        formatted_amount = self.format_amount(request.symbol, request.amount)
        formatted_price = self.format_price(request.symbol, request.price) if request.price else None

        max_retries = 3
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                params = {}
                if request.reduce_only: params["reduceOnly"] = True
                
                # Use a specific bot tag for isolation if not provided
                client_id = request.client_order_id or f"bot_{int(datetime.now().timestamp())}"
                params["clientOrderId"] = client_id

                raw_order = await self.exchange.create_order(
                    symbol=request.symbol,
                    type=request.order_type.value,
                    side=request.side.value,
                    amount=formatted_amount,
                    price=formatted_price,
                    params=params,
                )

                result = self._parse_order_result(raw_order, request)
                self._logger.info("order_executed", order_id=result.order_id, amount=formatted_amount)
                return result

            except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
                last_error = exc
                await asyncio.sleep(2 ** attempt)
            except Exception as exc:
                self._logger.error("order_failed", symbol=request.symbol, error=str(exc))
                raise ExchangeExecutionError(str(exc), request) from exc

        raise MaxRetriesExceededError(str(last_error), max_retries)

    def _parse_order_result(self, raw_order: dict[str, Any], request: OrderRequest) -> OrderResult:
        timestamp_ms = raw_order.get("timestamp")
        precision = self._precision_cache.get(raw_order.get("symbol", request.symbol))
        contract_size = precision.contract_size if precision else 1.0

        return OrderResult(
            order_id=str(raw_order.get("id", "")),
            client_order_id=raw_order.get("clientOrderId"),
            symbol=raw_order.get("symbol", request.symbol),
            side=Side(raw_order.get("side", request.side.value)),
            order_type=request.order_type,
            status=OrderStatus(raw_order.get("status", OrderStatus.CLOSED.value)),
            # Convert back from contracts to base currency
            amount=float(raw_order.get("amount", 0.0)) * contract_size,
            filled=float(raw_order.get("filled", 0.0)) * contract_size,
            remaining=float(raw_order.get("remaining", 0.0)) * contract_size,
            price=float(raw_order.get("price")) if raw_order.get("price") else None,
            average_fill_price=float(raw_order.get("average")) if raw_order.get("average") else None,
            fee=float(raw_order["fee"]["cost"]) if raw_order.get("fee") and raw_order["fee"].get("cost") else 0.0,
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc) if timestamp_ms else datetime.now(timezone.utc),
        )

    def format_amount(self, symbol: str, amount: float) -> float:
        precision = self._precision_cache[symbol]
        lot_size = Decimal(str(precision.lot_size))
        contract_size = Decimal(str(precision.contract_size))
        
        # Convert base (ETH) to contracts
        contract_units = Decimal(str(amount)) / contract_size
        
        # Round to lot size
        adjusted = (contract_units / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size
        return float(adjusted)

    def format_price(self, symbol: str, price: float) -> float:
        precision = self._precision_cache[symbol]
        tick_size = Decimal(str(precision.tick_size))
        adjusted = (Decimal(str(price)) / tick_size).to_integral_value() * tick_size
        return float(adjusted)

    async def fetch_balance(self) -> dict[str, Any]:
        if self.exchange is None: raise RuntimeError("Not connected")
        return await self.exchange.fetch_balance()

    async def fetch_positions(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        if self.exchange is None: raise RuntimeError("Not connected")
        positions = await self.exchange.fetch_positions([symbol] if symbol else None)
        return [p for p in positions if float(p.get("contracts", 0)) != 0]

    async def set_leverage(self, symbol: str, leverage: int, dry_run: bool = False, position_side: str = "long") -> None:
        if dry_run: return
        if self.exchange is None: raise RuntimeError("Not connected")
        params = {"openType": 2, "positionType": 1 if position_side == "long" else 2}
        try:
            await self.exchange.set_leverage(leverage, symbol, params=params)
        except Exception as e:
            if "2044" not in str(e): raise

    async def cancel_all_orders(self, symbol: str) -> list[dict]:
        if self.exchange is None: raise RuntimeError("Not connected")
        try:
            return await self.exchange.cancel_all_orders(symbol)
        except Exception:
            return []


class ExchangeExecutionError(Exception):
    def __init__(self, message: str, order_request: OrderRequest) -> None:
        self.order_request = order_request
        super().__init__(message)


class MaxRetriesExceededError(Exception):
    def __init__(self, message: str, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(message)
