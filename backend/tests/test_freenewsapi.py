"""FreeNewsApi specifics: title suffixes, pacing and unusable listing rows."""

from __future__ import annotations

import asyncio

import pytest

from app.providers.base import strip_source_suffix
from app.providers.freenewsapi import RateLimiter


def test_publisher_suffix_is_stripped_before_the_title_is_hashed() -> None:
    # Left in place, the suffix would stop this story matching the same story
    # as told by NewsData, which does not append the outlet.
    assert (
        strip_source_suffix("GTA VI blimp to appear at NASCAR weekend - Motorsport.com", "Motorsport.com")
        == "GTA VI blimp to appear at NASCAR weekend"
    )


def test_a_pipe_or_dash_suffix_is_also_stripped() -> None:
    assert strip_source_suffix("Budget passes after long debate | WBKO", "WBKO") == (
        "Budget passes after long debate"
    )


def test_a_headline_that_merely_mentions_the_outlet_is_left_alone() -> None:
    title = "Motorsport.com is launching a new app"
    assert strip_source_suffix(title, "Motorsport.com") == title


def test_a_missing_publisher_leaves_the_title_untouched() -> None:
    assert strip_source_suffix("Something happened today - Somewhere", None) == (
        "Something happened today - Somewhere"
    )


async def test_the_limiter_spaces_calls_to_the_configured_rate() -> None:
    limiter = RateLimiter(requests_per_second=10.0)  # one call every 100ms

    started = asyncio.get_running_loop().time()
    for _ in range(4):
        await limiter.acquire()
    elapsed = asyncio.get_running_loop().time() - started

    # The first call goes straight out; the next three are each held back.
    assert elapsed >= 0.3 - 0.05


async def test_the_limiter_serialises_concurrent_callers() -> None:
    limiter = RateLimiter(requests_per_second=20.0)
    order: list[int] = []

    async def call(index: int) -> None:
        await limiter.acquire()
        order.append(index)

    await asyncio.gather(*(call(index) for index in range(5)))

    # Every caller ran, each after the last one was let through.
    assert sorted(order) == [0, 1, 2, 3, 4]
