"""The disclosure-feed poll handler.

One job polls one feed.  It walks pages until it reaches an already-known
``release_id`` (so a quiet first page stops the walk), filters boilerplate
before anything is stored, collapses translation variants, and hands the
survivors to the ordinary ingestion path.  A poll never fetches an article
page; bodies are the extractor's job.
"""

from __future__ import annotations

from stockbrain.enums import ProviderStatus, SourceProvider
from stockbrain.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.ingestion.disclosure_feeds import (
    DisclosureFeed,
    FeedItem,
    group_releases,
    is_boilerplate,
    to_document,
)
from stockbrain.ingestion.service import IngestionOutcome
from stockbrain.jobs.registry import HandlerContext
from stockbrain.logging import get_logger
from stockbrain.observability.health import ProviderName

__all__ = ["handle_disclosure_feed_poll"]

log = get_logger(__name__)

_PROVIDER_HEALTH: dict[SourceProvider, ProviderName] = {
    SourceProvider.INVESTEGATE: ProviderName.INVESTEGATE,
    SourceProvider.EQS: ProviderName.EQS,
    SourceProvider.CNMV: ProviderName.CNMV,
    SourceProvider.GLOBENEWSWIRE: ProviderName.GLOBENEWSWIRE,
    SourceProvider.ACTUSNEWS: ProviderName.ACTUSNEWS,
}


async def handle_disclosure_feed_poll(context: HandlerContext) -> None:
    services = context.services
    name = str(context.payload.get("feed") or "")
    feeds = getattr(services, "disclosure_feeds", None) or {}
    feed: DisclosureFeed | None = feeds.get(name)
    if feed is None:
        raise RuntimeError(f"disclosure feed {name!r} is not configured")
    health_name = _PROVIDER_HEALTH[feed.provider]

    collected: list[FeedItem] = []
    filtered = 0
    try:
        for page in range(1, feed.max_pages + 1):
            items = await feed.fetch_page(page)
            if not items:
                if feed.allow_empty:
                    break
                services.health.record(
                    health_name, ProviderStatus.DEGRADED, detail="page shape changed"
                )
                raise ProviderResponseError(f"{name}: page {page} yielded no items")
            known = await services.ingestion.known_release_ids(
                feed.provider, [item.release_id for item in items]
            )
            fresh = [item for item in items if item.release_id not in known]
            for item in fresh:
                if is_boilerplate(item):
                    filtered += 1
                    continue
                collected.append(item)
            if len(fresh) < len(items):
                break
    except ProviderAuthError as exc:
        services.health.record(
            health_name, ProviderStatus.DOWN, detail=f"rejected the request: {exc}"[:300]
        )
        raise
    except ProviderRateLimited as exc:
        services.health.record(
            health_name, ProviderStatus.DOWN, detail=f"rate limited: {exc}"[:300]
        )
        raise
    except ProviderUnavailable as exc:
        services.health.record(health_name, ProviderStatus.DOWN, detail=str(exc)[:300])
        raise
    except ProviderResponseError as exc:
        services.health.record(health_name, ProviderStatus.DEGRADED, detail=str(exc)[:300])
        raise

    grouped = group_releases(collected, feed.native_language)
    documents = []
    malformed = 0
    for item in grouped:
        try:
            documents.append(to_document(item))
        except ValueError as exc:
            malformed += 1
            log.warning(
                "disclosure_feed_item_malformed",
                feed=name,
                release_id=item.release_id,
                error=str(exc),
            )

    results = await services.ingestion.ingest_many(documents)
    created = sum(1 for result in results if result.outcome is IngestionOutcome.CREATED_EVENT)
    services.health.record(
        health_name,
        ProviderStatus.HEALTHY,
        detail=f"{created} created, {filtered} filtered, {malformed} malformed",
        metrics={"created": created, "filtered": filtered, "malformed": malformed},
    )
    log.info(
        "disclosure_feed_poll_complete",
        feed=name,
        new=len(collected),
        created=created,
        filtered=filtered,
        malformed=malformed,
    )
