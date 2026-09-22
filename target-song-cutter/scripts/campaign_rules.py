#!/usr/bin/env python3
"""Shared title-based fallbacks for campaign routing and publishing tags."""

from __future__ import annotations

from collections.abc import Iterable


FUYA_SOUL_TAG = "芙娅之魂"
FUYA_SOUL_TITLE_KEYWORDS = ("玩游戏", "芙娅之魂")


def inferred_campaign_tags(title: object) -> list[str]:
    """Infer durable campaign tags from a reviewed or generated clip title."""
    normalized = str(title or "").strip().casefold()
    if normalized and any(
        keyword.casefold() in normalized for keyword in FUYA_SOUL_TITLE_KEYWORDS
    ):
        return [FUYA_SOUL_TAG]
    return []


def merge_campaign_tags(configured: Iterable[object], title: object) -> list[str]:
    """Keep configured campaign tags and add any title-derived fallback once."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in [*configured, *inferred_campaign_tags(title)]:
        tag = str(raw or "").strip()
        key = tag.casefold()
        if tag and key not in seen:
            result.append(tag)
            seen.add(key)
    return result
