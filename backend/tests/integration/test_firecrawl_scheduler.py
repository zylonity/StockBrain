"""Firecrawl scheduling correctness: cadence, restart, dedupe, no retry storms.

Reconstructed directly from ``firecrawl_activity_logs.csv``.  What the log shows
is four independent per-topic timers running at 20 and 30 minutes, nine enabled
queries between them, and one full sweep the instant the process started:

    04:59:16-39  nine searches  (every enabled query, on start-up)
    05:19:54     three          (ai_infrastructure, 20 min later)
    05:30:04-11  six            (the three 30-minute topics)
    05:40:27     three          (ai_infrastructure again)
    06:00:33-38  six            (the 30-minute topics again)
    06:01:31-32  two            (ai_infrastructure, third cycle)

Twenty-nine searches in 62 minutes.  In steady state that is 21 an hour: 3
queries every 20 minutes plus 6 every 30, which is 504 a day.

Each of these tests names the specific property of that behaviour it prevents
from coming back.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.system import DiscoveryQuery, DiscoveryTopic, Job
from stockbrain.db.session import Database
from stockbrain.enums import JobStatus, JobType
from stockbrain.ingestion.topics import DEFAULT_TOPICS, seed_default_topics
from stockbrain.jobs.handlers import effective_topic_interval_minutes
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.services import ServiceContainer

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "firecrawl_api_key": "fc-test",
        "firecrawl_enabled": True,
        "discovery_enabled": True,
        # Nothing else should start; these tests are about the sweep.
        "alpaca_news_enabled": False,
        "sec_enabled": False,
        "t212_metadata_enabled": False,
        "research_enabled": False,
        "classifier_enabled": False,
        "proposals_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _container(database: Database, **overrides: object) -> ServiceContainer:
    return ServiceContainer(
        settings=_settings(database, **overrides),
        database=database,
        health=ProviderHealthRegistry(),
    )


async def _seed_two_queries(database: Database, *, interval_minutes: int = 20) -> None:
    """One topic, two queries, on the cadence the incident actually ran at."""
    async with database.transaction() as session:
        topic = DiscoveryTopic(
            slug="ai_infrastructure",
            name="AI infrastructure",
            enabled=True,
            interval_minutes=interval_minutes,
            result_limit=10,
            freshness="qdr:h",
            include_domains=[],
            exclude_domains=[],
        )
        session.add(topic)
        await session.flush()
        session.add(DiscoveryQuery(topic_id=topic.id, query='"AI data center"', enabled=True))
        session.add(DiscoveryQuery(topic_id=topic.id, query='"data centre" power', enabled=True))


async def _jobs(database: Database) -> list[Job]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(Job)
                    .where(Job.job_type == JobType.FIRECRAWL_TOPIC_SEARCH.value)
                    .order_by(Job.created_at)
                )
            ).scalars()
        )


async def _queries(database: Database) -> list[DiscoveryQuery]:
    async with database.session() as session:
        return list(
            (
                await session.execute(sa.select(DiscoveryQuery).order_by(DiscoveryQuery.query))
            ).scalars()
        )


# ---------------------------------------------------------------------------
# The scheduler only enqueues
# ---------------------------------------------------------------------------
async def test_the_sweep_enqueues_and_makes_no_paid_call(clean_tables: Database) -> None:
    """The separation Phase 2 got right and must keep.

    The sweep decides *which* queries are due; the handler is the only thing
    that spends. A container built with no HTTP client at all still sweeps
    cleanly, which is the structural version of "the scheduler cannot spend".
    """
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_topic_searches()

    jobs = await _jobs(clean_tables)
    assert len(jobs) == 2
    assert all(job.status is JobStatus.PENDING for job in jobs)


async def test_a_paid_search_job_gets_exactly_one_attempt(clean_tables: Database) -> None:
    """The queue's retry is a second *paid* call, so it is switched off.

    Phase 2 left ``max_attempts`` at its default of 3, and the client itself
    retried up to 3 times inside each attempt -- up to nine billable requests
    per scheduled search. The durable cooldown is the retry now.
    """
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_topic_searches()
    assert all(job.max_attempts == 1 for job in await _jobs(clean_tables))


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
async def test_the_interval_floor_overrides_a_twenty_minute_topic_row(
    clean_tables: Database,
) -> None:
    """The exact cadence from the CSV, refused.

    ``ai_infrastructure`` asked for 20 minutes and its three queries account
    for 9 of the 21 searches an hour. A topic row asking for it now gets the
    configured floor, because the floor is applied where the interval is *used*.
    """
    await _seed_two_queries(clean_tables, interval_minutes=20)
    services = _container(clean_tables)
    await services._enqueue_due_topic_searches()

    for query in await _queries(clean_tables):
        assert query.next_eligible_at is not None
        # Claimed at enqueue time, one full floor interval out.
        gap = query.next_eligible_at - dt.datetime.now(dt.UTC)
        assert gap > dt.timedelta(minutes=700), gap


async def test_a_query_inside_its_cooldown_is_not_enqueued_again(
    clean_tables: Database,
) -> None:
    """The second sweep tick must not re-enqueue what the first just claimed."""
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_topic_searches()
    first = len(await _jobs(clean_tables))

    # Clear the queue so dedupe is not what stops the second sweep -- the
    # cooldown has to be sufficient on its own.
    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Job))
    await services._enqueue_due_topic_searches()

    assert first == 2
    assert await _jobs(clean_tables) == []


async def test_a_process_restart_does_not_reset_the_cooldown(
    clean_tables: Database,
) -> None:
    """The single behaviour that produced nine searches at 04:59:16.

    ``Scheduler._loop`` runs each task once immediately on start, so a restart
    used to re-run every query. Eligibility is now a committed column, and a
    brand-new container reads the same one.
    """
    await _seed_two_queries(clean_tables)
    await _container(clean_tables)._enqueue_due_topic_searches()
    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Job))

    # A different ServiceContainer with different objects: a restart.
    restarted = _container(clean_tables)
    await restarted._enqueue_due_topic_searches()
    assert await _jobs(clean_tables) == []


async def test_ten_restarts_in_a_row_still_spend_nothing(
    clean_tables: Database,
) -> None:
    """A crash-looping container is the worst case for a per-start sweep.

    Ten restarts used to be ten full sweeps -- ninety searches on the real
    topic set.
    """
    await _seed_two_queries(clean_tables)
    await _container(clean_tables)._enqueue_due_topic_searches()
    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Job))

    for _ in range(10):
        await _container(clean_tables)._enqueue_due_topic_searches()
    assert await _jobs(clean_tables) == []


async def test_a_query_becomes_eligible_again_once_the_cooldown_elapses(
    clean_tables: Database,
) -> None:
    """The cooldown is a delay, not a permanent stop."""
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_topic_searches()
    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Job))
        await session.execute(
            sa.update(DiscoveryQuery).values(
                next_eligible_at=sa.func.now() - dt.timedelta(minutes=1)
            )
        )
    await services._enqueue_due_topic_searches()
    assert len(await _jobs(clean_tables)) == 2


async def test_a_row_written_before_the_column_existed_is_not_treated_as_due(
    clean_tables: Database,
) -> None:
    """A ``NULL`` next-eligible on a query that *has* run is derived, not ignored.

    Reading the ``NULL`` as "run immediately" would re-run every existing query
    the moment this shipped -- a smaller copy of the incident. The migration
    backfills it; this covers a row that slipped through anyway.
    """
    await _seed_two_queries(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(
            sa.update(DiscoveryQuery).values(
                next_eligible_at=None, last_run_at=sa.func.now() - dt.timedelta(minutes=30)
            )
        )
    await _container(clean_tables)._enqueue_due_topic_searches()
    assert await _jobs(clean_tables) == []

    queries = await _queries(clean_tables)
    # And it is repaired in place, so the derivation happens once.
    assert all(query.next_eligible_at is not None for query in queries)


async def test_a_query_that_has_never_run_is_due_immediately(
    clean_tables: Database,
) -> None:
    """A genuinely new query should not wait twelve hours for its first search."""
    await _seed_two_queries(clean_tables)
    await _container(clean_tables)._enqueue_due_topic_searches()
    assert len(await _jobs(clean_tables)) == 2


# ---------------------------------------------------------------------------
# Dedupe and concurrency
# ---------------------------------------------------------------------------
async def test_two_sweeps_running_at_once_cannot_queue_the_same_query_twice(
    clean_tables: Database,
) -> None:
    """Two scheduler loops -- two containers, or one restarted before the old
    one exited -- must not produce two paid searches for one query.

    Guarded twice: ``uq_jobs_dedupe_key_active`` on the job row, and the
    ``FOR UPDATE ... SKIP LOCKED`` claim on the query row.
    """
    await _seed_two_queries(clean_tables)
    first = _container(clean_tables)
    second = _container(clean_tables)
    # Sequential rather than gathered: the two sweeps hold row locks on the same
    # table and gathering them on one asyncio loop with one pool can deadlock in
    # the test, which would prove nothing about production.
    await first._enqueue_due_topic_searches()
    await second._enqueue_due_topic_searches()

    jobs = await _jobs(clean_tables)
    assert len(jobs) == 2
    assert len({job.dedupe_key for job in jobs}) == 2


async def test_an_outstanding_job_suppresses_a_second_enqueue(
    clean_tables: Database,
) -> None:
    """A slow provider must not accumulate a backlog of identical searches.

    The cooldown alone would allow it once the interval passed; the dedupe key
    is what stops a second job while the first is still queued or running.
    """
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_topic_searches()
    async with clean_tables.transaction() as session:
        # Make them eligible again but leave the jobs in place.
        await session.execute(
            sa.update(DiscoveryQuery).values(next_eligible_at=sa.func.now() - dt.timedelta(hours=1))
        )
    await services._enqueue_due_topic_searches()
    assert len(await _jobs(clean_tables)) == 2


# ---------------------------------------------------------------------------
# Budget interaction
# ---------------------------------------------------------------------------
async def test_the_sweep_stops_when_the_daily_search_cap_is_reached(
    clean_tables: Database,
) -> None:
    """Queueing jobs that will refuse themselves is churn, not safety.

    The handler would refuse anyway -- the budget is the real limit -- but a
    queue full of doomed jobs hides the ones that matter and cycles the dedupe
    keys for nothing.
    """
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables, firecrawl_max_searches_per_day=0)
    await services._enqueue_due_topic_searches()
    assert await _jobs(clean_tables) == []


async def test_the_sweep_enqueues_only_as_many_as_the_budget_allows(
    clean_tables: Database,
) -> None:
    """One remaining search, two due queries: one job.

    Oldest-first, so a partial allowance is spent on what has waited longest
    rather than on whichever row PostgreSQL happened to return.
    """
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables, firecrawl_max_searches_per_day=1)
    await services._enqueue_due_topic_searches()
    assert len(await _jobs(clean_tables)) == 1


async def test_discovery_paused_stops_the_sweep(clean_tables: Database) -> None:
    """The durable pause covers paid discovery too."""
    from stockbrain.db.models.system import AppSetting
    from stockbrain.services import DISCOVERY_PAUSED_KEY

    await _seed_two_queries(clean_tables)
    async with clean_tables.transaction() as session:
        session.add(AppSetting(key=DISCOVERY_PAUSED_KEY, value={"paused": True}))
    await _container(clean_tables)._enqueue_due_topic_searches()
    assert await _jobs(clean_tables) == []


async def test_a_disabled_topic_is_never_swept(clean_tables: Database) -> None:
    await _seed_two_queries(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(DiscoveryTopic).values(enabled=False))
    await _container(clean_tables)._enqueue_due_topic_searches()
    assert await _jobs(clean_tables) == []


# ---------------------------------------------------------------------------
# The seeded defaults
# ---------------------------------------------------------------------------
async def test_the_seeded_default_topics_cost_less_than_the_daily_cap(
    clean_tables: Database,
) -> None:
    """The projection, computed from the rows a fresh install actually gets.

    The old seed enabled four topics and nine queries at 20-30 minutes: 504
    searches a day. This asserts the new seed's own arithmetic rather than a
    remembered number, so changing a seed interval fails here rather than in
    the billing.
    """
    async with clean_tables.transaction() as session:
        await seed_default_topics(session)

    settings = _settings(clean_tables)
    async with clean_tables.session() as session:
        rows = (
            await session.execute(
                sa.select(DiscoveryQuery, DiscoveryTopic)
                .join(DiscoveryTopic, DiscoveryTopic.id == DiscoveryQuery.topic_id)
                .where(DiscoveryQuery.enabled.is_(True), DiscoveryTopic.enabled.is_(True))
            )
        ).all()

    searches_per_day = sum(
        1440 / effective_topic_interval_minutes(topic.interval_minutes, settings)
        for _, topic in rows
    )
    # Five enabled queries at twelve hours apiece: ten searches a day.
    assert len(rows) == 5
    assert searches_per_day == 10
    assert searches_per_day <= settings.firecrawl_max_searches_per_day

    # Two credits each with the default 5-per-source limit and two sources.
    projected_credits = int(searches_per_day) * 2
    projected_credits += settings.firecrawl_max_scrapes_per_day
    assert projected_credits <= settings.firecrawl_daily_credit_cap
    assert projected_credits * 31 <= settings.firecrawl_monthly_credit_cap


async def test_the_seeded_result_limit_is_one_billing_block(
    clean_tables: Database,
) -> None:
    """Six results per source would be twelve billed results and 4 credits."""
    async with clean_tables.transaction() as session:
        await seed_default_topics(session)
    async with clean_tables.session() as session:
        limits = set((await session.execute(sa.select(DiscoveryTopic.result_limit))).scalars())
    assert limits == {5}


def test_no_seeded_topic_asks_for_a_sub_hour_cadence() -> None:
    """A defence against the seed drifting back.

    The floor would clamp it anyway; a seed that *asked* for 20 minutes would
    still be a seed that told the next reader 20 minutes was reasonable.
    """
    assert all(seed.interval_minutes >= 720 for seed in DEFAULT_TOPICS)
    assert sum(1 for seed in DEFAULT_TOPICS if seed.enabled) == 2
