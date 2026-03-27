"""Circuit Breaker: Daily PnL kill switch.

Monitors daily loss and triggers a trading halt when
pre-configured thresholds are breached. Prevents
catastrophic losses during adverse market conditions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import structlog

from src.models import CircuitBreakerConfig, DailyPnLSnapshot

logger = structlog.get_logger(__name__)


class CircuitBreakerState:
    """Possible circuit breaker states."""

    NORMAL = "normal"
    TRIPPED = "tripped"
    COOLDOWN = "cooldown"


class CircuitBreaker:
    """Daily loss limit and consecutive loss kill switch.

    Responsibilities:
        - Monitor daily PnL against max_daily_loss_pct
        - Track consecutive losses and trigger on threshold
        - Enforce cooldown period after tripping
        - Provide state query for main loop to block trading

    Attributes:
        config: Circuit breaker thresholds.
        _state: Current breaker state.
        _tripped_at: When the breaker was tripped.
        _cooldown_until: When trading can resume.
    """

    def __init__(self, config: CircuitBreakerConfig) -> None:
        """Initialize circuit breaker.

        Args:
            config: Thresholds for consecutive losses and cooldown.
        """
        self.config = config
        self._logger = logger.bind(component="CircuitBreaker")
        self._state: str = CircuitBreakerState.NORMAL
        self._tripped_at: Optional[datetime] = None
        self._cooldown_until: Optional[datetime] = None

    def check(
        self,
        daily_pnl: DailyPnLSnapshot,
        account_balance: float,
        max_daily_loss_pct: float = 5.0,
    ) -> bool:
        """Check if trading should be allowed.

        Evaluates all circuit breaker conditions:
            1. Max daily loss percentage exceeded
            2. Max consecutive losses reached
            3. Cooldown period still active

        If any condition is breached, the breaker trips.

        Args:
            daily_pnl: Current daily PnL snapshot.
            account_balance: Current account balance for % calculations.
            max_daily_loss_pct: Maximum allowed daily loss percentage.

        Returns:
            True if trading is ALLOWED, False if blocked.
        """
        if self._state == CircuitBreakerState.TRIPPED:
            if self._check_cooldown():
                return False
            # Cooldown expired, already transitioned to NORMAL in _check_cooldown

        if self._state == CircuitBreakerState.COOLDOWN:
            if self._check_cooldown():
                return False

        if not self._check_daily_loss(
            daily_pnl, account_balance, max_loss_pct=max_daily_loss_pct,
        ):
            self.trip(
                f"Daily loss exceeded threshold: "
                f"total_pnl={daily_pnl.total_pnl:.4f}, balance={account_balance:.2f}"
            )
            return False

        if not self._check_consecutive_losses(daily_pnl):
            self.trip(
                f"Consecutive losses reached threshold: "
                f"{daily_pnl.consecutive_losses} >= {self.config.max_consecutive_losses}"
            )
            return False

        return True

    def trip(self, reason: str) -> None:
        """Trip the circuit breaker and start cooldown.

        Sets state to TRIPPED, logs the reason, and schedules
        the cooldown period.

        Args:
            reason: Human-readable reason for tripping.
        """
        now = datetime.utcnow()
        self._state = CircuitBreakerState.TRIPPED
        self._tripped_at = now
        self._cooldown_until = now + timedelta(minutes=self.config.cooldown_minutes)
        self._logger.warning(
            "circuit_breaker_tripped",
            reason=reason,
            tripped_at=now.isoformat(),
            cooldown_until=self._cooldown_until.isoformat(),
            cooldown_minutes=self.config.cooldown_minutes,
        )

    def reset(self) -> None:
        """Manually reset the circuit breaker to NORMAL state.

        Can be called by operator or after cooldown expires.
        """
        self._logger.info(
            "circuit_breaker_reset",
            previous_state=self._state,
        )
        self._state = CircuitBreakerState.NORMAL
        self._tripped_at = None
        self._cooldown_until = None

    def _check_cooldown(self) -> bool:
        """Check if cooldown period has expired.

        If cooldown has expired, transition to NORMAL state.

        Returns:
            True if still in cooldown, False if expired.
        """
        if self._cooldown_until is None:
            return False

        now = datetime.utcnow()
        if now < self._cooldown_until:
            return True

        self._logger.info(
            "circuit_breaker_cooldown_expired",
            cooldown_until=self._cooldown_until.isoformat(),
        )
        self._state = CircuitBreakerState.NORMAL
        self._tripped_at = None
        self._cooldown_until = None
        return False

    def _check_daily_loss(
        self,
        daily_pnl: DailyPnLSnapshot,
        account_balance: float,
        max_loss_pct: float,
    ) -> bool:
        """Check if daily loss exceeds threshold.

        Args:
            daily_pnl: Current daily PnL.
            account_balance: Account balance.
            max_loss_pct: Maximum allowed daily loss percentage.

        Returns:
            True if daily loss is within limits.
        """
        if account_balance <= 0:
            return True

        loss_pct = abs(daily_pnl.total_pnl) / account_balance * 100.0
        return loss_pct <= max_loss_pct

    def _check_consecutive_losses(
        self,
        daily_pnl: DailyPnLSnapshot,
    ) -> bool:
        """Check if consecutive losses exceed threshold.

        Args:
            daily_pnl: Current daily PnL snapshot.

        Returns:
            True if consecutive losses are within limits.
        """
        return daily_pnl.consecutive_losses < self.config.max_consecutive_losses

    @property
    def is_tripped(self) -> bool:
        """Check if circuit breaker is currently tripped."""
        return self._state == CircuitBreakerState.TRIPPED

    @property
    def is_in_cooldown(self) -> bool:
        """Check if circuit breaker is in cooldown period."""
        return self._state == CircuitBreakerState.COOLDOWN

    @property
    def is_normal(self) -> bool:
        """Check if circuit breaker is in normal state."""
        return self._state == CircuitBreakerState.NORMAL

    @property
    def status(self) -> dict:
        """Get circuit breaker status for logging/monitoring.

        Returns:
            Dict with state, tripped_at, cooldown_until.
        """
        return {
            "state": self._state,
            "tripped_at": self._tripped_at.isoformat() if self._tripped_at else None,
            "cooldown_until": (
                self._cooldown_until.isoformat() if self._cooldown_until else None
            ),
            "is_tripped": self.is_tripped,
            "is_in_cooldown": self.is_in_cooldown,
            "is_normal": self.is_normal,
        }
