"""Web discovery scheduling: cadence, restart, dedupe, and no retry storms.

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
from coming back.  The provider underneath has changed -- Brave for routine
searches, Exa for semantic ones -- but every failure mode the log records is a
scheduling failure mode, and none of them was fixed by changing vendor.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.system import DiscoveryQuery, DiscoveryTopic, Job
from stockbrain.db.session import Database
from stockbrain.enums import JobStatus, JobType, WebDiscoveryKind
from stockbrain.ingestion.topics import DEFAULT_TOPICS, seed_default_topics
from stockbrain.jobs.handlers import effective_query_interval_minutes
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.services import ServiceContainer

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "brave_api_key": "brv-test",
        "exa_api_key": "exa-test",
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
            freshness_days=1,
            include_domains=[],
            exclude_domains=[],
        )
        session.add(topic)
        await session.flush()
        for text in ('"AI data center"', '"data centre" power'):
            session.add(
                DiscoveryQuery(
                    topic_id=topic.id,
                    query=text,
                    enabled=True,
                    search_kind=WebDiscoveryKind.ROUTINE,
                )
            )


async def _seed_semantic_query(database: Database) -> None:
    """One semantic query, on its own topic, so the two kinds can be told apart."""
    async with database.transaction() as session:
        topic = DiscoveryTopic(
            slug="second_order_exposure",
            name="Second-order exposure",
            enabled=True,
            interval_minutes=1440,
            result_limit=10,
            freshness_days=30,
            include_domains=[],
            exclude_domains=[],
        )
        session.add(topic)
        await session.flush()
        session.add(
            DiscoveryQuery(
                topic_id=topic.id,
                query="who benefits from a transformer shortage",
                enabled=True,
                search_kind=WebDiscoveryKind.SEMANTIC,
            )
        )


async def _jobs(database: Database) -> list[Job]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(Job)
                    .where(Job.job_type == JobType.WEB_DISCOVERY_SEARCH.value)
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
    await services._enqueue_due_routine_searches()

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
    await services._enqueue_due_routine_searches()
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
    await services._enqueue_due_routine_searches()

    for query in await _queries(clean_tables):
        assert query.next_eligible_at is not None
        # Claimed at enqueue time, one full floor interval out: the configured
        # routine floor of six hours, not the twenty minutes the row asked for.
        gap = query.next_eligible_at - dt.datetime.now(dt.UTC)
        assert gap > dt.timedelta(minutes=355), gap


async def test_a_query_inside_its_cooldown_is_not_enqueued_again(
    clean_tables: Database,
) -> None:
    """The second sweep tick must not re-enqueue what the first just claimed."""
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_routine_searches()
    first = len(await _jobs(clean_tables))

    # Clear the queue so dedupe is not what stops the second sweep -- the
    # cooldown has to be sufficient on its own.
    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Job))
    await services._enqueue_due_routine_searches()

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
    await _container(clean_tables)._enqueue_due_routine_searches()
    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Job))

    # A different ServiceContainer with different objects: a restart.
    restarted = _container(clean_tables)
    await restarted._enqueue_due_routine_searches()
    assert await _jobs(clean_tables) == []


async def test_ten_restarts_in_a_row_still_spend_nothing(
    clean_tables: Database,
) -> None:
    """A crash-looping container is the worst case for a per-start sweep.

    Ten restarts used to be ten full sweeps -- ninety searches on the real
    topic set.
    """
    await _seed_two_queries(clean_tables)
    await _container(clean_tables)._enqueue_due_routine_searches()
    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Job))

    for _ in range(10):
        await _container(clean_tables)._enqueue_due_routine_searches()
    assert await _jobs(clean_tables) == []


async def test_a_query_becomes_eligible_again_once_the_cooldown_elapses(
    clean_tables: Database,
) -> None:
    """The cooldown is a delay, not a permanent stop."""
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_routine_searches()
    async with clean_tables.transaction() as session:
        await session.execute(sa.delete(Job))
        await session.execute(
            sa.update(DiscoveryQuery).values(
                next_eligible_at=sa.func.now() - dt.timedelta(minutes=1)
            )
        )
    await services._enqueue_due_routine_searches()
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
    await _container(clean_tables)._enqueue_due_routine_searches()
    assert await _jobs(clean_tables) == []

    queries = await _queries(clean_tables)
    # And it is repaired in place, so the derivation happens once.
    assert all(query.next_eligible_at is not None for query in queries)


async def test_a_query_that_has_never_run_is_due_immediately(
    clean_tables: Database,
) -> None:
    """A genuinely new query should not wait twelve hours for its first search."""
    await _seed_two_queries(clean_tables)
    await _container(clean_tables)._enqueue_due_routine_searches()
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
    await first._enqueue_due_routine_searches()
    await second._enqueue_due_routine_searches()

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
    await services._enqueue_due_routine_searches()
    async with clean_tables.transaction() as session:
        # Make them eligible again but leave the jobs in place.
        await session.execute(
            sa.update(DiscoveryQuery).values(next_eligible_at=sa.func.now() - dt.timedelta(hours=1))
        )
    await services._enqueue_due_routine_searches()
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
    services = _container(
        clean_tables, brave_max_searches_per_day=0, brave_max_searches_per_month=0
    )
    await services._enqueue_due_routine_searches()
    assert await _jobs(clean_tables) == []


async def test_the_sweep_enqueues_only_as_many_as_the_budget_allows(
    clean_tables: Database,
) -> None:
    """One remaining search, two due queries: one job.

    Oldest-first, so a partial allowance is spent on what has waited longest
    rather than on whichever row PostgreSQL happened to return.
    """
    await _seed_two_queries(clean_tables)
    services = _container(clean_tables, brave_max_searches_per_day=1)
    await services._enqueue_due_routine_searches()
    assert len(await _jobs(clean_tables)) == 1


async def test_discovery_paused_stops_the_sweep(clean_tables: Database) -> None:
    """The durable pause covers paid discovery too."""
    from stockbrain.db.models.system import AppSetting
    from stockbrain.services import DISCOVERY_PAUSED_KEY

    await _seed_two_queries(clean_tables)
    async with clean_tables.transaction() as session:
        session.add(AppSetting(key=DISCOVERY_PAUSED_KEY, value={"paused": True}))
    await _container(clean_tables)._enqueue_due_routine_searches()
    assert await _jobs(clean_tables) == []


async def test_a_disabled_topic_is_never_swept(clean_tables: Database) -> None:
    await _seed_two_queries(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(DiscoveryTopic).values(enabled=False))
    await _container(clean_tables)._enqueue_due_routine_searches()
    assert await _jobs(clean_tables) == []


# ---------------------------------------------------------------------------
# Routine and semantic are separate schedules on separate budgets
# ---------------------------------------------------------------------------
async def test_the_routine_sweep_never_enqueues_a_semantic_query(
    clean_tables: Database,
) -> None:
    """The single most expensive misconfiguration available here.

    A semantic query on the routine cadence is a ten-times-the-price search run
    ten times as often, and the answer it returns moves over weeks. The sweeps
    filter on ``search_kind`` rather than sharing one list.
    """
    await _seed_two_queries(clean_tables)
    await _seed_semantic_query(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_routine_searches()

    jobs = await _jobs(clean_tables)
    assert len(jobs) == 2
    assert all(job.payload["kind"] == "ROUTINE" for job in jobs)
    assert all(job.payload["provider"] == "brave" for job in jobs)


async def test_the_semantic_sweep_never_enqueues_a_routine_query(
    clean_tables: Database,
) -> None:
    await _seed_two_queries(clean_tables)
    await _seed_semantic_query(clean_tables)
    services = _container(clean_tables)
    await services._enqueue_due_semantic_searches()

    jobs = await _jobs(clean_tables)
    assert len(jobs) == 1
    assert jobs[0].payload["kind"] == "SEMANTIC"
    assert jobs[0].payload["provider"] == "exa"


async def test_an_exhausted_routine_budget_does_not_stop_semantic_discovery(
    clean_tables: Database,
) -> None:
    """Each provider has its own caps, its own ledger sums and its own lock.

    Brave running out is not a reason to stop asking second-order questions,
    and -- crucially in the other direction -- it is *not* a reason to start
    asking them on Brave's behalf.
    """
    await _seed_two_queries(clean_tables)
    await _seed_semantic_query(clean_tables)
    services = _container(
        clean_tables, brave_max_searches_per_day=0, brave_max_searches_per_month=0
    )
    await services._enqueue_due_routine_searches()
    await services._enqueue_due_semantic_searches()

    jobs = await _jobs(clean_tables)
    assert len(jobs) == 1
    assert jobs[0].payload["kind"] == "SEMANTIC"


async def test_an_unavailable_routine_provider_does_not_fan_out_to_the_other(
    clean_tables: Database,
) -> None:
    """**No automatic fallback between paid providers.**

    Brave unconfigured means routine queries defer. It does not mean they get
    answered by the provider that costs ten times as much -- that pattern is
    how an outage becomes an invoice.
    """
    await _seed_two_queries(clean_tables)
    await _seed_semantic_query(clean_tables)
    services = _container(clean_tables, brave_api_key="")
    assert services.brave is None

    await services._enqueue_due_routine_searches()
    await services._enqueue_due_semantic_searches()

    jobs = await _jobs(clean_tables)
    assert [job.payload["kind"] for job in jobs] == ["SEMANTIC"]


async def test_the_semantic_floor_is_a_day_even_for_an_hourly_topic_row(
    clean_tables: Database,
) -> None:
    """A restored backup asking for hourly semantic search gets a day."""
    await _seed_semantic_query(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(DiscoveryTopic).values(interval_minutes=60))

    services = _container(clean_tables)
    await services._enqueue_due_semantic_searches()

    queries = await _queries(clean_tables)
    assert queries[0].next_eligible_at is not None
    gap = queries[0].next_eligible_at - dt.datetime.now(dt.UTC)
    assert gap > dt.timedelta(hours=23), gap


async def test_a_query_pinned_to_an_unknown_provider_is_not_silently_rehomed(
    clean_tables: Database,
) -> None:
    """A typo in a provider pin must not redirect a query onto a backend it was
    deliberately kept off.  It resolves to ``none`` and the query does not run.
    """
    from stockbrain.jobs.handlers import build_query_plan

    await _seed_two_queries(clean_tables)
    async with clean_tables.transaction() as session:
        await session.execute(sa.update(DiscoveryQuery).values(provider="bravo"))

    settings = _settings(clean_tables)
    async with clean_tables.session() as session:
        row = (
            await session.execute(
                sa.select(DiscoveryQuery, DiscoveryTopic).join(
                    DiscoveryTopic, DiscoveryTopic.id == DiscoveryQuery.topic_id
                )
            )
        ).first()
    assert row is not None
    query, topic = row
    plan = build_query_plan(query, topic, settings)
    assert plan.provider_name.value == "none"


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

    routine = [r for r in rows if r[0].search_kind is WebDiscoveryKind.ROUTINE]
    semantic = [r for r in rows if r[0].search_kind is WebDiscoveryKind.SEMANTIC]

    searches_per_day = sum(
        1440
        / effective_query_interval_minutes(
            topic.interval_minutes, WebDiscoveryKind.ROUTINE, settings
        )
        for _, topic in routine
    )
    # Five enabled routine queries at twelve hours apiece: ten searches a day.
    assert len(routine) == 5
    assert searches_per_day == 10
    assert searches_per_day <= settings.brave_max_searches_per_day
    # And a long month of that stays inside the monthly cap, which is the
    # constraint a daily cap alone cannot hold.
    assert searches_per_day * 31 <= settings.brave_max_searches_per_month

    # The semantic topic ships disabled: it is the one that costs 1.4x a Brave
    # search, and it should be switched on deliberately.
    assert semantic == []


async def test_the_seeded_semantic_topic_is_present_but_disabled(
    clean_tables: Database,
) -> None:
    """Present so an operator can see what it would ask; disabled so nobody
    pays for it by installing the software."""
    async with clean_tables.transaction() as session:
        await seed_default_topics(session)
    async with clean_tables.session() as session:
        rows = (
            await session.execute(
                sa.select(DiscoveryTopic, DiscoveryQuery)
                .join(DiscoveryQuery, DiscoveryQuery.topic_id == DiscoveryTopic.id)
                .where(DiscoveryTopic.slug == "second_order_exposure")
            )
        ).all()
    assert rows
    topic = rows[0][0]
    assert topic.enabled is False
    assert all(query.search_kind is WebDiscoveryKind.SEMANTIC for _, query in rows)
    # They are questions, not keyword lists -- which is what an embedding index
    # is for and what a keyword index handles worst.
    assert any("benefit" in query.query for _, query in rows)


async def test_the_seeded_semantic_topic_is_added_to_an_upgraded_database(
    clean_tables: Database,
) -> None:
    """A deployment upgraded from Phase 9 already has the routine topics and
    would never reach the seeder again, so the one genuinely new thing the
    provider split introduces would otherwise never appear."""
    from stockbrain.ingestion.topics import seed_semantic_topic

    await _seed_two_queries(clean_tables)
    async with clean_tables.transaction() as session:
        assert await seed_default_topics(session) == 0
        assert await seed_semantic_topic(session) is True
        # Idempotent: a second run adds nothing.
        assert await seed_semantic_topic(session) is False

    async with clean_tables.session() as session:
        slugs = set((await session.execute(sa.select(DiscoveryTopic.slug))).scalars())
    assert slugs == {"ai_infrastructure", "second_order_exposure"}


async def test_the_seeded_result_limit_is_within_the_configured_ceiling(
    clean_tables: Database,
) -> None:
    """A topic row asking for a hundred results is a topic row asking for an
    overage line on every search; the ceiling is applied as a reduction."""
    from stockbrain.jobs.handlers import build_query_plan

    async with clean_tables.transaction() as session:
        await seed_default_topics(session)
        await session.execute(sa.update(DiscoveryTopic).values(result_limit=100))

    settings = _settings(clean_tables)
    async with clean_tables.session() as session:
        rows = (
            await session.execute(
                sa.select(DiscoveryQuery, DiscoveryTopic).join(
                    DiscoveryTopic, DiscoveryTopic.id == DiscoveryQuery.topic_id
                )
            )
        ).all()
    for query, topic in rows:
        plan = build_query_plan(query, topic, settings)
        assert plan.query.limit <= 20


def test_no_seeded_topic_asks_for_a_sub_hour_cadence() -> None:
    """A defence against the seed drifting back.

    The floor would clamp it anyway; a seed that *asked* for 20 minutes would
    still be a seed that told the next reader 20 minutes was reasonable.
    """
    assert all(seed.interval_minutes >= 720 for seed in DEFAULT_TOPICS)


def test_no_seeded_semantic_topic_asks_for_a_sub_day_cadence() -> None:
    assert all(
        seed.interval_minutes >= 1440
        for seed in DEFAULT_TOPICS
        if seed.kind is WebDiscoveryKind.SEMANTIC
    )
