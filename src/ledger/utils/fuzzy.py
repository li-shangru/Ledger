"""
Fuzzy matching utilities for merchant normalization.

Uses rapidfuzz for efficient fuzzy string matching.
"""

import logging
import re

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


def _normalize_for_match(value: str) -> str:
    """Normalize noisy merchant text before fuzzy matching.

    Significant digits are KEPT (``407 ETR``, ``M2 CAKE``, ``CAMP 31 BBQ`` are
    distinct merchants), but store-number suffixes like ``#1234`` are noise and
    are stripped first so a location number never blocks a match.
    """
    value = re.sub(r"(?i)'s\b", "s", value)
    value = re.sub(r"#\s*\d+", " ", value)
    value = re.sub(r"[^A-Za-z0-9& ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip().upper()


def _digit_tokens(value: str) -> set[str]:
    """Digit runs in normalized text, used to keep digit-distinct merchants apart."""
    return set(re.findall(r"\d+", value))


def find_best_match(
    query: str,
    candidates: list[str],
    threshold: int = 85,
    min_length: int = 3,
) -> tuple[str | None, int]:
    """
    Find best fuzzy match from candidates.

    Args:
        query: Text to match
        candidates: List of possible matches
        threshold: Minimum similarity score (0-100)
        min_length: Minimum query length to attempt matching

    Returns:
        (best_match, score) or (None, 0) if no match above threshold
    """
    if not query or len(query) < min_length:
        return None, 0

    if not candidates:
        return None, 0

    normalized_query = _normalize_for_match(query)
    normalized_candidates = [_normalize_for_match(candidate) for candidate in candidates]

    # Use WRatio (weighted ratio) which handles:
    # - Case insensitivity
    # - Partial matches
    # - Token sorting
    # - Length differences
    result = process.extractOne(
        normalized_query,
        normalized_candidates,
        scorer=fuzz.WRatio,
        score_cutoff=threshold,
    )

    if result:
        # extractOne returns (choice, score, index).  Map back to the EXACT
        # original via the index rather than a normalized-text dict lookup:
        # two distinct originals can normalize to the same key, and a dict
        # would collapse them — returning the wrong original.
        _normalized_match, score, idx = result
        # A digit that survives normalization is significant: refuse a match whose
        # digit tokens differ from the query's, so "408 ETR" never maps to "407 ETR"
        # (and inherits its category at high confidence). Store numbers were already
        # stripped by _normalize_for_match, so this only guards meaningful digits.
        if _digit_tokens(normalized_query) != _digit_tokens(normalized_candidates[idx]):
            return None, 0
        best_match = candidates[idx]
        # Log only non-identifying metadata — raw merchant text is financial PII
        # and must not appear in any log level (mirrors find_merchant_match behaviour).
        logger.debug(
            "fuzzy best_match: score=%s query_len=%s match_len=%s",
            int(score),
            len(query),
            len(best_match),
        )
        return best_match, int(score)

    return None, 0


def find_merchant_match(
    query: str,
    merchant_names: list[str],
    threshold: int = 85,
    min_length: int = 3,
) -> str | None:
    """
    Find best matching merchant from list.

    Convenience wrapper around find_best_match.

    Args:
        query: Raw merchant text
        merchant_names: List of normalized merchant names
        threshold: Minimum similarity score
        min_length: Minimum query length

    Returns:
        Best matching merchant name, or None if no match
    """
    match, score = find_best_match(query, merchant_names, threshold, min_length)
    if match:
        # Log only non-identifying metadata at DEBUG — raw merchant text is financial PII
        # and must not appear in INFO-level logs.
        logger.debug("fuzzy merchant match: score=%s query_len=%s", score, len(query))
    return match
