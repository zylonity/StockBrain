"""Live verification for the keyless disclosure feeds.  Opt-in and cheap.

    pytest -m live -s tests/integration/test_disclosure_feeds_live.py

Deselected by default (``addopts`` carries ``-m "not live"``).  One HTTP GET per
feed; the assertion is the *contract* -- at least one release and every release
id non-empty -- which is the early warning for a restyle.  Never a credential.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stockbrain.config import Settings
from stockbrain.ingestion.actusnews import ActusNewsFeed
from stockbrain.ingestion.cnmv import CNMVFeed
from stockbrain.ingestion.disclosure_feeds import DisclosureFeed
from stockbrain.ingestion.eqs import EQSFeed
from stockbrain.ingestion.globenewswire import GlobeNewswireFeed
from stockbrain.ingestion.investegate import InvestegateFeed

pytestmark = pytest.mark.live

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


def _live_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_env": "local", "log_level": "WARNING"}
    base.update(overrides)
    if _ENV_PATH.is_file():
        return Settings(_env_file=str(_ENV_PATH), **base)  # type: ignore[arg-type]
    return Settings(**base)  # type: ignore[arg-type]


async def _assert_one_page(feed: DisclosureFeed) -> None:
    try:
        items = await feed.fetch_page(1)
    finally:
        await feed.aclose()
    print(f"\n{feed.name}: {len(items)} items")
    assert len(items) >= 1
    assert all(item.release_id for item in items)


async def test_investegate_live_contract() -> None:
    await _assert_one_page(InvestegateFeed(_live_settings()))


async def test_eqs_live_contract() -> None:
    await _assert_one_page(EQSFeed(_live_settings()))


async def test_cnmv_inside_information_live_contract() -> None:
    """Inside information may legitimately be empty on a quiet day."""
    feed = CNMVFeed(_live_settings(), kind="ip")
    try:
        items = await feed.fetch_page(1)
    finally:
        await feed.aclose()
    print(f"\n{feed.name}: {len(items)} items")
    assert all(item.release_id for item in items)


async def test_cnmv_other_relevant_live_contract() -> None:
    await _assert_one_page(CNMVFeed(_live_settings(), kind="oir"))


async def test_globenewswire_live_contract() -> None:
    await _assert_one_page(GlobeNewswireFeed(_live_settings(), country="France"))


async def test_actusnews_live_contract() -> None:
    await _assert_one_page(ActusNewsFeed(_live_settings()))
