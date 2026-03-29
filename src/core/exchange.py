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

    Attributes:
        exchange: ccxt.pro.mexc instance (async)
        _precision_cache: Cache of AssetPrecision per symbol
        _ws_running: Flag to control WebSocket lifecycle
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        sandbox: bool = False,
    ) -> None:
        """Initialize the exchange interface.

        Args:
            api_key: MEXC API key.
            api_secret: MEXC API secret.
            sandbox: If True, use MEXC testnet.
        """
        self._logger = logger.bind(component="ExchangeInterface")
        self._precision_cache: dict[str, AssetPrecision] = {}
        self._ws_running: bool = False
        self.exchange: Optional[ccxt.mexc] = None
        self._api_key = api_key
        self._api_secret = api_secret
        self._sandbox = sandbox or os.getenv("TESTNET", "false").lower() == "true"

    async def connect(self) -> None:
        """Establish connection to MEXC Futures and load markets.

        Initializes the ccxt.pro.mexc instance, sets required options
        (defaultType='swap' for USDT-M futures), and loads market data.

        Raises:
            ccxt.NetworkError: If connection to MEXC fails.
            ccxt.ExchangeError: If markets cannot be loaded.
        """
        # Close previous exchange instance if reconnecting
        if self.exchange is not None:
            try:
                await self.exchange.close()
            except Exception:
                pass
            self.exchange = None

        self.exchange = ccxt.mexc(
            {
                "apiKey": self._api_key,
                "secret": self._api_secret,
                "options": {
                    "defaultType": "swap",
                },
            }
        )

        if self._sandbox:
            self._logger.warning(
                "testnet_not_supported",
                message="MEXC does not support testnet via API. Running in LIVE mode with real funds. Use small amounts for testing.",
            )

        await self.exchange.load_markets()
        self._ws_running = True
        self._logger.info(
            "exchange_connected",
            sandbox=self._sandbox,
            markets=len(self.exchange.markets),
        )

    async def disconnect(self) -> None:
        """Gracefully close all connections.

        Closes WebSocket streams and the REST API connection.
        Should be called during shutdown to free resources.
        """
        self._ws_running = False

        if self.exchange is not None:
            try:
                await self.exchange.close()
                self._logger.info("exchange_disconnected")
            except Exception as exc:
                self._logger.warning(
                    "disconnect_error",
                    error=str(exc),
                )
            finally:
                self.exchange = None

    # ── Market Data ────────────────────────────────────────────────────────

    async def load_precision(self, symbol: str) -> AssetPrecision:
        """Load and cache tick size / lot size rules for a symbol.

        Fetches market info from MEXC and extracts precision rules.
        Results are cached to avoid repeated API calls.

        Args:
            symbol: Trading symbol in CCXT format (e.g., 'BTC/USDT:USDT').

        Returns:
            AssetPrecision with tick_size, lot_size, min/max amounts.
        """
        if symbol in self._precision_cache:
            return self._precision_cache[symbol]

        if self.exchange is None:
            raise RuntimeError("Exchange not connected. Call connect() first.")

        market = self.exchange.market(symbol)

        # Extract precision from market info.
        # CCXT standardises these under market['precision']['amount'] and
        # market['precision']['price'].  Some exchanges report the number of
        # decimal places (int) while others report the step size (float).
        precision_info = market.get("precision", {})
        price_prec = precision_info.get("price", 8)
        amount_prec = precision_info.get("amount", 8)

        # Convert decimal-places integers to step-size floats when needed.
        if isinstance(price_prec, int):
            tick_size = float(Decimal("1").scaleb(-price_prec))
        else:
            tick_size = float(price_prec)

        if isinstance(amount_prec, int):
            lot_size = float(Decimal("1").scaleb(-amount_prec))
        else:
            lot_size = float(amount_prec)

        limits = market.get("limits", {})
        amount_limits = limits.get("amount", {})
        cost_limits = limits.get("cost", {})

        asset_precision = AssetPrecision(
            symbol=symbol,
            price_precision=price_prec if isinstance(price_prec, int) else len(str(tick_size).split(".")[1]) if "." in str(tick_size) else 0,
            amount_precision=amount_prec if isinstance(amount_prec, int) else len(str(lot_size).split(".")[1]) if "." in str(lot_size) else 0,
            tick_size=tick_size,
            lot_size=lot_size,
            min_amount=float(amount_limits.get("min", 0.0)),
            max_amount=float(amount_limits.get("max", float("inf"))),
            min_cost=float(cost_limits.get("min", 0.0)),
        )

        self._precision_cache[symbol] = asset_precision
        self._logger.info(
            "precision_loaded",
            symbol=symbol,
            tick_size=tick_size,
            lot_size=lot_size,
        )
        return asset_precision

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 100,
    ) -> list[OHLCV]:
        """Fetch historical OHLCV candles for strategy warm-up.

        Downloads the last `limit` candles to populate indicators
        (Ichimoku, MACD, RSI) before live trading begins.

        Args:
            symbol: Trading symbol.
            timeframe: Candle timeframe (e.g., '1m', '5m', '1h').
            limit: Number of candles to fetch.

        Returns:
            List of OHLCV candles sorted oldest to newest.
        """
        if self.exchange is None:
            raise RuntimeError("Exchange not connected. Call connect() first.")

        raw_candles: list[list] = await self.exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
        )

        candles: list[OHLCV] = []
        for entry in raw_candles:
            # Each entry is [timestamp, open, high, low, close, volume]
            candles.append(
                OHLCV(
                    timestamp=datetime.fromtimestamp(entry[0] / 1000, tz=timezone.utc),
                    open=float(entry[1]),
                    high=float(entry[2]),
                    low=float(entry[3]),
                    close=float(entry[4]),
                    volume=float(entry[5]),
                )
            )

        self._logger.debug(
            "ohlcv_fetched",
            symbol=symbol,
            timeframe=timeframe,
            count=len(candles),
        )
        return candles

    async def watch_ticker(self, symbol: str) -> AsyncGenerator[Ticker, None]:
        """Subscribe to real-time ticker via WebSocket.

        Yields Ticker snapshots as they arrive from the MEXC WebSocket.
        Manages reconnection automatically.

        Args:
            symbol: Trading symbol to watch.

        Yields:
            Ticker with current bid/ask/last prices.

        Raises:
            asyncio.CancelledError: When WebSocket is intentionally stopped.
        """
        if self.exchange is None:
            raise RuntimeError("Exchange not connected. Call connect() first.")

        self._logger.info("ticker_subscribed", symbol=symbol)

        while self._ws_running:
            try:
                raw_ticker = await self.exchange.watch_ticker(symbol)

                ticker = Ticker(
                    symbol=symbol,
                    bid=float(raw_ticker.get("bid", 0.0)),
                    ask=float(raw_ticker.get("ask", 0.0)),
                    last=float(raw_ticker.get("last", 0.0)),
                    volume_24h=float(raw_ticker.get("baseVolume", 0.0)),
                    timestamp=datetime.fromtimestamp(
                        raw_ticker["timestamp"] / 1000, tz=timezone.utc
                    )
                    if raw_ticker.get("timestamp")
                    else datetime.now(timezone.utc),
                )

                yield ticker

            except asyncio.CancelledError:
                self._logger.info("ticker_unsubscribed", symbol=symbol)
                raise
            except ccxt.NetworkError as exc:
                self._logger.warning(
                    "ticker_network_error",
                    symbol=symbol,
                    error=str(exc),
                )
                await asyncio.sleep(1)
            except ccxt.ExchangeError as exc:
                self._logger.error(
                    "ticker_exchange_error",
                    symbol=symbol,
                    error=str(exc),
                )
                raise

    # ── Order Execution ────────────────────────────────────────────────────

    async def safe_order_execution(
        self,
        request: OrderRequest,
        dry_run: bool = False,
    ) -> OrderResult:
        """Execute an order with error-specific retry logic.

        Error handling strategy:
            - ccxt.NetworkError: Retry up to 3 times with exponential backoff.
            - ccxt.ExchangeError: Abort immediately, log error details.
            - ccxt.InvalidOrder: Abort, log order validation failure.

        Before execution, amounts are formatted via `format_amount()`.

        Args:
            request: Validated OrderRequest with symbol, side, amount, etc.
            dry_run: If True, simulate the order without executing on exchange.

        Returns:
            OrderResult with execution details.

        Raises:
            ExchangeExecutionError: For non-retryable exchange errors.
            MaxRetriesExceededError: After exhausting network retries.
        """
        if dry_run:
            self._logger.info(
                "dry_run_order_skipped",
                symbol=request.symbol,
                side=request.side.value,
                amount=request.amount,
                leverage=request.leverage,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
            )
            return OrderResult(
                order_id=f"DRY_RUN_{datetime.now(timezone.utc).timestamp()}",
                client_order_id=None,
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
            raise RuntimeError("Exchange not connected. Call connect() first.")

        formatted_amount = self.format_amount(request.symbol, request.amount)
        formatted_price = (
            self.format_price(request.symbol, request.price)
            if request.price is not None
            else None
        )

        max_retries = 3
        last_error: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            try:
                params: dict[str, Any] = {}
                if request.reduce_only:
                    params["reduceOnly"] = True
                if request.client_order_id:
                    params["clientOrderId"] = request.client_order_id

                order_type = request.order_type.value
                side = request.side.value

                raw_order = await self.exchange.create_order(
                    symbol=request.symbol,
                    type=order_type,
                    side=side,
                    amount=formatted_amount,
                    price=formatted_price,
                    params=params,
                )

                result = self._parse_order_result(raw_order, request)
                self._logger.info(
                    "order_executed",
                    order_id=result.order_id,
                    symbol=request.symbol,
                    side=side,
                    amount=formatted_amount,
                    status=result.status.value,
                )
                return result

            except ccxt.NetworkError as exc:
                last_error = exc
                backoff = 2 ** (attempt - 1)
                self._logger.warning(
                    "order_network_retry",
                    attempt=attempt,
                    max_retries=max_retries,
                    backoff=backoff,
                    error=str(exc),
                )
                await asyncio.sleep(backoff)

            except ccxt.InvalidOrder as exc:
                self._logger.error(
                    "order_invalid",
                    symbol=request.symbol,
                    error=str(exc),
                )
                raise ExchangeExecutionError(
                    f"Invalid order for {request.symbol}: {exc}",
                    order_request=request,
                ) from exc

            except ccxt.ExchangeError as exc:
                self._logger.error(
                    "order_exchange_error",
                    symbol=request.symbol,
                    error=str(exc),
                )
                raise ExchangeExecutionError(
                    f"Exchange error for {request.symbol}: {exc}",
                    order_request=request,
                ) from exc

        raise MaxRetriesExceededError(
            f"Order execution failed after {max_retries} attempts: {last_error}",
            attempts=max_retries,
        )

    def _parse_order_result(
        self, raw_order: dict[str, Any], request: OrderRequest
    ) -> OrderResult:
        """Convert raw ccxt order dict into an OrderResult dataclass.

        Args:
            raw_order: Raw order response from ccxt.
            request: Original order request for context.

        Returns:
            Parsed OrderResult.
        """
        timestamp_ms = raw_order.get("timestamp")
        return OrderResult(
            order_id=str(raw_order.get("id", "")),
            client_order_id=raw_order.get("clientOrderId"),
            symbol=raw_order.get("symbol", request.symbol),
            side=Side(raw_order.get("side", request.side.value)),
            order_type=request.order_type,
            status=OrderStatus(raw_order.get("status", OrderStatus.PENDING.value)),
            amount=float(raw_order.get("amount", request.amount)),
            filled=float(raw_order.get("filled", 0.0)),
            remaining=float(raw_order.get("remaining", 0.0)),
            price=float(raw_order["price"]) if raw_order.get("price") else None,
            average_fill_price=(
                float(raw_order["average"])
                if raw_order.get("average")
                else None
            ),
            fee=float(raw_order["fee"]["cost"])
            if raw_order.get("fee") and raw_order["fee"].get("cost")
            else None,
            timestamp=(
                datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                if timestamp_ms
                else datetime.now(timezone.utc)
            ),
        )

    def format_amount(self, symbol: str, amount: float) -> float:
        """Adjust amount to comply with exchange lot size rules.

        Rounds down to the nearest valid lot size increment.
        Uses cached AssetPrecision for the symbol.

        Args:
            symbol: Trading symbol.
            amount: Desired amount.

        Returns:
            Adjusted amount that complies with exchange rules.

        Raises:
            KeyError: If precision for symbol hasn't been loaded yet.
        """
        if symbol not in self._precision_cache:
            raise KeyError(
                f"Precision for {symbol} not loaded. Call load_precision() first."
            )

        precision = self._precision_cache[symbol]
        lot_size = Decimal(str(precision.lot_size))
        amount_d = Decimal(str(amount))

        # Round down to the nearest lot size increment
        adjusted = (amount_d / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size

        return float(adjusted)

    def format_price(self, symbol: str, price: float) -> float:
        """Adjust price to comply with exchange tick size rules.

        Rounds to the nearest valid tick size increment.

        Args:
            symbol: Trading symbol.
            price: Desired price.

        Returns:
            Adjusted price that complies with exchange rules.
        """
        if symbol not in self._precision_cache:
            raise KeyError(
                f"Precision for {symbol} not loaded. Call load_precision() first."
            )

        precision = self._precision_cache[symbol]
        tick_size = Decimal(str(precision.tick_size))
        price_d = Decimal(str(price))

        # Round down to the nearest tick size
        adjusted = (price_d / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size

        return float(adjusted)

    # ── Account ────────────────────────────────────────────────────────────

    async def fetch_balance(self) -> dict[str, Any]:
        """Fetch futures account balance.

        Returns:
            Dict with balance info including 'USDT' free/used/total.
        """
        if self.exchange is None:
            raise RuntimeError("Exchange not connected. Call connect() first.")

        balance = await self.exchange.fetch_balance()
        return balance

    async def fetch_positions(
        self,
        symbol: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch open positions.

        Args:
            symbol: Optional filter for specific symbol.

        Returns:
            List of position dicts from exchange.
        """
        if self.exchange is None:
            raise RuntimeError("Exchange not connected. Call connect() first.")

        symbols = [symbol] if symbol else None
        positions = await self.exchange.fetch_positions(symbols=symbols)

        # Filter out zero-size positions that exchanges sometimes return
        active_positions = [
            p for p in positions
            if float(p.get("contracts", 0)) != 0
            or float(p.get("size", 0)) != 0
            or abs(float(p.get("notional", 0) or 0)) > 0
        ]

        return active_positions

    async def set_leverage(self, symbol: str, leverage: int, dry_run: bool = False) -> None:
        """Set leverage for a symbol.

        Args:
            symbol: Trading symbol.
            leverage: Leverage multiplier (1-125).
            dry_run: If True, skip the actual API call.
        """
        if dry_run:
            self._logger.info(
                "dry_run_leverage_skipped",
                symbol=symbol,
                leverage=leverage,
            )
            return

        if self.exchange is None:
            raise RuntimeError("Exchange not connected. Call connect() first.")

        await self.exchange.set_leverage(leverage, symbol)
        self._logger.info(
            "leverage_set",
            symbol=symbol,
            leverage=leverage,
        )

    async def cancel_all_orders(self, symbol: str) -> list[dict]:
        """Cancel all open orders for a symbol.

        Used during graceful shutdown to flatten pending orders.

        Args:
            symbol: Trading symbol.

        Returns:
            List of canceled order dicts.
        """
        if self.exchange is None:
            raise RuntimeError("Exchange not connected. Call connect() first.")

        try:
            canceled = await self.exchange.cancel_all_orders(symbol)
            self._logger.info(
                "orders_canceled",
                symbol=symbol,
                count=len(canceled),
            )
            return canceled
        except ccxt.ExchangeError as exc:
            # Some exchanges return errors when there are no open orders
            self._logger.debug(
                "cancel_all_noop",
                symbol=symbol,
                error=str(exc),
            )
            return []


class ExchangeExecutionError(Exception):
    """Raised when order execution fails due to exchange-side errors."""

    def __init__(self, message: str, order_request: OrderRequest) -> None:
        self.order_request = order_request
        super().__init__(message)


class MaxRetriesExceededError(Exception):
    """Raised when network retries are exhausted."""

    def __init__(self, message: str, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(message)
