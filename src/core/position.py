"""Position Tracker: Persistent state management for open positions.

Saves and restores position state using TinyDB (JSON file).
If the container crashes, the bot can resume monitoring
trailing stops immediately on restart.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog
from tinydb import TinyDB, Query

from src.models import Position, PositionSide, DailyPnLSnapshot

logger = structlog.get_logger(__name__)

DEFAULT_STATE_DIR = Path("data/state")
DEFAULT_POSITIONS_FILE = "positions.json"
DEFAULT_PNL_FILE = "daily_pnl.json"


class PositionTracker:
    """Persistent position state manager using TinyDB.

    Responsibilities:
        - Save/load open positions to/from JSON file
        - Track daily PnL for circuit breaker
        - Provide fast lookup of positions by symbol
        - Support graceful recovery after container restart

    Attributes:
        _db: TinyDB instance for positions.
        _pnl_db: TinyDB instance for daily PnL tracking.
        _state_dir: Directory for state files.
    """

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        """Initialize position tracker.

        Args:
            state_dir: Directory for persistence files. Defaults to data/state/.
        """
        self._logger = logger.bind(component="PositionTracker")
        self._state_dir = state_dir or DEFAULT_STATE_DIR
        self._db: Optional[TinyDB] = None
        self._pnl_db: Optional[TinyDB] = None

    def open(self) -> None:
        """Open database connections and ensure state directory exists.

        Creates TinyDB instances for positions and daily PnL.
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)

        positions_path = self._state_dir / DEFAULT_POSITIONS_FILE
        pnl_path = self._state_dir / DEFAULT_PNL_FILE

        self._db = TinyDB(str(positions_path))
        self._pnl_db = TinyDB(str(pnl_path))

        self._logger.info(
            "position_tracker_opened",
            state_dir=str(self._state_dir),
            positions_file=str(positions_path),
            pnl_file=str(pnl_path),
        )

    def close(self) -> None:
        """Close database connections gracefully.

        Should be called during shutdown to ensure data is flushed.
        """
        if self._db is not None:
            self._db.close()
            self._db = None

        if self._pnl_db is not None:
            self._pnl_db.close()
            self._pnl_db = None

        self._logger.info("position_tracker_closed")

    # ── Position CRUD ──────────────────────────────────────────────────────

    def save_position(self, position: Position) -> None:
        """Save or update a position.

        If a position with the same symbol already exists, update it.
        Otherwise, insert a new record.

        Args:
            position: Position to persist.
        """
        if self._db is None:
            raise RuntimeError("PositionTracker is not open. Call open() first.")

        data = self._position_to_dict(position)
        PositionQuery = Query()

        existing = self._db.search(PositionQuery.symbol == position.symbol)
        if existing:
            self._db.update(data, PositionQuery.symbol == position.symbol)
            self._logger.debug("position_updated", symbol=position.symbol)
        else:
            self._db.insert(data)
            self._logger.debug("position_inserted", symbol=position.symbol)

    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position by symbol.

        Args:
            symbol: Trading symbol to look up.

        Returns:
            Position if found, None otherwise.
        """
        if self._db is None:
            raise RuntimeError("PositionTracker is not open. Call open() first.")

        PositionQuery = Query()
        results = self._db.search(PositionQuery.symbol == symbol)

        if not results:
            return None

        return self._dict_to_position(results[0])

    def get_all_positions(self) -> list[Position]:
        """Get all open positions.

        Returns:
            List of all tracked positions.
        """
        if self._db is None:
            raise RuntimeError("PositionTracker is not open. Call open() first.")

        return [self._dict_to_position(doc) for doc in self._db.all()]

    def remove_position(self, symbol: str) -> bool:
        """Remove a closed position from tracking.

        Args:
            symbol: Trading symbol to remove.

        Returns:
            True if position was found and removed, False if not found.
        """
        if self._db is None:
            raise RuntimeError("PositionTracker is not open. Call open() first.")

        PositionQuery = Query()
        existing = self._db.search(PositionQuery.symbol == symbol)

        if not existing:
            self._logger.warning("remove_position_not_found", symbol=symbol)
            return False

        removed_ids = self._db.remove(PositionQuery.symbol == symbol)
        self._logger.info("position_removed", symbol=symbol, doc_ids=removed_ids)
        return len(removed_ids) > 0

    def has_open_positions(self) -> bool:
        """Check if there are any open positions.

        Returns:
            True if at least one position exists.
        """
        if self._db is None:
            raise RuntimeError("PositionTracker is not open. Call open() first.")

        return len(self._db) > 0

    # ── Daily PnL Tracking ─────────────────────────────────────────────────

    def update_daily_pnl(
        self,
        realized_pnl: float,
        unrealized_pnl: float,
        is_winner: Optional[bool] = None,
    ) -> DailyPnLSnapshot:
        """Update today's PnL snapshot.

        Tracks realized and unrealized PnL, win/loss count,
        and consecutive losses for circuit breaker.

        Args:
            realized_pnl: Realized PnL from closed trades today.
            unrealized_pnl: Current unrealized PnL from open positions.
            is_winner: Whether the last closed trade was a winner.

        Returns:
            Updated DailyPnLSnapshot.
        """
        if self._pnl_db is None:
            raise RuntimeError("PositionTracker is not open. Call open() first.")

        today = datetime.utcnow().strftime("%Y-%m-%d")
        PnLQuery = Query()
        existing = self._pnl_db.search(PnLQuery.date == today)

        if existing:
            record = existing[0]
            trades_count = record["trades_count"]
            winning_trades = record["winning_trades"]
            losing_trades = record["losing_trades"]
            consecutive_losses = record["consecutive_losses"]

            if is_winner is not None:
                trades_count += 1
                if is_winner:
                    winning_trades += 1
                    consecutive_losses = 0
                else:
                    losing_trades += 1
                    consecutive_losses += 1

            snapshot = DailyPnLSnapshot(
                date=today,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                total_pnl=realized_pnl + unrealized_pnl,
                trades_count=trades_count,
                winning_trades=winning_trades,
                losing_trades=losing_trades,
                consecutive_losses=consecutive_losses,
            )

            self._pnl_db.update(self._pnl_to_dict(snapshot), PnLQuery.date == today)
        else:
            trades_count = 1 if is_winner is not None else 0
            winning_trades = 1 if is_winner is True else 0
            losing_trades = 1 if is_winner is False else 0
            consecutive_losses = 0 if is_winner is not False else (1 if is_winner is not None else 0)

            snapshot = DailyPnLSnapshot(
                date=today,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                total_pnl=realized_pnl + unrealized_pnl,
                trades_count=trades_count,
                winning_trades=winning_trades,
                losing_trades=losing_trades,
                consecutive_losses=consecutive_losses,
            )

            self._pnl_db.insert(self._pnl_to_dict(snapshot))

        self._logger.debug(
            "daily_pnl_updated",
            date=today,
            total_pnl=snapshot.total_pnl,
            consecutive_losses=snapshot.consecutive_losses,
        )

        return snapshot

    def get_daily_pnl(self, date: Optional[str] = None) -> Optional[DailyPnLSnapshot]:
        """Get PnL snapshot for a specific date.

        Args:
            date: Date string in YYYY-MM-DD format. Defaults to today.

        Returns:
            DailyPnLSnapshot if found, None otherwise.
        """
        if self._pnl_db is None:
            raise RuntimeError("PositionTracker is not open. Call open() first.")

        target_date = date or datetime.utcnow().strftime("%Y-%m-%d")
        PnLQuery = Query()
        results = self._pnl_db.search(PnLQuery.date == target_date)

        if not results:
            return None

        return self._dict_to_pnl(results[0])

    def get_consecutive_losses(self) -> int:
        """Get current consecutive loss count.

        Returns:
            Number of consecutive losing trades today.
        """
        snapshot = self.get_daily_pnl()
        if snapshot is None:
            return 0
        return snapshot.consecutive_losses

    # ── Serialization ──────────────────────────────────────────────────────

    def _position_to_dict(self, position: Position) -> dict:
        """Serialize Position to dict for TinyDB storage.

        Converts datetime objects to ISO format strings.

        Args:
            position: Position to serialize.

        Returns:
            Dictionary suitable for TinyDB storage.
        """
        return {
            "symbol": position.symbol,
            "side": position.side.value,
            "entry_price": position.entry_price,
            "amount": position.amount,
            "leverage": position.leverage,
            "stop_loss": position.stop_loss,
            "trailing_stop": position.trailing_stop,
            "take_profit": position.take_profit,
            "unrealized_pnl": position.unrealized_pnl,
            "realized_pnl": position.realized_pnl,
            "opened_at": position.opened_at.isoformat(),
            "updated_at": position.updated_at.isoformat(),
        }

    def _dict_to_position(self, data: dict) -> Position:
        """Deserialize dict from TinyDB to Position.

        Converts ISO format strings back to datetime objects.

        Args:
            data: Dictionary from TinyDB.

        Returns:
            Reconstructed Position.
        """
        return Position(
            symbol=data["symbol"],
            side=PositionSide(data["side"]),
            entry_price=data["entry_price"],
            amount=data["amount"],
            leverage=data["leverage"],
            stop_loss=data["stop_loss"],
            trailing_stop=data["trailing_stop"],
            take_profit=data["take_profit"],
            unrealized_pnl=data["unrealized_pnl"],
            realized_pnl=data["realized_pnl"],
            opened_at=datetime.fromisoformat(data["opened_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    @staticmethod
    def _pnl_to_dict(snapshot: DailyPnLSnapshot) -> dict:
        """Serialize DailyPnLSnapshot to dict for TinyDB.

        Args:
            snapshot: Snapshot to serialize.

        Returns:
            Dictionary suitable for TinyDB storage.
        """
        return {
            "date": snapshot.date,
            "realized_pnl": snapshot.realized_pnl,
            "unrealized_pnl": snapshot.unrealized_pnl,
            "total_pnl": snapshot.total_pnl,
            "trades_count": snapshot.trades_count,
            "winning_trades": snapshot.winning_trades,
            "losing_trades": snapshot.losing_trades,
            "consecutive_losses": snapshot.consecutive_losses,
        }

    @staticmethod
    def _dict_to_pnl(data: dict) -> DailyPnLSnapshot:
        """Deserialize dict from TinyDB to DailyPnLSnapshot.

        Args:
            data: Dictionary from TinyDB.

        Returns:
            Reconstructed DailyPnLSnapshot.
        """
        return DailyPnLSnapshot(
            date=data["date"],
            realized_pnl=data["realized_pnl"],
            unrealized_pnl=data["unrealized_pnl"],
            total_pnl=data["total_pnl"],
            trades_count=data["trades_count"],
            winning_trades=data["winning_trades"],
            losing_trades=data["losing_trades"],
            consecutive_losses=data["consecutive_losses"],
        )
