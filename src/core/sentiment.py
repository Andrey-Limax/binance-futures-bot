"""Sentiment Analyzer: Fear & Greed Index integration.

Fetches the Crypto Fear & Greed Index from Alternative.me to
block new entries during extreme market conditions.

Cache: 1 hour TTL to avoid rate limiting.
Fallback: If API fails, return neutral (score=50) and log warning.
The bot MUST NOT crash due to sentiment API failures.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import structlog

from src.models import SentimentConfig, SentimentResult

logger = structlog.get_logger(__name__)

# Alternative.me Fear & Greed Index API (free, no key required)
FEAR_GREED_API_URL = "https://api.alternative.me/fng/"
FALLBACK_SCORE = 50
FALLBACK_LABEL = "neutral"


class SentimentAnalyzer:
    """Async Fear & Greed Index analyzer with caching and fallback.

    Responsibilities:
        - Fetch Fear & Greed Index from Alternative.me API
        - Cache results for 1 hour (configurable TTL)
        - Provide fallback neutral sentiment if API is unreachable
        - Determine if new entries should be blocked (extreme fear/greed)

    Attributes:
        config: Sentiment configuration (thresholds, TTL).
        _cache: Last successful SentimentResult.
        _cache_expiry: When the cache expires.
    """

    def __init__(self, config: SentimentConfig) -> None:
        """Initialize sentiment analyzer.

        Args:
            config: Sentiment thresholds and cache TTL.
        """
        self.config = config
        self._logger = logger.bind(component="SentimentAnalyzer")
        self._cache: Optional[SentimentResult] = None
        self._cache_expiry: Optional[datetime] = None

    async def get_sentiment(self) -> SentimentResult:
        """Get current sentiment, using cache if still valid.

        If cache is fresh (within TTL), return cached result.
        Otherwise, fetch from API. On failure, return fallback neutral.

        Returns:
            SentimentResult with score, label, and blocking flags.
        """
        if self.is_cache_valid():
            self._logger.debug("Returning cached sentiment", score=self._cache.score)
            return self._cache

        self._logger.info("Cache miss or expired, fetching fresh sentiment")
        result = await self.fetch_fear_greed_index()
        self._cache = result
        self._cache_expiry = datetime.utcnow() + timedelta(
            seconds=self.config.cache_ttl_seconds
        )
        return result

    async def fetch_fear_greed_index(self) -> SentimentResult:
        """Fetch Fear & Greed Index from Alternative.me API.

        Makes an async HTTP GET request to the Alternative.me endpoint.
        Parses the JSON response and maps score to labels:
            - 0-24: Extreme Fear
            - 25-49: Fear
            - 50-74: Greed
            - 75-100: Extreme Greed

        On ANY exception (network error, timeout, parse error):
            - Log warning with error details
            - Return fallback neutral SentimentResult (score=50, is_fallback=True)

        Returns:
            SentimentResult with score, label, and extreme flags.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(FEAR_GREED_API_URL) as response:
                    response.raise_for_status()
                    data = await response.json()
                    return self._parse_response(data)
        except Exception as exc:
            self._logger.warning(
                "Failed to fetch Fear & Greed Index, using fallback",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return self._build_fallback(error=exc)

    def _parse_response(self, data: dict) -> SentimentResult:
        """Parse Alternative.me API response into SentimentResult.

        Expected response format:
            {
                "data": [{
                    "value": "45",
                    "value_classification": "Fear",
                    ...
                }]
            }

        Args:
            data: Raw JSON response dict.

        Returns:
            Parsed SentimentResult.
        """
        entry = data["data"][0]
        score = int(entry["value"])
        label = self._classify_score(score)

        return SentimentResult(
            score=score,
            label=label,
            is_extreme_fear=score <= self.config.extreme_fear_threshold,
            is_extreme_greed=score >= self.config.extreme_greed_threshold,
            fetched_at=datetime.utcnow(),
            source="alternative.me",
            is_fallback=False,
        )

    def _build_fallback(self, error: Optional[Exception] = None) -> SentimentResult:
        """Build a fallback neutral SentimentResult.

        Used when the API is unreachable or returns invalid data.

        Args:
            error: Optional exception that triggered the fallback.

        Returns:
            Neutral SentimentResult (score=50, is_fallback=True).
        """
        return SentimentResult(
            score=FALLBACK_SCORE,
            label=FALLBACK_LABEL,
            is_extreme_fear=False,
            is_extreme_greed=False,
            fetched_at=datetime.utcnow(),
            source="fallback",
            is_fallback=True,
        )

    def _classify_score(self, score: int) -> str:
        """Map numeric score to human-readable label.

        Args:
            score: Fear & Greed score (0-100).

        Returns:
            Label string: 'extreme_fear', 'fear', 'neutral', 'greed', or 'extreme_greed'.
        """
        if score <= self.config.extreme_fear_threshold:
            return "extreme_fear"
        if score < 45:
            return "fear"
        if score <= 55:
            return "neutral"
        if score < self.config.extreme_greed_threshold:
            return "greed"
        return "extreme_greed"

    def is_cache_valid(self) -> bool:
        """Check if the cached sentiment is still within TTL.

        Returns:
            True if cache exists and hasn't expired.
        """
        if self._cache is None or self._cache_expiry is None:
            return False
        return datetime.utcnow() < self._cache_expiry

    def invalidate_cache(self) -> None:
        """Force cache invalidation on next fetch."""
        self._cache = None
        self._cache_expiry = None
