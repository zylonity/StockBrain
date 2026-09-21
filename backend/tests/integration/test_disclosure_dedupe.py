"""A disclosure release re-delivered under another language is a duplicate."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from stockbrain.db.models.sources import Source
from stockbrain.db.session import Database
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.service import IngestionOutcome, IngestionService

pytestmark = pytest.mark.integration


def _release(*, url: str, headline: str, body: str, language: str) -> RawSourceDocument:
    return RawSourceDocument(
        provider=SourceProvider.GLOBENEWSWIRE,
        provider_item_id="3365169",
        url=url,
        source_name="GlobeNewswire",
        source_category=SourceCategory.ISSUER,
        headline=headline,
        published_at=dt.datetime(2026, 9, 21, 7, 55, tzinfo=dt.UTC),
        body=body,
        metadata={"language": language},
    )


async def test_provider_item_id_is_checked_before_url_and_hash(
    clean_tables: Database,
) -> None:
    service = IngestionService(clean_tables)
    first = await service.ingest(
        _release(
            url="https://www.globenewswire.com/news-release/2026/09/21/3365169/0/en/spie.html",
            headline="SPIE announces the launch of a sustainability-linked bond issue",
            body="<p>English body.</p>",
            language="en",
        )
    )
    assert first.outcome is IngestionOutcome.CREATED_EVENT

    # Same release number, different URL, headline and body: only the provider
    # identity layer can catch this, which is exactly what makes it a duplicate.
    second = await service.ingest(
        _release(
            url="https://www.globenewswire.com/news-release/2026/09/21/3365169/0/fr/spie.html",
            headline="SPIE annonce le lancement d'une émission obligataire",
            body="<p>Corps français.</p>",
            language="fr",
        )
    )
    assert second.outcome is IngestionOutcome.DUPLICATE_SOURCE
    assert second.detail == "PROVIDER_ITEM_ID"

    async with clean_tables.session() as session:
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(Source))
        ).scalar_one()
    assert count == 1


def _generic_release(*, provider_item_id: str, url: str) -> RawSourceDocument:
    return RawSourceDocument(
        provider=SourceProvider.INVESTEGATE,
        provider_item_id=provider_item_id,
        url=url,
        source_name="Investegate",
        source_category=SourceCategory.REGULATOR,
        headline="Interim Results",
        published_at=dt.datetime(2026, 9, 21, 8, 0, tzinfo=dt.UTC),
        body=None,
        is_distinct_event=True,
        metadata={"language": "en"},
    )


async def test_distinct_regulatory_events_with_the_same_headline_are_not_collapsed(
    clean_tables: Database,
) -> None:
    """Two RNS releases with a generic headline are two events, not one.

    Without a body, `content_hash` is headline-only, so two different releases
    of "Interim Results" would otherwise collide on Layer 3 before the
    `is_distinct_event` flag is ever consulted.
    """
    service = IngestionService(clean_tables)
    first = await service.ingest(
        _generic_release(
            provider_item_id="rns-100001",
            url="https://www.investegate.co.uk/article.aspx?id=100001",
        )
    )
    assert first.outcome is IngestionOutcome.CREATED_EVENT

    second = await service.ingest(
        _generic_release(
            provider_item_id="rns-100002",
            url="https://www.investegate.co.uk/article.aspx?id=100002",
        )
    )
    assert second.outcome is IngestionOutcome.CREATED_EVENT
    assert second.event_id != first.event_id

    async with clean_tables.session() as session:
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(Source))
        ).scalar_one()
    assert count == 2
