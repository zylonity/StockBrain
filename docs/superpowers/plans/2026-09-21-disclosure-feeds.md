# Disclosure Feeds (non-US news) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five keyless, per-wire disclosure feeds (Investegate, EQS, CNMV, GlobeNewswire, Actusnews) that land non-US regulatory and issuer releases on the ordinary ingestion path, collapse translations into one event, and hand provider-known ticker/exchange/ISIN identity to instrument resolution.

**Architecture:** A shared `FeedItem` DTO + `DisclosureFeed` protocol + text-only `FeedHttpClient` (foundation, Task 1) feeds five fixture-driven adapters (Tasks 2–5). A `DISCLOSURE_FEED_POLL` handler walks pages until it reaches an already-known `release_id`, filters boilerplate, groups language variants, and calls the existing `IngestionService.ingest_many` (Task 6). The classifier stage backfills `exchange_hint` from source metadata and passes a source ISIN through to `ResolutionRequest` (Task 7). Language hardening makes extraction honour a source's language and broadens name-suffix normalisation (Task 8). Docs, opt-in live tests and full verification close it out (Task 9).

**Tech Stack:** Python 3.12, `lxml` (already a transitive pin — **never add a dependency**), `httpx`, SQLAlchemy 2 async, Alembic, pytest (`-m "not live"` by default), ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-21-disclosure-feeds-design.md` — the plan argues from the spec; executors read both.

## Global Constraints

- **No new dependency.** `lxml` is already pinned transitively; there is no `feedparser`.
- **No prompt file change and no `PROMPT_VERSION` change.** `prompts/event_classifier/v1.md` is untouched; only `classifier.py` template *values* gain `exchange:`/`isin:`.
- **Body is always `None`.** A poll never fetches an article/announcement page; bodies are `CONTENT_EXTRACT`'s job on demand.
- **No retry inside a feed fetch.** The next scheduled poll is the retry. `FeedHttpClient` always calls the client with `retry_safe=False`.
- **Never store a filtered (boilerplate) item.**
- **Never assign a generic `SourceProvider`**; each wire is its own value.
- **Every feed defaults off**; `DISCLOSURE_FEEDS_ENABLED=false` is the master gate and nothing below it runs.
- **Do not touch thesis/proposal code paths**, `previous_thesis_id`, or the resolver's rungs.
- **Identity ISIN must actually reach `ResolutionRequest`.** `event_company_impacts` has no ISIN column; do not invent one — read the event's primary source `provider_metadata` in the resolution flow.
- **Do not read `.env` secrets in tests.** Unit tests use explicit `Settings(...)` values; integration tests use the `clean_tables`/`migrated_database` fixtures.
- **Do not start compose** for unit work. Integration tests need only the `stockbrain_test` database.
- **Preserve unrelated working-tree changes.** `STOCKBRAIN_TECHNICAL_SPEC.md`, `handoff.md`, `firecrawl_activity_logs.csv` and the LLM archive CSV are pre-existing and must not be staged.

**Owned-file map (no shared writes):**

| Task | Wave | Owned files |
|---|---|---|
| 1 | serial | `backend/stockbrain/enums.py`, `backend/stockbrain/observability/health.py`, `backend/stockbrain/config.py`, `backend/alembic/versions/20260921_1200_disclosure_feeds.py`, `backend/stockbrain/ingestion/disclosure_feeds.py`, `backend/stockbrain/ingestion/service.py`, `backend/stockbrain/ingestion/dedupe.py`, `backend/tests/unit/test_disclosure_feeds.py`, `backend/tests/unit/test_disclosure_settings.py`, `backend/tests/integration/test_disclosure_dedupe.py`, `backend/tests/integration/test_migration_disclosure_feeds.py` |
| 2 | parallel | `backend/stockbrain/ingestion/investegate.py`, `backend/tests/unit/test_investegate_feed.py` |
| 3 | parallel | `backend/stockbrain/ingestion/eqs.py`, `backend/tests/unit/test_eqs_feed.py` |
| 4 | parallel | `backend/stockbrain/ingestion/cnmv.py`, `backend/tests/unit/test_cnmv_feed.py` |
| 5 | parallel | `backend/stockbrain/ingestion/globenewswire.py`, `backend/stockbrain/ingestion/actusnews.py`, `backend/tests/unit/test_globenewswire_feed.py`, `backend/tests/unit/test_actusnews_feed.py` |
| 6 | parallel | `backend/stockbrain/jobs/disclosure.py`, `backend/stockbrain/services.py`, `backend/tests/integration/test_disclosure_handler.py`, `backend/tests/integration/test_disclosure_scheduler.py` |
| 7 | parallel | `backend/stockbrain/intelligence/classifier.py`, `backend/stockbrain/intelligence/service.py`, `backend/stockbrain/instruments/service.py`, `backend/tests/integration/test_disclosure_identity.py` |
| 8 | parallel | `backend/stockbrain/extraction/base.py`, `backend/stockbrain/extraction/local.py`, `backend/stockbrain/extraction/firecrawl.py`, `backend/stockbrain/instruments/normalize.py`, `backend/stockbrain/jobs/handlers.py`, `backend/tests/unit/test_local_extraction.py`, `backend/tests/unit/test_instruments_normalize.py`, `backend/tests/integration/test_content_extraction.py` |
| 9 | serial | `.env.example`, `backend/pyproject.toml`, `backend/tests/integration/test_disclosure_feeds_live.py` |

**Ownership note:** Task 6 owns `jobs/disclosure.py` (a new, focused handler module) and `services.py`; Task 8 owns `jobs/handlers.py` (the language call site) and the extraction modules. That keeps the two concurrent tasks on disjoint files while still honoring Task 8's ownership of the per-source-language call sites. If an executor would rather put the handler in `jobs/handlers.py`, it must first serialize tasks 6 and 8 — which the dispatch forbids.

---

## Task 1: Foundation — DTO, protocol, filter, grouping, fetch, enums, migration, settings

**Files:**
- Modify: `backend/stockbrain/config.py`
- Modify: `backend/stockbrain/enums.py`
- Modify: `backend/stockbrain/observability/health.py`
- Create: `backend/alembic/versions/20260921_1200_disclosure_feeds.py`
- Create: `backend/stockbrain/ingestion/disclosure_feeds.py`
- Modify: `backend/stockbrain/ingestion/service.py`
- Modify: `backend/stockbrain/ingestion/dedupe.py`
- Test: `backend/tests/unit/test_disclosure_settings.py`
- Test: `backend/tests/unit/test_disclosure_feeds.py`
- Test: `backend/tests/integration/test_migration_disclosure_feeds.py`
- Test: `backend/tests/integration/test_disclosure_dedupe.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces (used verbatim by Tasks 2–9):
  - `FeedItem(provider, release_id, language, url, headline, published_at, company_name=None, ticker=None, isin=None, exchange_hint=None, category=None, alternate_language_urls={}, raw={})`
  - `DisclosureFeed` protocol: `name: str`, `provider: SourceProvider`, `native_language: str`, `max_pages: int`, `allow_empty: bool`, `async fetch_page(page: int) -> list[FeedItem]`, `async aclose() -> None`
  - `FeedHttpClient(settings, *, provider: str, base_url: str, language: str, client: httpx.AsyncClient | None = None)` with `async get(url: str, *, language: str) -> str` and `aclose()`
  - `to_document(item: FeedItem) -> RawSourceDocument`
  - `is_boilerplate(item: FeedItem) -> bool`
  - `group_releases(items: Sequence[FeedItem], native_language: str = "en") -> list[FeedItem]`
  - `SourceProvider.INVESTEGATE|EQS|CNMV|GLOBENEWSWIRE|ACTUSNEWS`
  - `ProviderName.INVESTEGATE|EQS|CNMV|GLOBENEWSWIRE|ACTUSNEWS`
  - `JobType.DISCLOSURE_FEED_POLL`
  - `IngestionService.known_release_ids(provider: SourceProvider, release_ids: Sequence[str]) -> set[str]`
  - `Settings.disclosure_feeds_enabled|disclosure_feed_user_agent_contact|disclosure_feed_timeout_seconds|investegate_enabled|investegate_interval_seconds|investegate_max_pages|eqs_enabled|eqs_interval_seconds|cnmv_enabled|cnmv_interval_seconds|globenewswire_enabled|globenewswire_countries|globenewswire_interval_seconds|actusnews_enabled|actusnews_interval_seconds`

---

- [ ] **Step 1: Write the failing settings test**

Create `backend/tests/unit/test_disclosure_settings.py`:

```python
"""The new disclosure-feed settings: defaults off and CSV parsing."""

from __future__ import annotations

from typing import Any

import pytest

from stockbrain.config import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def test_every_disclosure_feed_ships_disabled() -> None:
    settings = _settings()
    assert settings.disclosure_feeds_enabled is False
    assert settings.investegate_enabled is False
    assert settings.eqs_enabled is False
    assert settings.cnmv_enabled is False
    assert settings.globenewswire_enabled is False
    assert settings.actusnews_enabled is False


def test_disclosure_feed_defaults_match_the_spec() -> None:
    settings = _settings()
    assert settings.disclosure_feed_timeout_seconds == 20.0
    assert settings.investegate_interval_seconds == 300.0
    assert settings.investegate_max_pages == 3
    assert settings.eqs_interval_seconds == 300.0
    assert settings.cnmv_interval_seconds == 600.0
    assert settings.globenewswire_interval_seconds == 900.0
    assert settings.actusnews_interval_seconds == 900.0
    assert settings.globenewswire_countries == [
        "France",
        "Netherlands",
        "Belgium",
        "Portugal",
        "Spain",
        "Canada",
    ]


def test_globenewswire_countries_accepts_a_comma_separated_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLOBENEWSWIRE_COUNTRIES", "France,Canada")
    assert _settings().globenewswire_countries == ["France", "Canada"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_disclosure_settings.py -q`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'disclosure_feeds_enabled'`.

- [ ] **Step 3: Add the settings**

In `backend/stockbrain/config.py`, immediately after `CommaSeparatedStrs = Annotated[list[str], NoDecode, Field(default_factory=list)]` (line 120), add:

```python
GlobeNewswireCountries = Annotated[list[str], NoDecode]
"""A list with a non-empty default, so it cannot reuse ``CommaSeparatedStrs``
(which already carries ``default_factory=list`` and would raise)."""
```

Then, immediately after the SEC EDGAR block (after `sec_www_base_url: str = "https://www.sec.gov"`), insert:

```python
    # ------------------------------------------------------------------
    # Keyless non-US disclosure feeds (all default off)
    # ------------------------------------------------------------------
    disclosure_feeds_enabled: bool = False
    """Master gate.  Nothing below it runs: no client, no schedule, no job."""
    disclosure_feed_user_agent_contact: str = ""
    """Contact for the shared User-Agent; falls back to SEC_CONTACT_EMAIL."""
    disclosure_feed_timeout_seconds: float = Field(default=20.0, ge=5.0, le=120.0)

    investegate_enabled: bool = False
    investegate_interval_seconds: float = Field(default=300.0, ge=60.0, le=86400.0)
    investegate_max_pages: int = Field(default=3, ge=1, le=20)

    eqs_enabled: bool = False
    eqs_interval_seconds: float = Field(default=300.0, ge=60.0, le=86400.0)

    cnmv_enabled: bool = False
    cnmv_interval_seconds: float = Field(default=600.0, ge=60.0, le=86400.0)

    globenewswire_enabled: bool = False
    globenewswire_countries: GlobeNewswireCountries = Field(
        default_factory=lambda: [
            "France",
            "Netherlands",
            "Belgium",
            "Portugal",
            "Spain",
            "Canada",
        ]
    )
    globenewswire_interval_seconds: float = Field(default=900.0, ge=60.0, le=86400.0)

    actusnews_enabled: bool = False
    actusnews_interval_seconds: float = Field(default=900.0, ge=60.0, le=86400.0)
```

Then add `"globenewswire_countries",` to the field list of the existing `_split_csv` validator (the `@field_validator("telegram_allowed_user_ids", ...)` decorator). The final decorator argument list ends:

```python
    @field_validator(
        "telegram_allowed_user_ids",
        "telegram_allowed_chat_ids",
        "cors_allow_origins",
        "risk_allowed_instrument_types",
        "risk_allowed_sessions",
        "brave_result_filter",
        "globenewswire_countries",
        mode="before",
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_disclosure_settings.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Write the failing migration test**

Create `backend/tests/integration/test_migration_disclosure_feeds.py`:

```python
"""The disclosure-feed enum migration against a real database."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

DISCLOSURE = "d47f2a9c81e5"
PREVIOUS = "b7d2e4f1a9c3"
NEW_LABELS = {"INVESTEGATE", "EQS", "CNMV", "GLOBENEWSWIRE", "ACTUSNEWS"}


def _config(url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _enum_labels(url: str, type_name: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t "
                    "ON t.oid = e.enumtypid WHERE t.typname = :name"
                ),
                {"name": type_name},
            )
            return {str(row[0]) for row in rows}
    finally:
        await engine.dispose()


def test_source_provider_gains_the_disclosure_wire_labels(migrated_database: str) -> None:
    labels = asyncio.run(_enum_labels(migrated_database, "source_provider"))
    assert NEW_LABELS <= labels


def test_the_migration_round_trips(migrated_database: str) -> None:
    config = _config(migrated_database)
    try:
        command.downgrade(config, PREVIOUS)
        command.upgrade(config, DISCLOSURE)
        command.upgrade(config, "head")
        command.check(config)
    finally:
        command.upgrade(config, "head")


def test_the_migration_imports_no_application_code() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20260921_1200_disclosure_feeds.py"
    ).read_text()
    imports = [
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    ]
    assert not any("stockbrain" in line for line in imports), imports
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_migration_disclosure_feeds.py -q`
Expected: FAIL — the migration file does not exist and the enum labels are absent. (If no test database is available, the integration tests skip; the enum additions are still required by the unit and parser tests.)

- [ ] **Step 7: Add the enums, health names and migration**

In `backend/stockbrain/enums.py`, extend `SourceProvider` (after `MANUAL = "MANUAL"`):

```python
    # Keyless non-US disclosure wires.  Each is its own provenance value; there
    # is deliberately no generic "DISCLOSURE" member.
    INVESTEGATE = "INVESTEGATE"
    EQS = "EQS"
    CNMV = "CNMV"
    GLOBENEWSWIRE = "GLOBENEWSWIRE"
    ACTUSNEWS = "ACTUSNEWS"
```

Extend `JobType` (after `SEC_REFRESH = "SEC_REFRESH"`):

```python
    DISCLOSURE_FEED_POLL = "DISCLOSURE_FEED_POLL"
    """One disclosure feed's walk-until-known poll; the feed name is payload."""
```

In `backend/stockbrain/observability/health.py`, extend `ProviderName` (after `TRADINGAGENTS = "tradingagents"`):

```python
    INVESTEGATE = "investegate"
    EQS = "eqs"
    CNMV = "cnmv"
    GLOBENEWSWIRE = "globenewswire"
    ACTUSNEWS = "actusnews"
```

Do **not** add the feeds to `SUBSYSTEM_PROVIDERS`: they default off, and an
unchecked feed would otherwise drag the discovery subsystem to `DEGRADED` on a
fresh install.  Each feed is visible individually on the provider board.

Create `backend/alembic/versions/20260921_1200_disclosure_feeds.py`:

```python
"""Disclosure-feed provenance labels.

Revision ID: d47f2a9c81e5
Revises: b7d2e4f1a9c3

``SourceProvider`` gains the five keyless non-US wires so a source row's
provenance says which wire carried it.  ``JobType`` and ``ProviderName`` are
free text in the database (``jobs.job_type``, ``provider_health.provider``), so
neither needs a PostgreSQL type change; only ``source_provider`` is a native
enum.

No application code is imported.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d47f2a9c81e5"
down_revision: str | None = "b7d2e4f1a9c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS so a database that has somehow seen a label already is not
    # stranded.  ADD VALUE cannot run inside a transaction before PostgreSQL 12;
    # this deployment is on 17.
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'INVESTEGATE'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'EQS'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'CNMV'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'GLOBENEWSWIRE'")
    op.execute("ALTER TYPE source_provider ADD VALUE IF NOT EXISTS 'ACTUSNEWS'")


def downgrade() -> None:
    # PostgreSQL cannot remove an enum value, and recreating the type would
    # rewrite every source row.  An unused label is inert; a downgrade that
    # destroys provenance is not.  Same posture as the Brave/Exa migration.
    pass
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_migration_disclosure_feeds.py -q`
Expected: PASS (3 passed) when the test database is up; skipped otherwise.

- [ ] **Step 9: Write the failing DTO/filter/grouping test**

Create `backend/tests/unit/test_disclosure_feeds.py`:

```python
"""Unit tests for the shared disclosure-feed vocabulary."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.errors import ProviderUnavailable
from stockbrain.ingestion.disclosure_feeds import (
    FeedHttpClient,
    FeedItem,
    group_releases,
    is_boilerplate,
    to_document,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _item(
    *,
    provider: SourceProvider = SourceProvider.INVESTEGATE,
    release_id: str = "9782378",
    language: str = "en",
    url: str = "https://www.investegate.co.uk/announcement/rns/barclays--barc/x/9782378",
    headline: str = "Interim Results",
    published_at: dt.datetime | None = None,
    company_name: str | None = "Barclays",
    ticker: str | None = "BARC",
    isin: str | None = None,
    exchange_hint: str | None = "London Stock Exchange",
    category: str | None = "RNS",
) -> FeedItem:
    return FeedItem(
        provider=provider,
        release_id=release_id,
        language=language,
        url=url,
        headline=headline,
        published_at=published_at or dt.datetime(2026, 9, 21, 11, 46, tzinfo=dt.UTC),
        company_name=company_name,
        ticker=ticker,
        isin=isin,
        exchange_hint=exchange_hint,
        category=category,
        raw={"source": category},
    )


# ---------------------------------------------------------------------------
# to_document
# ---------------------------------------------------------------------------
def test_to_document_maps_provider_identity_and_language() -> None:
    document = to_document(_item())
    assert document.provider is SourceProvider.INVESTEGATE
    assert document.provider_item_id == "9782378"
    assert document.url.endswith("/9782378")
    assert document.source_name == "Investegate"
    assert document.symbols == ["BARC"]
    assert document.body is None
    assert document.metadata["language"] == "en"
    assert document.metadata["company_name"] == "Barclays"
    assert document.metadata["feed_category"] == "RNS"


def test_regulator_disclosures_are_distinct_events() -> None:
    assert to_document(_item(category="RNS")).source_category is SourceCategory.REGULATOR
    assert to_document(_item(category="RNS")).is_distinct_event is True
    newswire = _item(provider=SourceProvider.GLOBENEWSWIRE, category="Other News")
    assert to_document(newswire).source_category is SourceCategory.ISSUER
    assert to_document(newswire).is_distinct_event is False


def test_an_rns_is_regulator_but_another_investegate_code_is_issuer() -> None:
    assert to_document(_item(category="RNS")).source_category is SourceCategory.REGULATOR
    assert to_document(_item(category="PRN")).source_category is SourceCategory.ISSUER


def test_an_eqs_regulatory_category_is_regulator_but_corporate_is_issuer() -> None:
    regulatory = _item(
        provider=SourceProvider.EQS,
        ticker=None,
        exchange_hint=None,
        category="voting-rights",
        isin="DE0006231004",
    )
    corporate = _item(
        provider=SourceProvider.EQS,
        ticker=None,
        exchange_hint=None,
        category="corporate",
        isin="DE000A1H8BV3",
    )
    assert to_document(regulatory).source_category is SourceCategory.REGULATOR
    assert to_document(regulatory).is_distinct_event is True
    assert to_document(corporate).source_category is SourceCategory.ISSUER
    assert to_document(corporate).is_distinct_event is False


def test_investegate_carries_both_lse_and_aim_as_alternates() -> None:
    metadata = to_document(_item()).metadata
    assert metadata["exchange_hint"] == "London Stock Exchange"
    assert metadata["exchange_hints"] == [
        "London Stock Exchange",
        "London Stock Exchange AIM",
    ]


def test_the_issuer_name_is_prefixed_onto_a_headline_that_omits_it() -> None:
    # "Barclays" names no company in a generic headline: prefixed.
    assert to_document(_item()).headline == "Barclays: Interim Results"
    # The issuer is already in the headline (any case): left alone.
    same_headline = _item(
        company_name="Barclays", headline="Barclays PLC announces interim results"
    )
    assert to_document(same_headline).headline == "Barclays PLC announces interim results"
    # No company name at all: the raw headline is used verbatim.
    assert to_document(_item(company_name=None)).headline == "Interim Results"


def test_a_malformed_item_is_refused_by_mapping() -> None:
    with pytest.raises(ValueError, match="url is not an absolute http"):
        to_document(_item(url=""))
    with pytest.raises(ValueError, match="url is not an absolute http"):
        to_document(_item(url="/announcement/rns/barclays--barc/x/9782378"))
    with pytest.raises(ValueError, match="headline is empty"):
        to_document(_item(headline=""))
    with pytest.raises(ValueError, match="release_id is empty"):
        to_document(_item(release_id=""))
    with pytest.raises(ValueError, match="published_at is naive"):
        to_document(
            _item(published_at=dt.datetime(2026, 9, 21, 11, 46))  # noqa: DTZ001 -- the point
        )


def test_a_non_utc_timestamp_is_normalised_to_utc() -> None:
    eastern = dt.timezone(dt.timedelta(hours=-4))
    item = _item(published_at=dt.datetime(2026, 9, 21, 7, 46, tzinfo=eastern))
    assert to_document(item).published_at == dt.datetime(2026, 9, 21, 11, 46, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# is_boilerplate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "headline",
    [
        "Transaction in Own Shares",
        "Holding(s) in Company",
        "Director/PDMR Shareholding",
        "Form 8.3 - DCC Energy plc 210926",
        "Form 38.5a (EPT/RI) DCC Energy plc BNPP FM",
        "Exercise of Warrants",
        "Investor Presentation via Investor Meet Company",
    ],
)
def test_investegate_boilerplate_headlines_are_filtered(headline: str) -> None:
    assert is_boilerplate(_item(headline=headline)) is True


def test_a_substantive_investegate_headline_survives() -> None:
    assert is_boilerplate(_item(headline="Interim Results")) is False
    assert is_boilerplate(_item(headline="Result of Tender Offer")) is False


def test_eqs_category_and_headline_rules_apply() -> None:
    assert is_boilerplate(
        _item(provider=SourceProvider.EQS, category="voting-rights", headline="")
    )
    assert is_boilerplate(
        _item(provider=SourceProvider.EQS, category="directors-dealings", headline="")
    )
    assert is_boilerplate(
        _item(
            provider=SourceProvider.EQS,
            category="other-capital-market-information",
            headline="Release of a capital market information",
            ticker=None,
            exchange_hint=None,
        )
    )
    assert not is_boilerplate(
        _item(provider=SourceProvider.EQS, category="corporate", headline="Interim Results")
    )


def test_cnmv_inside_information_is_never_filtered() -> None:
    item = _item(
        provider=SourceProvider.CNMV,
        category="Información privilegiada",
        headline="Sobre suspensiones, levantamientos y exclusiones de negociación",
        company_name="SOCIETE GENERALE EFFEKTEN GMBH",
        ticker=None,
        exchange_hint="Bolsa de Madrid",
    )
    assert is_boilerplate(item) is False


def test_cnmv_oir_buyback_is_filtered_but_a_real_suspension_is_not() -> None:
    buyback = _item(
        provider=SourceProvider.CNMV,
        category="Otra información relevante",
        headline="Programas de recompra de acciones, estabilización y autocartera",
        company_name="EDREAMS ODIGEO, S.A.",
        ticker=None,
        exchange_hint="Bolsa de Madrid",
    )
    real = _item(
        provider=SourceProvider.CNMV,
        category="Otra información relevante",
        headline="Sobre suspensiones, levantamientos y exclusiones de negociación",
        company_name="ERCROS, S.A. (ERCROS)",
        ticker=None,
        exchange_hint="Bolsa de Madrid",
    )
    warrant_issuer = _item(
        provider=SourceProvider.CNMV,
        category="Otra información relevante",
        headline="Sobre suspensiones, levantamientos y exclusiones de negociación",
        company_name="SOCIETE GENERALE EFFEKTEN GMBH",
        ticker=None,
        exchange_hint="Bolsa de Madrid",
    )
    assert is_boilerplate(buyback) is True
    assert is_boilerplate(real) is False
    assert is_boilerplate(warrant_issuer) is True


def test_globenewswire_boilerplate_survives_typographic_apostrophes() -> None:
    item = _item(
        provider=SourceProvider.GLOBENEWSWIRE,
        headline=(
            "RIBER: INFORMATION MENSUELLE RELATIVE AU NOMBRE TOTAL "
            "D\u2019ACTIONS ET DE DROITS DE VOTE COMPOSANT LE CAPITAL SOCIAL"
        ),
        ticker=None,
        exchange_hint=None,
    )
    assert is_boilerplate(item) is True


def test_actusnews_has_no_boilerplate_rules() -> None:
    item = _item(
        provider=SourceProvider.ACTUSNEWS,
        headline="Number of outstanding shares and voting rights",
        ticker=None,
        exchange_hint=None,
    )
    assert is_boilerplate(item) is False


# ---------------------------------------------------------------------------
# group_releases
# ---------------------------------------------------------------------------
def test_group_releases_prefers_english_and_keeps_the_other_urls() -> None:
    english = _item(language="en", url="https://example.com/en")
    french = _item(language="fr", url="https://example.com/fr")
    grouped = group_releases([french, english])
    assert len(grouped) == 1
    assert grouped[0].language == "en"
    assert grouped[0].url == "https://example.com/en"
    assert grouped[0].alternate_language_urls == {"fr": "https://example.com/fr"}


def test_group_releases_falls_back_to_native_then_first() -> None:
    german = _item(language="de", url="https://example.com/de")
    french = _item(language="fr", url="https://example.com/fr")
    assert group_releases([german, french], "de")[0].language == "de"
    assert group_releases([german, french], "it")[0].language == "de"


def test_group_releases_keeps_distinct_releases_separate() -> None:
    first = _item(release_id="1", language="en")
    second = _item(release_id="2", language="en")
    assert len(group_releases([first, second])) == 2


# ---------------------------------------------------------------------------
# FeedHttpClient
# ---------------------------------------------------------------------------
async def test_feed_http_client_returns_text_and_maps_429_to_unavailable() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "30"}, text="slow down")
        return httpx.Response(200, text="<rss>ok</rss>")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://feeds.example"
    )
    http = FeedHttpClient(
        _settings(), provider="test", base_url="https://feeds.example", language="en", client=client
    )
    try:
        with pytest.raises(ProviderUnavailable):
            await http.get("/feed", language="en")
        # No retry: one request for one poll attempt.
        assert len(calls) == 1
        assert await http.get("/feed", language="en") == "<rss>ok</rss>"
    finally:
        await http.aclose()
    # The injected client belongs to the caller and must not be closed.
    assert client.is_closed is False
    await client.aclose()


async def test_feed_http_client_sends_the_request_language_and_user_agent() -> None:
    """Headers must reach the actual request even through an injected client.

    ``ProviderHttpClient.__init__`` only applies its ``headers=`` kwarg when it
    creates its own ``httpx.AsyncClient``; an injected client (every test's
    only way to control the transport) silently drops them. ``FeedHttpClient``
    must therefore carry its own default headers and pass them on every
    request explicitly, not rely on the client construction path.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["language"] = request.headers.get("accept-language", "")
        seen["user-agent"] = request.headers.get("user-agent", "")
        seen["accept"] = request.headers.get("accept", "")
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://feeds.example"
    )
    http = FeedHttpClient(
        _settings(), provider="test", base_url="https://feeds.example", language="en", client=client
    )
    try:
        await http.get("/feed", language="de")
    finally:
        await http.aclose()
        await client.aclose()
    assert seen["language"] == "de"
    assert seen["user-agent"] == "StockBrain/0.1.0 (+unset)"
    assert seen["accept"].startswith("text/html")
```

- [ ] **Step 10: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_disclosure_feeds.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.ingestion.disclosure_feeds'`.

- [ ] **Step 11: Implement the shared module**

Create `backend/stockbrain/ingestion/disclosure_feeds.py`:

```python
"""Shared vocabulary for the keyless non-US disclosure feeds.

One release from one wire becomes one :class:`FeedItem`; the handler filters
boilerplate, groups language variants, and maps the survivors onto the ordinary
:class:`~stockbrain.ingestion.base.RawSourceDocument` path.  Nothing here
interprets content.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from stockbrain.config import Settings
from stockbrain.enums import SourceCategory, SourceProvider
from stockbrain.errors import ProviderRateLimited, ProviderUnavailable
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.ingestion.base import RawSourceDocument

__all__ = [
    "BoilerplateRule",
    "DisclosureFeed",
    "FeedHttpClient",
    "FeedItem",
    "group_releases",
    "is_boilerplate",
    "to_document",
]

#: A boilerplate rule: a compiled pattern and the human-readable rule id it
#: came from.  Data rather than code so the table can grow without a logic
#: review.
BoilerplateRule = tuple[re.Pattern[str], str]

_CNMV_IP_LABEL = "Información privilegiada"
_CNMV_OIR_LABEL = "Otra información relevante"

#: Typographic apostrophes are normalised before matching so a rule written
#: with an ASCII apostrophe still matches a feed that prints ``’``.
_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'"})

_BOILERPLATE_RULES: dict[SourceProvider, tuple[BoilerplateRule, ...]] = {
    SourceProvider.INVESTEGATE: tuple(
        (re.compile(pattern, re.IGNORECASE), pattern)
        for pattern in (
            r"Transaction in Own Shares",
            r"Holding\(s\) in Company",
            r"Director/PDMR Shareholding",
            r"\bForm 8\b",
            r"\bForm 38\.5",
            r"Total Voting Rights",
            r"Exercise of Warrants",
            r"Block Listing",
            r"Investor Presentation via Investor Meet Company",
        )
    ),
    SourceProvider.EQS: (
        (re.compile(r"voting-rights", re.IGNORECASE), "EQS category voting-rights"),
        (re.compile(r"directors-dealings", re.IGNORECASE), "EQS category directors-dealings"),
        (
            re.compile(r"Release of a capital market information", re.IGNORECASE),
            "Art. 5 capital market information",
        ),
        (re.compile(r"Transaction in Own Shares", re.IGNORECASE), "Transaction in Own Shares"),
    ),
    SourceProvider.CNMV: (
        (
            re.compile(r"Programas de recompra de acciones", re.IGNORECASE),
            "Programas de recompra de acciones",
        ),
        (re.compile(r"Contratos de liquidez", re.IGNORECASE), "Contratos de liquidez"),
        (
            re.compile(
                r"Sobre suspensiones.*(?:SOCIETE GENERALE EFFEKTEN"
                r"|BNP PARIBAS ISSUANCE|CITIGROUP GLOBAL MARKETS)",
                re.IGNORECASE | re.DOTALL,
            ),
            "Sobre suspensiones for warrant issuers",
        ),
    ),
    SourceProvider.GLOBENEWSWIRE: tuple(
        (re.compile(pattern, re.IGNORECASE), pattern)
        for pattern in (
            r"Déclaration des opérations de rachat",
            r"Disclosure of trading in own shares",
            r"Déclaration hebdomadaire des transactions",
            r"Number of outstanding shares and voting rights",
            # The fixture prints "NOMBRE TOTAL D'ACTIONS ET DE DROITS DE VOTE";
            # the optional "total" matches the real wording without widening
            # the rule to unrelated notices.
            r"Nombre (?:total )?d'actions et de droits de vote",
        )
    ),
}

_SOURCE_NAMES: dict[SourceProvider, str] = {
    SourceProvider.INVESTEGATE: "Investegate",
    SourceProvider.EQS: "EQS News",
    SourceProvider.CNMV: "CNMV",
    SourceProvider.GLOBENEWSWIRE: "GlobeNewswire",
    SourceProvider.ACTUSNEWS: "Actusnews Wire",
}

_EQS_REGULATORY_CATEGORIES = frozenset(
    {
        "voting-rights",
        "directors-dealings",
        "other-capital-market-information",
        "uk-regulatory",
        "ad-hoc",
    }
)


@dataclass(slots=True)
class FeedItem:
    """One release as a feed printed it, before it becomes a source row."""

    provider: SourceProvider
    release_id: str
    language: str
    url: str
    headline: str
    published_at: dt.datetime
    company_name: str | None = None
    ticker: str | None = None
    isin: str | None = None
    exchange_hint: str | None = None
    category: str | None = None
    alternate_language_urls: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DisclosureFeed(Protocol):
    """One pollable disclosure list."""

    name: str
    provider: SourceProvider
    native_language: str
    max_pages: int
    allow_empty: bool

    async def fetch_page(self, page: int) -> list[FeedItem]: ...

    async def aclose(self) -> None: ...


class FeedHttpClient(ProviderHttpClient):
    """The shared fetch helper: plain text, one attempt, classified errors.

    ``ProviderHttpClient`` decodes JSON and classifies 429 as a rate-limit
    error.  A disclosure feed answers HTML or XML, and the whole poll is the
    unit that must stop on a 429 -- so text is returned and 429 is mapped to
    :class:`ProviderUnavailable`, which the handler records as ``DOWN``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        provider: str,
        base_url: str,
        language: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        contact = (
            settings.disclosure_feed_user_agent_contact.strip()
            or settings.sec_contact_email.strip()
            or "unset"
        )
        # Kept on ``self`` and re-sent on every request in :meth:`get` below,
        # rather than relied on as client-level defaults: ``ProviderHttpClient``
        # only applies its ``headers=`` kwarg when it builds its *own*
        # ``httpx.AsyncClient``.  An injected client -- every test's only way to
        # control the transport -- silently drops them otherwise.
        self._default_headers = {
            "User-Agent": f"{settings.app_name}/{settings.app_version} (+{contact})",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/rss+xml;q=0.8,text/xml;q=0.8,*/*;q=0.5"
            ),
            "Accept-Language": language,
        }
        super().__init__(
            provider=provider,
            base_url=base_url,
            headers=self._default_headers,
            timeout_seconds=settings.disclosure_feed_timeout_seconds,
            # ``client`` is passed through so a test can inject a mock transport
            # and own its lifecycle, exactly as ``ProviderHttpClient`` allows.
            client=client,
        )

    async def get(self, url: str, *, language: str) -> str:
        """GET one feed URL with no retry and the request's language preference."""
        headers = dict(self._default_headers)
        headers["Accept-Language"] = language
        return await self.request_json(
            "GET",
            url,
            headers=headers,
            retry_safe=False,
        )

    def _decode(self, response: httpx.Response) -> str:
        return response.text

    def _classify(self, response: httpx.Response) -> Exception:
        error = super()._classify(response)
        if isinstance(error, ProviderRateLimited):
            return ProviderUnavailable(f"{self.provider}: rate limited (HTTP 429)")
        return error


def _source_category(item: FeedItem) -> SourceCategory:
    """The transparent trust category a feed item maps onto."""
    if item.provider is SourceProvider.CNMV:
        return SourceCategory.REGULATOR
    if item.provider is SourceProvider.INVESTEGATE:
        return SourceCategory.REGULATOR if item.category == "RNS" else SourceCategory.ISSUER
    if item.provider is SourceProvider.EQS:
        if (item.category or "").lower() in _EQS_REGULATORY_CATEGORIES:
            return SourceCategory.REGULATOR
        return SourceCategory.ISSUER
    if item.provider in (SourceProvider.GLOBENEWSWIRE, SourceProvider.ACTUSNEWS):
        return SourceCategory.ISSUER
    return SourceCategory.UNKNOWN


def _exchange_hints(item: FeedItem) -> list[str]:
    hints: list[str] = []
    if item.exchange_hint:
        hints.append(item.exchange_hint)
    if item.provider is SourceProvider.INVESTEGATE:
        # The page does not say LSE or AIM, so both travel as alternates.
        aim = "London Stock Exchange AIM"
        if aim not in hints:
            hints.append(aim)
    return hints


def _canonical_headline(item: FeedItem) -> str:
    """Prefix the issuer name onto a headline that does not already carry it.

    Bodies are never fetched for these feeds (``body=None`` below), so the
    headline is the classifier's only readable text. A generic category label
    -- EQS's "Release of a capital market information", say -- names no
    company, and without a ticker there is nothing else for the model to
    resolve an instrument from. The raw, unprefixed headline is unaffected:
    it is still reachable through the item's own fields and ``raw_payload``.
    """
    headline = item.headline
    company = item.company_name
    if not company or company.casefold() in headline.casefold():
        return headline
    return f"{company}: {headline}"


def to_document(item: FeedItem) -> RawSourceDocument:
    """Map one feed item onto the canonical ingestion DTO.

    Validation is real rather than nominal: ``RawSourceDocument`` accepts an
    empty or relative URL, an empty headline, or a naive timestamp, so a
    malformed row is refused here instead of becoming an untitled,
    unreachable, or wrongly-timed source.
    """
    if not item.release_id or not item.release_id.strip():
        raise ValueError("release_id is empty")
    if not item.headline or not item.headline.strip():
        raise ValueError("headline is empty")
    parsed_url = urlsplit(item.url or "")
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError(f"url is not an absolute http(s) URL: {item.url!r}")
    if item.published_at.tzinfo is None:
        raise ValueError("published_at is naive, not timezone-aware")
    published_at = item.published_at.astimezone(dt.UTC)

    category = _source_category(item)
    return RawSourceDocument(
        provider=item.provider,
        provider_item_id=item.release_id,
        url=item.url,
        source_name=_SOURCE_NAMES.get(item.provider, item.provider.value.title()),
        source_category=category,
        headline=_canonical_headline(item),
        published_at=published_at,
        # Bodies are the extractor's job, on demand and budgeted.
        body=None,
        symbols=[item.ticker] if item.ticker else [],
        is_distinct_event=category is SourceCategory.REGULATOR,
        raw_payload=dict(item.raw),
        metadata={
            "language": item.language,
            "isin": item.isin,
            "exchange_hint": item.exchange_hint,
            "exchange_hints": _exchange_hints(item),
            "alternate_language_urls": dict(item.alternate_language_urls),
            "feed_category": item.category,
            "company_name": item.company_name,
        },
    )


def is_boilerplate(item: FeedItem) -> bool:
    """Whether a feed item is routine noise that must never be stored.

    Matched against the headline, the feed's own category label and the printed
    company name, so a rule may combine a category with an issuer (the CNMV
    warrant-suspension case).  The CNMV inside-information feed is never
    filtered: a suspension it carries is a real event.
    """
    rules = _BOILERPLATE_RULES.get(item.provider)
    if not rules:
        return False
    if item.provider is SourceProvider.CNMV and item.category == _CNMV_IP_LABEL:
        return False
    haystack = " \n ".join(
        part for part in (item.headline, item.category, item.company_name) if part
    ).translate(_APOSTROPHES)
    return any(pattern.search(haystack) for pattern, _ in rules)


def group_releases(items: Sequence[FeedItem], native_language: str = "en") -> list[FeedItem]:
    """Collapse one release's language variants into a single item.

    Within a poll batch the same ``(provider, release_id)`` may arrive in
    several languages.  English is preferred; otherwise the feed's native
    language; otherwise the first variant seen.  The others become
    ``alternate_language_urls`` on the chosen item, so translation collapse
    happens before ingestion rather than as a unique-index violation.
    """
    grouped: dict[tuple[SourceProvider, str], list[FeedItem]] = {}
    order: list[tuple[SourceProvider, str]] = []
    for item in items:
        key = (item.provider, item.release_id)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)

    result: list[FeedItem] = []
    for key in order:
        variants = grouped[key]
        chosen = _preferred_variant(variants, native_language)
        # ``chosen`` is a reference into the caller's own list; mutating it in
        # place would corrupt whatever else holds that item and would break a
        # second call over the same batch. Build a new mapping instead: start
        # from whatever the item already carried, add each sibling variant
        # (existing entries win on a language collision), and never record a
        # same-language variant as an "alternate" of itself.
        alternates = dict(chosen.alternate_language_urls)
        for other in variants:
            if other is chosen or not other.language or other.language == chosen.language:
                continue
            alternates.setdefault(other.language, other.url)
        result.append(replace(chosen, alternate_language_urls=alternates))
    return result


def _preferred_variant(variants: Sequence[FeedItem], native_language: str) -> FeedItem:
    for item in variants:
        if item.language == "en":
            return item
    for item in variants:
        if item.language == native_language:
            return item
    return variants[0]
```

- [ ] **Step 12: Run the test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_disclosure_feeds.py -q`
Expected: PASS.

- [ ] **Step 13: Write the failing duplicate-provenance test**

Create `backend/tests/integration/test_disclosure_dedupe.py`:

```python
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
```

- [ ] **Step 14: Run the test to verify it fails**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_dedupe.py -q`
Expected: FAIL (or skip with no database). With the enum absent the test cannot even collect.

- [ ] **Step 15: Teach ingestion about metadata-only feeds and known releases**

In `backend/stockbrain/ingestion/service.py`, extend `METADATA_ONLY_PROVIDERS`:

```python
METADATA_ONLY_PROVIDERS: frozenset[SourceProvider] = frozenset(
    {
        SourceProvider.BRAVE,
        SourceProvider.EXA,
        SourceProvider.FIRECRAWL,
        # The disclosure feeds deliver metadata only; their bodies are the
        # CONTENT_EXTRACT path's job.  Without this, the sweep would never offer
        # them and `extraction_method` would falsely claim PROVIDER.
        SourceProvider.INVESTEGATE,
        SourceProvider.EQS,
        SourceProvider.CNMV,
        SourceProvider.GLOBENEWSWIRE,
        SourceProvider.ACTUSNEWS,
    }
)
```

Add the lookup method to `IngestionService`, immediately after `ingest_many`:

```python
    async def known_release_ids(
        self, provider: SourceProvider, release_ids: Sequence[str]
    ) -> set[str]:
        """Which of these provider item ids have already been ingested.

        The ingest provider identity layer is the whole idempotency story for
        the disclosure feeds: the unique index on ``(provider,
        provider_item_id)`` makes a re-poll or a late translation a duplicate,
        and this read is how a poll discovers it has reached what it already
        has without walking every page.
        """
        wanted = [value for value in release_ids if value]
        if not wanted:
            return set()
        async with self._database.session() as session:
            rows = await session.execute(
                sa.select(Source.provider_item_id).where(
                    Source.provider == provider,
                    Source.provider_item_id.in_(wanted),
                )
            )
        return {str(value) for value in rows.scalars() if value}
```

Add `Sequence` to the existing `from collections.abc import AsyncIterator, Sequence`? The file already imports `from collections.abc import Awaitable, Callable` (line 18). Change that import to `from collections.abc import Awaitable, Callable, Sequence`.

- [ ] **Step 16: Run the test to verify it passes**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_dedupe.py -q`
Expected: PASS (skipped with no database, but the import and unit suite must still pass).

- [ ] **Step 17: Run the whole new unit surface**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_disclosure_settings.py tests/unit/test_disclosure_feeds.py -q`
Expected: PASS.

- [ ] **Step 18: Write the failing distinct-event dedupe regression**

`find_duplicate_source`'s content-hash layer (`backend/stockbrain/ingestion/dedupe.py`)
matches *any* existing `Source` with the same `content_hash`, regardless of
`provider_item_id`. For a body-less regulatory release, `content_hash(headline, "")`
is derived from the headline alone (`normalize_document` passes `html_to_text(None)`,
which is `""`, as the body). Two distinct RNS releases with a generic headline (e.g.
"Interim Results") but different `provider_item_id` and different URLs therefore hash
identically, and the second one is misclassified as a `CONTENT_HASH` duplicate instead
of becoming its own event — silently dropping a real regulatory disclosure. This is a
correctness bug in the existing dedupe module, found while building the feeds that
will produce exactly this shape of document; it is not a spec requirement, so it gets
its own regression rather than living inside the spec's boilerplate/grouping tests.

Append to `backend/tests/integration/test_disclosure_dedupe.py`:

```python
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
```

- [ ] **Step 19: Run the test to verify it fails**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_dedupe.py -q`
Expected: FAIL — the second release comes back `DUPLICATE_SOURCE` (or skip with no database).

- [ ] **Step 20: Fix the content-hash layer to respect `is_distinct_event`**

In `backend/stockbrain/ingestion/dedupe.py`, change the content-hash check in
`find_duplicate_source`:

```python
    stmt = sa.select(Source).where(Source.content_hash == normalized.content_hash)
    existing = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if existing is not None and not (
        document.is_distinct_event and document.provider_item_id
    ):
        return SourceMatch(existing, DuplicateReason.CONTENT_HASH)

    return None
```

A document that *is* the event (not a report about one) and carries a stable
provider identity has already cleared the provider-ID and canonical-URL layers
above; a headline collision on such a document is two distinct regulatory
releases, not the same wire story syndicated twice, so Layer 3 must not fire for
it. Everything else — newswire articles, press coverage, anything without a
provider item id — keeps the existing content-hash behaviour unchanged.

- [ ] **Step 21: Run the test to verify it passes, and the full dedupe suite**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_dedupe.py -q`
Expected: PASS, 2 passed (or skipped together with no database) — both the original
translation-dedupe test and the new distinct-event regression.

- [ ] **Step 22: Commit**

```bash
git add backend/stockbrain/config.py backend/stockbrain/enums.py \
  backend/stockbrain/observability/health.py \
  backend/alembic/versions/20260921_1200_disclosure_feeds.py \
  backend/stockbrain/ingestion/disclosure_feeds.py \
  backend/stockbrain/ingestion/service.py \
  backend/stockbrain/ingestion/dedupe.py \
  backend/tests/unit/test_disclosure_settings.py \
  backend/tests/unit/test_disclosure_feeds.py \
  backend/tests/integration/test_migration_disclosure_feeds.py \
  backend/tests/integration/test_disclosure_dedupe.py
git commit -m "feat(ingestion): disclosure-feed foundation -- DTO, filter, fetch, enums"
```

---

## Task 2: Investegate adapter

**Files:**
- Create: `backend/stockbrain/ingestion/investegate.py`
- Test: `backend/tests/unit/test_investegate_feed.py`

**Interfaces:**
- Consumes: `FeedHttpClient`, `FeedItem`, `DisclosureFeed` from Task 1; `Settings` fields from Task 1.
- Produces: `InvestegateFeed(settings: Settings, *, client: httpx.AsyncClient | None = None)` — `name = "investegate"`, `provider = SourceProvider.INVESTEGATE`, `native_language = "en"`, `max_pages = settings.investegate_max_pages`, `allow_empty = False`, `async fetch_page(page)`, `async aclose()`, `parse_page(content: str) -> list[FeedItem]`.

Fixture facts pinned by the tests: 50 announcement rows; the page prints Europe/London
local time (BST, UTC+1, in effect on this date), so every timestamp is converted; first
release `9782378` prints `21 Sep 2026 11:46 AM` local and is `2026-09-21 10:46 UTC`,
last `9782195` prints `21 Sep 2026 10:00 AM` local and is `2026-09-21 09:00 UTC`;
exactly 33 of 50 headlines are boilerplate.

---

- [ ] **Step 1: Write the failing parser test**

Create `backend/tests/unit/test_investegate_feed.py`:

```python
"""The Investegate front-page parser against the committed fixture."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import is_boilerplate
from stockbrain.ingestion.investegate import InvestegateFeed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
FRONT_PAGE = FIXTURES / "investegate_front_page.html"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _feed() -> InvestegateFeed:
    return InvestegateFeed(_settings())


def _items() -> list[Any]:
    return _feed().parse_page(FRONT_PAGE.read_text(encoding="utf-8"))


def test_front_page_yields_fifty_announcements() -> None:
    assert len(_items()) == 50


def test_first_row_fields() -> None:
    first = _items()[0]
    assert first.provider is SourceProvider.INVESTEGATE
    assert first.release_id == "9782378"
    assert first.language == "en"
    assert first.url == (
        "https://www.investegate.co.uk/announcement/rns/barclays--barc/"
        "irish-form-38-5-b-dcc-energy-plc/9782378"
    )
    assert first.headline == "Irish Form 38.5 B DCC ENERGY PLC"
    # The page prints "11:46 AM" in Europe/London local time (BST, UTC+1).
    assert first.published_at == dt.datetime(2026, 9, 21, 10, 46, tzinfo=dt.UTC)
    assert first.company_name == "Barclays"
    assert first.ticker == "BARC"
    assert first.category == "RNS"
    assert first.exchange_hint == "London Stock Exchange"


def test_last_row_fields() -> None:
    last = _items()[-1]
    assert last.release_id == "9782195"
    assert last.headline == "TradersYard Shifts Focus to Futures Trading"
    assert last.company_name == "FinanceWire News"
    assert last.ticker == "FNEWS"
    assert last.category == "FNW"
    assert last.published_at == dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)


def test_boilerplate_count_matches_the_fixture() -> None:
    items = _items()
    assert sum(1 for item in items if is_boilerplate(item)) == 33


def test_the_adapter_walks_three_pages_by_default() -> None:
    feed = _feed()
    assert feed.max_pages == 3
    assert feed.allow_empty is False


def test_a_restyled_page_raises_provider_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        _feed().parse_page("<html><body><p>the table is gone</p></body></html>")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_investegate_feed.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.ingestion.investegate'`.

- [ ] **Step 3: Implement the adapter**

Create `backend/stockbrain/ingestion/investegate.py`:

```python
"""The Investegate front-page RNS list.

Rows are ``<tr>`` with a datetime cell, a source-code cell, a company cell and
an announcement link whose last path segment is the language-agnostic release
id.  The announcement page itself is never fetched here: the body is the
existing ``CONTENT_EXTRACT`` path's job, on demand.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from lxml import etree
from lxml import html as lxml_html

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["InvestegateFeed"]

_BASE_URL = "https://www.investegate.co.uk"
_NATIVE_LANGUAGE = "en"
_MAIN_EXCHANGE = "London Stock Exchange"

_DATETIME_FORMAT = "%d %b %Y %I:%M %p"
#: The page prints wall-clock Europe/London time (GMT/BST), not UTC.
_LONDON = ZoneInfo("Europe/London")
_COMPANY_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<ticker>[A-Za-z0-9.\-]+)\)\s*$")
_ANNOUNCEMENT_LINK = (
    'a[contains(concat(" ", normalize-space(@class), " "), " announcement-link ")]'
)


class InvestegateFeed:
    """Parse the all-market RNS list; the body is the extractor's job."""

    name = "investegate"
    provider = SourceProvider.INVESTEGATE
    native_language = _NATIVE_LANGUAGE

    def __init__(
        self, settings: Settings, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.max_pages = settings.investegate_max_pages
        self.allow_empty = False
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=_NATIVE_LANGUAGE,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        path = "/" if page <= 1 else f"/?page={page}"
        content = await self._http.get(path, language=_NATIVE_LANGUAGE)
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        """Parse one front-page table.  Raises when the table is gone."""
        if not content or not content.strip():
            raise ProviderResponseError("investegate: empty response")
        try:
            root = lxml_html.fromstring(content)
        except (etree.ParserError, etree.XMLSyntaxError) as exc:
            raise ProviderResponseError("investegate: response was not HTML") from exc

        rows = root.xpath(f"//{_ANNOUNCEMENT_LINK}/ancestor::tr[1]")
        if not rows:
            raise ProviderResponseError(
                "investegate: no announcement rows found (page shape changed)"
            )
        items = [item for row in rows if (item := self._parse_row(row)) is not None]
        if not items:
            raise ProviderResponseError("investegate: rows carried no usable announcements")
        return items

    def _parse_row(self, row: Any) -> FeedItem | None:
        cells = row.xpath("./td")
        if len(cells) < 4:
            return None
        published_at = _parse_datetime(_text(cells[0]))
        links = cells[3].xpath(f".//{_ANNOUNCEMENT_LINK}")
        if not links:
            return None
        href = (links[0].get("href") or "").strip()
        headline = _text(links[0])
        release_id = href.rstrip("/").rsplit("/", 1)[-1]
        source_code = _text(cells[1])
        company = _text(cells[2])
        name, ticker = _split_company(company)
        if not published_at or not href or not headline or not release_id or not source_code:
            return None
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=_NATIVE_LANGUAGE,
            url=href,
            headline=headline,
            published_at=published_at,
            company_name=name,
            ticker=ticker,
            exchange_hint=_MAIN_EXCHANGE,
            category=source_code,
            raw={
                "datetime": _text(cells[0]),
                "source": source_code,
                "company": company,
                "headline": headline,
                "href": href,
            },
        )


def _text(element: Any) -> str:
    return " ".join((element.text_content() or "").split())


def _split_company(company: str) -> tuple[str | None, str | None]:
    match = _COMPANY_RE.match(company)
    if match is None:
        return (company or None, None)
    name = match.group("name").strip()
    return (name or None, match.group("ticker").upper())


def _parse_datetime(value: str) -> dt.datetime | None:
    """Parse the page's Europe/London wall-clock time and convert it to UTC."""
    try:
        local = dt.datetime.strptime(value.strip(), _DATETIME_FORMAT).replace(tzinfo=_LONDON)
    except ValueError:
        return None
    return local.astimezone(dt.UTC)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_investegate_feed.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/stockbrain/ingestion/investegate.py backend/tests/unit/test_investegate_feed.py
git commit -m "feat(ingestion): Investegate front-page adapter"
```

---

## Task 3: EQS adapter

**Files:**
- Create: `backend/stockbrain/ingestion/eqs.py`
- Test: `backend/tests/unit/test_eqs_feed.py`

**Interfaces:**
- Consumes: `FeedHttpClient`, `FeedItem` from Task 1.
- Produces: `EQSFeed(settings: Settings, *, client: httpx.AsyncClient | None = None)` — `name = "eqs"`, `provider = SourceProvider.EQS`, `native_language = "de"`, `max_pages = 1`, `allow_empty = False`, `parse_page`.

Fixture facts pinned by the tests: 30 items; the page prints Europe/Berlin local
time (`data-current-time="2026-09-21 10:33:33"` alongside a "12:33" first item --
CEST, UTC+2 -- confirming the displayed clock is local, not UTC) so every
timestamp is converted; first release `a0a61b7f-2644-4343-bc8b-c5d1eaa908cc`
prints `2026-09-21 12:33` local and is `2026-09-21 10:33 UTC` with ISIN
`AU0000066086`; last release `f1a7a533-7e59-4d93-b645-d9fd6c2b3824` prints
`09:45` local and is `07:45 UTC`, `de`, ISIN `None` (the `noisin…` placeholder is
dropped); item 16's category comes from the URL slug `directors-dealings`
because the fixture's `data-news-category` attribute is malformed (`Directors'`
terminates the attribute); exactly 10 of 30 are boilerplate.

---

- [ ] **Step 1: Write the failing parser test**

Create `backend/tests/unit/test_eqs_feed.py`:

```python
"""The EQS homepage parser against the committed fixture."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import is_boilerplate
from stockbrain.ingestion.eqs import EQSFeed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
HOME = FIXTURES / "eqs_home.html"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _items() -> list[Any]:
    return EQSFeed(_settings()).parse_page(HOME.read_text(encoding="utf-8"))


def test_homepage_yields_thirty_releases() -> None:
    assert len(_items()) == 30


def test_first_row_fields() -> None:
    first = _items()[0]
    assert first.provider is SourceProvider.EQS
    assert first.release_id == "a0a61b7f-2644-4343-bc8b-c5d1eaa908cc"
    assert first.language == "en"
    assert first.url.endswith(
        "/first-commercial-scale-production-of-vulcans-proprietary-vulsorb-"
        "lithium-extraction-material/a0a61b7f-2644-4343-bc8b-c5d1eaa908cc_en"
    )
    assert first.headline.startswith("First commercial scale production of Vulcan")
    # The page prints "12:33" in Europe/Berlin local time (CEST, UTC+2).
    assert first.published_at == dt.datetime(2026, 9, 21, 10, 33, tzinfo=dt.UTC)
    assert first.company_name == "Vulcan Energy Resources Limited"
    assert first.isin == "AU0000066086"
    assert first.category == "corporate"
    assert first.exchange_hint is None


def test_last_row_fields() -> None:
    last = _items()[-1]
    assert last.release_id == "f1a7a533-7e59-4d93-b645-d9fd6c2b3824"
    assert last.language == "de"
    assert last.headline.startswith("Comarch im IDC MarketScape 2026")
    assert last.published_at == dt.datetime(2026, 9, 21, 7, 45, tzinfo=dt.UTC)
    assert last.company_name == "Comarch"
    # ``noisin084340`` is EQS's placeholder, not an ISIN.
    assert last.isin is None


def test_the_directors_category_comes_from_the_url_slug() -> None:
    """The fixture's attribute is malformed, so the URL path is authoritative."""
    assert _items()[16].category == "directors-dealings"


def test_boilerplate_count_matches_the_fixture() -> None:
    items = _items()
    assert sum(1 for item in items if is_boilerplate(item)) == 10


def test_the_homepage_has_no_pagination() -> None:
    feed = EQSFeed(_settings())
    assert feed.max_pages == 1
    assert feed.allow_empty is False


def test_a_restyled_page_raises_provider_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        EQSFeed(_settings()).parse_page("<html><body><p>no feed here</p></body></html>")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_eqs_feed.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.ingestion.eqs'`.

- [ ] **Step 3: Implement the adapter**

Create `backend/stockbrain/ingestion/eqs.py`:

```python
"""The EQS News homepage realtime list.

Each item is an anchor carrying ``data-news-*`` attributes and an
``…/news/<category-slug>/<headline-slug>/<uuid>_<lang>`` href.  The category is
read from the URL path rather than ``data-news-category``: the fixture's
directors-dealings attribute contains an unescaped apostrophe and parses as
``Directors``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from lxml import etree
from lxml import html as lxml_html

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["EQSFeed"]

_BASE_URL = "https://www.eqs-news.com"
_PATH = "/"
_NATIVE_LANGUAGE = "de"

#: A real ISIN, not EQS's ``noisinNNNNNN`` placeholder.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CATEGORY_RE = re.compile(r"/news/([^/]+)/")
_LANGUAGE_RE = re.compile(r"^[a-z]{2}$")
_DATE_FORMAT = "%d %B %Y %H:%M"
_ITEM_XPATH = '//a[@data-wio="news-feed-list-item"]'
#: The page prints wall-clock Europe/Berlin time (CET/CEST), not UTC --
#: confirmed by its own ``data-current-time`` attribute sitting two hours
#: behind the displayed time of the item captured at the same moment.
_BERLIN = ZoneInfo("Europe/Berlin")


class EQSFeed:
    """Parse the 30-item realtime list; it has no pagination without JS."""

    name = "eqs"
    provider = SourceProvider.EQS
    native_language = _NATIVE_LANGUAGE

    def __init__(
        self, settings: Settings, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.max_pages = 1
        self.allow_empty = False
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=_NATIVE_LANGUAGE,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        content = await self._http.get(_PATH, language=_NATIVE_LANGUAGE)
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        if not content or not content.strip():
            raise ProviderResponseError("eqs: empty response")
        try:
            root = lxml_html.fromstring(content)
        except (etree.ParserError, etree.XMLSyntaxError) as exc:
            raise ProviderResponseError("eqs: response was not HTML") from exc

        anchors = root.xpath(_ITEM_XPATH)
        if not anchors:
            raise ProviderResponseError("eqs: no news items found (page shape changed)")
        default_date = _first_date(root)
        items = [
            item
            for anchor in anchors
            if (item := self._parse_item(anchor, default_date)) is not None
        ]
        if not items:
            raise ProviderResponseError("eqs: news items carried no usable releases")
        return items

    def _parse_item(self, anchor: Any, default_date: str | None) -> FeedItem | None:
        href = (anchor.get("href") or "").strip()
        release_id = (anchor.get("data-news-item") or "").strip()
        if not href or not release_id:
            return None
        language = (
            _language_from_url(href) or _language_from_attribute(anchor) or _NATIVE_LANGUAGE
        )
        category = _category_from_url(href)
        company = _first_text(anchor, './/h4[contains(@class,"news__company")]')
        heading = _first_text(anchor, './/p[contains(@class,"news__heading")]')
        headline = heading or company
        if not headline:
            return None
        date_text = _preceding_date(anchor) or default_date
        time_text = _first_text(anchor, './/span[contains(@class,"news__time")]')
        published_at = _parse_datetime(date_text, time_text)
        if published_at is None:
            return None
        raw_isin = (anchor.get("data-news-isin") or "").strip()
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=language,
            url=href,
            headline=headline,
            published_at=published_at,
            company_name=company or None,
            isin=raw_isin if _ISIN_RE.match(raw_isin) else None,
            exchange_hint=None,
            category=category,
            raw={
                "data-news-item": release_id,
                "data-news-uuid": anchor.get("data-news-uuid"),
                "data-news-languages": anchor.get("data-news-languages"),
                "data-news-isin": raw_isin or None,
                "data-news-category": anchor.get("data-news-category"),
                "href": href,
                "company": company,
                "heading": heading,
                "date": date_text,
                "time": time_text,
            },
        )


def _first_text(root: Any, xpath: str) -> str:
    matches = root.xpath(xpath)
    if not matches:
        return ""
    return " ".join((matches[0].text_content() or "").split())


def _first_date(root: Any) -> str | None:
    for element in root.xpath('//p[contains(@class,"news__date")]'):
        text = " ".join((element.text_content() or "").split())
        if text:
            return text
    return None


def _preceding_date(anchor: Any) -> str | None:
    matches = anchor.xpath('preceding-sibling::p[contains(@class,"news__date")][1]')
    if not matches:
        return None
    return " ".join((matches[0].text_content() or "").split()) or None


def _category_from_url(href: str) -> str | None:
    match = _CATEGORY_RE.search(urlsplit(href).path)
    return match.group(1).lower() if match else None


def _language_from_url(href: str) -> str | None:
    segment = urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]
    suffix = segment.rsplit("_", 1)[-1].lower()
    return suffix if _LANGUAGE_RE.match(suffix) else None


def _language_from_attribute(anchor: Any) -> str | None:
    raw = anchor.get("data-news-languages")
    if not raw:
        return None
    try:
        languages = json.loads(raw)
    except ValueError:
        return None
    if isinstance(languages, dict):
        values = list(languages.values())
    elif isinstance(languages, list):
        values = languages
    else:
        return None
    return values[0] if values else None


def _parse_datetime(date_text: str | None, time_text: str | None) -> dt.datetime | None:
    """Parse the page's Europe/Berlin wall-clock time and convert it to UTC."""
    if not date_text or not time_text:
        return None
    try:
        local = dt.datetime.strptime(
            f"{date_text} {time_text}", _DATE_FORMAT
        ).replace(tzinfo=_BERLIN)
    except ValueError:
        return None
    return local.astimezone(dt.UTC)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_eqs_feed.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/stockbrain/ingestion/eqs.py backend/tests/unit/test_eqs_feed.py
git commit -m "feat(ingestion): EQS homepage adapter"
```

---

## Task 4: CNMV adapter (IP + OIR)

**Files:**
- Create: `backend/stockbrain/ingestion/cnmv.py`
- Test: `backend/tests/unit/test_cnmv_feed.py`

**Interfaces:**
- Consumes: `FeedHttpClient`, `FeedItem` from Task 1.
- Produces: `CNMVFeed(settings: Settings, *, kind: str, client: httpx.AsyncClient | None = None)` — `kind` is `"ip"` or `"oir"`; `name = f"cnmv_{kind}"`, `provider = SourceProvider.CNMV`, `native_language = "es"`, `max_pages = 1`, `allow_empty = (kind == "ip")`, `parse_page`. Raises `ValueError` on an unknown kind.

Fixture facts pinned by the tests: OIR has 17 items; first `nreg=42823` at `2026-09-21 10:33:40 UTC`, last `nreg=42807` at `2026-09-18 15:39:10 UTC`; the headline is the category text plus the issuer detail that follows the bold timestamp (only the timestamp itself is dropped); exactly 6 of 17 are boilerplate (the three ERCROS suspensions survive); the IP fixture has a `Channel` with zero items and is valid.

---

- [ ] **Step 1: Write the failing parser test**

Create `backend/tests/unit/test_cnmv_feed.py`:

```python
"""The CNMV non-standard RSS parser against the committed fixtures."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.cnmv import CNMVFeed
from stockbrain.ingestion.disclosure_feeds import is_boilerplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
OIR = FIXTURES / "cnmv_oir.xml"
IP_EMPTY = FIXTURES / "cnmv_ip_empty.xml"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _oir() -> list[Any]:
    return CNMVFeed(_settings(), kind="oir").parse_page(OIR.read_text(encoding="utf-8"))


def test_oir_yields_seventeen_releases() -> None:
    assert len(_oir()) == 17


def test_first_oir_row_fields() -> None:
    first = _oir()[0]
    assert first.provider is SourceProvider.CNMV
    assert first.release_id == "42823"
    assert first.language == "es"
    assert first.url.endswith("Resultado-OIR.aspx?nreg=42823")
    assert first.headline == (
        "Sobre suspensiones, levantamientos y exclusiones de negociación "
        "SOCIEDAD RECTORA DE LA BOLSA DE VALORES DE BARCELONA anuncia la "
        "exclusión de negociación de las acciones de ERCROS, S.A. con efectos "
        "del 22/09/2026, inclusive."
    )
    assert first.published_at == dt.datetime(2026, 9, 21, 10, 33, 40, tzinfo=dt.UTC)
    assert first.company_name == "ERCROS, S.A. (ERCROS)"
    assert first.category == "Otra información relevante"
    assert first.exchange_hint == "Bolsa de Madrid"


def test_last_oir_row_fields() -> None:
    last = _oir()[-1]
    assert last.release_id == "42807"
    assert last.headline == (
        "Sobre instrumentos financieros La Sociedad comunica que va a proceder "
        "a la amortización anticipada total de la emisión de cédulas "
        "territoriales denominada “Cédulas Territoriales – Marzo 2022” "
        "con código ISIN ES0413211A67."
    )
    assert last.company_name == "BANCO BILBAO VIZCAYA ARGENTARIA, S.A."
    assert last.published_at == dt.datetime(2026, 9, 18, 15, 39, 10, tzinfo=dt.UTC)


def test_boilerplate_count_matches_the_fixture() -> None:
    items = _oir()
    assert sum(1 for item in items if is_boilerplate(item)) == 6


def test_inside_information_is_never_filtered_by_the_oir_rules() -> None:
    items = _oir()
    # The three ERCROS suspensions are real events, not warrant-issuer noise.
    assert sum(1 for item in items if "Sobre suspensiones" in item.headline) == 3
    assert all(not is_boilerplate(item) for item in items if item.release_id in {"42823", "42822", "42821"})


def test_an_empty_inside_information_channel_is_valid() -> None:
    feed = CNMVFeed(_settings(), kind="ip")
    assert feed.allow_empty is True
    assert feed.parse_page(IP_EMPTY.read_text(encoding="utf-8")) == []


def test_oir_is_not_allowed_to_be_empty() -> None:
    assert CNMVFeed(_settings(), kind="oir").allow_empty is False


def test_a_missing_channel_raises_provider_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        CNMVFeed(_settings(), kind="oir").parse_page("<rss><Other/></rss>")


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        CNMVFeed(_settings(), kind="other")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_cnmv_feed.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.ingestion.cnmv'`.

- [ ] **Step 3: Implement the adapter**

Create `backend/stockbrain/ingestion/cnmv.py`:

```python
"""The two CNMV RSS feeds: inside information and other relevant information.

The feed is a non-standard RSS document: a capitalised ``Channel`` element, a
capitalised ``Title`` per item, and a description of ``<b>time date</b>
category<BR/><BR/>detail``. The headline is the category plus the issuer detail
that follows it -- the classifier's only readable text for this provider --
with just the bold timestamp dropped. An empty channel is a valid answer for
inside information; a missing channel is a shape change and raises.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from lxml import etree

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["CNMVFeed"]

_BASE_URL = "https://www.cnmv.es"
_NATIVE_LANGUAGE = "es"
_EXCHANGE_HINT = "Bolsa de Madrid"

#: The description is ``<b>time date</b> category<BR/><BR/>detail``.  Only the
#: bold timestamp is discarded; the category and the detail after it are the
#: classifier's only readable text for this provider.
_BOLD_STAMP_RE = re.compile(r"<b>.*?</b>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

#: kind -> (path, feed label, allow_empty)
_KINDS: dict[str, tuple[str, str, bool]] = {
    "ip": (
        "/Portal/Informacion-privilegiada/RSS.asmx/GetNoticiasCNMV",
        "Información privilegiada",
        True,
    ),
    "oir": (
        "/Portal/Otra-Informacion-Relevante/RSS.asmx/GetNoticiasCNMV",
        "Otra información relevante",
        False,
    ),
}


class CNMVFeed:
    """Parse one CNMV feed.  The feed is Spanish only; there are no variants."""

    provider = SourceProvider.CNMV
    native_language = _NATIVE_LANGUAGE

    def __init__(
        self,
        settings: Settings,
        *,
        kind: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if kind not in _KINDS:
            raise ValueError(f"CNMVFeed kind must be one of {sorted(_KINDS)}, got {kind!r}")
        path, category, allow_empty = _KINDS[kind]
        self.kind = kind
        self.name = f"cnmv_{kind}"
        self.category = category
        self.allow_empty = allow_empty
        self.max_pages = 1
        self._path = path
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=_NATIVE_LANGUAGE,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        content = await self._http.get(self._path, language=_NATIVE_LANGUAGE)
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        """Parse one feed.

        A missing ``Channel`` is a shape change and raises.  A present channel
        with no items is a *valid* empty feed -- the caller's ``allow_empty``
        decides whether that is expected for this feed.
        """
        if not content or not content.strip():
            raise ProviderResponseError("cnmv: empty response")
        try:
            root = etree.fromstring(content.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise ProviderResponseError("cnmv: response was not XML") from exc

        channel = root.find("Channel")
        if channel is None:
            raise ProviderResponseError("cnmv: no Channel element (page shape changed)")
        raw_items = channel.findall("item")
        if not raw_items:
            return []
        items = [item for element in raw_items if (item := self._parse_item(element)) is not None]
        if not items:
            raise ProviderResponseError("cnmv: items carried no usable releases")
        return items

    def _parse_item(self, element: Any) -> FeedItem | None:
        link = _clean(element.findtext("link"))
        title = _clean(element.findtext("Title"))
        description = _clean(element.findtext("description"))
        published_at = _parse_datetime(element.findtext("pubDate"))
        release_id = _release_id(link)
        headline = _headline(description)
        if not link or not headline or not release_id or published_at is None:
            return None
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=_NATIVE_LANGUAGE,
            url=link,
            headline=headline,
            published_at=published_at,
            company_name=title or None,
            exchange_hint=_EXCHANGE_HINT,
            category=self.category,
            raw={
                "Title": title,
                "link": link,
                "pubDate": element.findtext("pubDate"),
                "description": description,
            },
        )


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _release_id(link: str) -> str | None:
    if not link:
        return None
    query = parse_qs(urlparse(link).query)
    values = query.get("nreg")
    return values[0] if values else None


def _headline(description: str) -> str:
    """The category label plus the issuer detail that follows it.

    Only the bold ``time date`` stamp is dropped; the rest of the description
    -- category and detail alike -- survives, because for CNMV this is the
    only readable text the classifier ever sees (bodies are ``None``).
    """
    if not description:
        return ""
    text = _BOLD_STAMP_RE.sub(" ", description, count=1)
    text = _BR_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return _clean(html.unescape(text))


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_cnmv_feed.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/stockbrain/ingestion/cnmv.py backend/tests/unit/test_cnmv_feed.py
git commit -m "feat(ingestion): CNMV inside-information and OIR adapters"
```

---

## Task 5: GlobeNewswire (per country) and ActusNews adapters

**Files:**
- Create: `backend/stockbrain/ingestion/globenewswire.py`
- Create: `backend/stockbrain/ingestion/actusnews.py`
- Test: `backend/tests/unit/test_globenewswire_feed.py`
- Test: `backend/tests/unit/test_actusnews_feed.py`
- Add: `backend/tests/fixtures/disclosure_feeds/globenewswire_canada_valid.xml` (already captured in the working tree; commit it here)
- Add: `backend/tests/fixtures/disclosure_feeds/README.md` (already captured; commit it here)

**Interfaces:**
- Consumes: `FeedHttpClient`, `FeedItem`, `group_releases` from Task 1.
- Produces:
  - `GlobeNewswireFeed(settings: Settings, *, country: str, client: httpx.AsyncClient | None = None)` — `name = f"globenewswire_{country.lower()}"`, `provider = SourceProvider.GLOBENEWSWIRE`, country-mapped `native_language`, `max_pages = 1`, `allow_empty = False`, `parse_page`. Feed URL `https://www.globenewswire.com/RssFeed/country/<Country>`.
  - `ActusNewsFeed(settings: Settings, *, client: httpx.AsyncClient | None = None)` — `name = "actusnews"`, `provider = SourceProvider.ACTUSNEWS`, `native_language = "en"`, `max_pages = 1`, `allow_empty = False`, `parse_page`. Feed URL `https://www.actusnews.com/en/rss`.

Fixture facts pinned by the tests: GlobeNewswire France has 20 items, first release `3365280` at `2026-09-21 09:42 UTC`, last `3364880` at `2026-09-18 16:07 UTC`; 5 of 20 are boilerplate; grouping the France batch yields 12 items and release `3365169` (SPIE) keeps the English URL with the French URL in `alternate_language_urls`. Netherlands has 20 items and 0 boilerplate. Canada has two captures: `globenewswire_canada_valid.xml` (20 items, first `3365350`, last `3364980`, `(TSX: CNR)` on `3365354` and `(TSX: MX)` on `3365336`, 0 boilerplate, 17 after grouping three translation pairs) and `globenewswire_canada.xml`, which is a 404 XHTML page and must raise `ProviderResponseError`. ActusNews has 20 items and 0 boilerplate.

---

- [ ] **Step 1: Write the failing GlobeNewswire test**

Create `backend/tests/unit/test_globenewswire_feed.py`:

```python
"""The GlobeNewswire per-country RSS parser against the committed fixtures."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import group_releases, is_boilerplate
from stockbrain.ingestion.globenewswire import GlobeNewswireFeed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
FRANCE = FIXTURES / "globenewswire_france.xml"
NETHERLANDS = FIXTURES / "globenewswire_netherlands.xml"
CANADA = FIXTURES / "globenewswire_canada_valid.xml"
CANADA_404 = FIXTURES / "globenewswire_canada.xml"

SPIE_FRENCH_URL = (
    "https://www.globenewswire.com/news-release/2026/09/21/3365169/0/fr/"
    "spie-annonce-le-lancement-d-une-%C3%A9mission-obligataire-au-format-"
    "sustainability-linked.html"
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _france_items() -> list[Any]:
    feed = GlobeNewswireFeed(_settings(), country="France")
    return feed.parse_page(FRANCE.read_text(encoding="utf-8"))


def _canada_items() -> list[Any]:
    feed = GlobeNewswireFeed(_settings(), country="Canada")
    return feed.parse_page(CANADA.read_text(encoding="utf-8"))


def test_france_yields_twenty_releases() -> None:
    assert len(_france_items()) == 20


def test_france_first_and_last_rows() -> None:
    first = _france_items()[0]
    assert first.provider is SourceProvider.GLOBENEWSWIRE
    assert first.release_id == "3365280"
    assert first.language == "fr"
    assert first.headline.startswith("FLEURY MICHON : Déclaration des opérations de rachat")
    assert first.published_at == dt.datetime(2026, 9, 21, 9, 42, tzinfo=dt.UTC)
    assert first.company_name == "FLEURY MICHON"

    last = _france_items()[-1]
    assert last.release_id == "3364880"
    assert last.language == "fr"
    assert last.headline.startswith("Succès du programme de rachat")
    assert last.published_at == dt.datetime(2026, 9, 18, 16, 7, tzinfo=dt.UTC)
    assert last.company_name == "Amundi"


def test_france_boilerplate_count_matches_the_fixture() -> None:
    items = _france_items()
    assert sum(1 for item in items if is_boilerplate(item)) == 5


def test_netherlands_yields_twenty_and_filters_none() -> None:
    feed = GlobeNewswireFeed(_settings(), country="Netherlands")
    items = feed.parse_page(NETHERLANDS.read_text(encoding="utf-8"))
    assert len(items) == 20
    assert sum(1 for item in items if is_boilerplate(item)) == 0


def test_grouping_collapses_a_translation_and_keeps_the_other_url() -> None:
    feed = GlobeNewswireFeed(_settings(), country="France")
    items = feed.parse_page(FRANCE.read_text(encoding="utf-8"))
    grouped = group_releases(items, feed.native_language)
    assert len(grouped) == 12
    spie = next(item for item in grouped if item.release_id == "3365169")
    assert spie.language == "en"
    assert spie.alternate_language_urls == {"fr": SPIE_FRENCH_URL}


def test_canada_yields_twenty_releases_and_extracts_tsx_tickers() -> None:
    items = _canada_items()
    assert len(items) == 20
    assert items[0].release_id == "3365350"
    assert items[0].language == "en"
    assert items[0].company_name == "Kruger Products Inc."
    assert items[0].published_at == dt.datetime(2026, 9, 21, 11, 0, tzinfo=dt.UTC)
    assert items[-1].release_id == "3364980"
    assert items[-1].company_name == "Liberty Gold Corp."
    # ``(TSX: CNR)`` on release 3365354 and ``(TSX: MX)`` on release 3365336.
    tickers = {item.release_id: item.ticker for item in items if item.ticker}
    assert tickers == {"3365354": "CNR", "3365336": "MX"}


def test_canada_has_no_boilerplate_and_groups_three_translations() -> None:
    feed = GlobeNewswireFeed(_settings(), country="Canada")
    items = feed.parse_page(CANADA.read_text(encoding="utf-8"))
    assert sum(1 for item in items if is_boilerplate(item)) == 0
    grouped = group_releases(items, feed.native_language)
    assert len(grouped) == 17


def test_the_country_carries_a_native_language() -> None:
    assert GlobeNewswireFeed(_settings(), country="France").native_language == "fr"
    assert GlobeNewswireFeed(_settings(), country="Canada").native_language == "en"


def test_the_committed_canada_404_capture_raises() -> None:
    """A 404 XHTML page is a shape change, not an empty feed."""
    feed = GlobeNewswireFeed(_settings(), country="Canada")
    with pytest.raises(ProviderResponseError):
        feed.parse_page(CANADA_404.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_globenewswire_feed.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.ingestion.globenewswire'`.

- [ ] **Step 3: Implement the GlobeNewswire adapter**

Create `backend/stockbrain/ingestion/globenewswire.py`:

```python
"""GlobeNewswire's per-country RSS feeds, one adapter instance per country.

``dc:identifier`` is the language-agnostic release number shared by the
English and French variants, so it is the ``release_id``.  ``dc:language``
carries the variant.  The namespace URI the feed declares is non-standard
(``http://dublincore.org/documents/dcmi-namespace/``), so fields are read by
local name rather than by a hard-coded namespace.
"""

from __future__ import annotations

import datetime as dt
import re
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from lxml import etree

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["GlobeNewswireFeed"]

_BASE_URL = "https://www.globenewswire.com"
_NATIVE_LANGUAGE = "en"

#: A country's default language, used only when a release has no English
#: variant in the batch.
_COUNTRY_LANGUAGES = {
    "france": "fr",
    "netherlands": "nl",
    "belgium": "nl",
    "portugal": "pt",
    "spain": "es",
    "canada": "en",
}

#: ``(TSX: X)`` / ``(TSXV: X)`` in a title or description is a hint, not
#: identity: the resolver still treats it as rung-3 evidence.
_TICKER_RE = re.compile(r"\((TSXV?):\s*([A-Za-z0-9.\-]+)\)", re.IGNORECASE)


class GlobeNewswireFeed:
    """Parse one country's RSS feed; variants share ``dc:identifier``."""

    provider = SourceProvider.GLOBENEWSWIRE

    def __init__(
        self,
        settings: Settings,
        *,
        country: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.country = country
        self.name = f"globenewswire_{country.lower()}"
        self.native_language = _COUNTRY_LANGUAGES.get(country.lower(), _NATIVE_LANGUAGE)
        self.max_pages = 1
        self.allow_empty = False
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=self.native_language,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        content = await self._http.get(
            f"/RssFeed/country/{self.country}", language=self.native_language
        )
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        if not content or not content.strip():
            raise ProviderResponseError("globenewswire: empty response")
        try:
            root = etree.fromstring(content.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise ProviderResponseError("globenewswire: response was not RSS XML") from exc
        if root.tag.split("}")[-1] != "rss":
            raise ProviderResponseError("globenewswire: root element was not rss")
        channel = root.find("channel")
        if channel is None:
            raise ProviderResponseError(
                "globenewswire: no channel element (page shape changed)"
            )
        raw_items = channel.findall("item")
        if not raw_items:
            raise ProviderResponseError("globenewswire: feed carried no items")
        items = [item for element in raw_items if (item := self._parse_item(element)) is not None]
        if not items:
            raise ProviderResponseError("globenewswire: items carried no usable releases")
        return items

    def _parse_item(self, element: Any) -> FeedItem | None:
        fields = {child.tag.split("}")[-1]: (child.text or "").strip() for child in element}
        release_id = fields.get("identifier", "")
        headline = fields.get("title", "")
        link = fields.get("link", "")
        if not release_id or not headline or not link:
            return None
        published_at = _parse_datetime(fields.get("pubDate"))
        if published_at is None:
            return None
        ticker_match = _TICKER_RE.search(f"{headline}\n{fields.get('description', '')}")
        subjects = [
            (child.text or "").strip()
            for child in element
            if child.tag.split("}")[-1] == "subject" and (child.text or "").strip()
        ]
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=fields.get("language") or self.native_language,
            url=link,
            headline=headline,
            published_at=published_at,
            company_name=fields.get("contributor") or None,
            ticker=ticker_match.group(2).upper() if ticker_match else None,
            exchange_hint=None,
            category=subjects[0] if subjects else self.country,
            raw={**fields, "subjects": subjects},
        )


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
```

- [ ] **Step 4: Run the GlobeNewswire test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_globenewswire_feed.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Write the failing ActusNews test**

Create `backend/tests/unit/test_actusnews_feed.py`:

```python
"""The Actusnews Wire English RSS parser against the committed fixture."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.actusnews import ActusNewsFeed
from stockbrain.ingestion.disclosure_feeds import is_boilerplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
EN_FEED = FIXTURES / "actusnews_en.xml"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _items() -> list[Any]:
    return ActusNewsFeed(_settings()).parse_page(EN_FEED.read_text(encoding="utf-8"))


def test_feed_yields_twenty_releases() -> None:
    assert len(_items()) == 20


def test_first_row_fields() -> None:
    first = _items()[0]
    assert first.provider is SourceProvider.ACTUSNEWS
    assert first.release_id == (
        "2026/09/18/artprice-news-a-world-book-becomes-a-mirror-for-artificial-intelligence"
    )
    assert first.language == "en"
    assert first.url.endswith(
        "/en/artmarket/pr/2026/09/18/"
        "artprice-news-a-world-book-becomes-a-mirror-for-artificial-intelligence"
    )
    assert first.headline == (
        "ARTPRICE NEWS: A WORLD-BOOK BECOMES A MIRROR FOR ARTIFICIAL INTELLIGENCE"
    )
    assert first.company_name == "ARTMARKET.COM"
    assert first.published_at == dt.datetime(2026, 9, 18, 16, 45, tzinfo=dt.UTC)
    assert first.category == "Actusnews"
    assert first.exchange_hint is None


def test_last_row_fields() -> None:
    last = _items()[-1]
    assert last.release_id == (
        "2026/09/14/wavestone-continues-its-expansion-in-the-united-states-"
        "with-the-acquisition-of-sand-cherry"
    )
    assert last.headline == (
        "Wavestone continues its expansion in the United States with the acquisition "
        "of Sand Cherry"
    )
    assert last.company_name == "WAVESTONE"
    assert last.published_at == dt.datetime(2026, 9, 14, 5, 30, tzinfo=dt.UTC)


def test_a_title_without_the_company_separator_is_kept_whole() -> None:
    items = _items()
    drone = next(item for item in items if item.company_name == "DRONE VOLT")
    assert drone.headline.startswith("DRONE VOLT announces its results")


def test_actusnews_has_no_boilerplate_rules() -> None:
    items = _items()
    assert sum(1 for item in items if is_boilerplate(item)) == 0


def test_an_empty_feed_raises_provider_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        ActusNewsFeed(_settings()).parse_page("<rss><channel></channel></rss>")
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_actusnews_feed.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.ingestion.actusnews'`.

- [ ] **Step 7: Implement the ActusNews adapter**

Create `backend/stockbrain/ingestion/actusnews.py`:

```python
"""The Actusnews Wire English RSS feed for French issuers.

Only ``/en/rss`` is subscribed; the French feed is not.  The title convention is
``COMPANY : headline`` (also ``COMPANY - headline``), and ``dc:creator`` names
the company, so the headline is the remainder once that prefix is removed.  A
title without the separator is kept whole.
"""

from __future__ import annotations

import datetime as dt
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from lxml import etree

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedHttpClient, FeedItem

__all__ = ["ActusNewsFeed"]

_BASE_URL = "https://www.actusnews.com"
_PATH = "/en/rss"
_NATIVE_LANGUAGE = "en"
_COMPANY = "Actusnews"
_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


class ActusNewsFeed:
    """Parse the English feed; the ``COMPANY : headline`` title is split."""

    name = "actusnews"
    provider = SourceProvider.ACTUSNEWS
    native_language = _NATIVE_LANGUAGE

    def __init__(
        self, settings: Settings, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.max_pages = 1
        self.allow_empty = False
        self._http = FeedHttpClient(
            settings,
            provider=self.name,
            base_url=_BASE_URL,
            language=_NATIVE_LANGUAGE,
            client=client,
        )

    async def fetch_page(self, page: int) -> list[FeedItem]:
        content = await self._http.get(_PATH, language=_NATIVE_LANGUAGE)
        return self.parse_page(content)

    async def aclose(self) -> None:
        await self._http.aclose()

    def parse_page(self, content: str) -> list[FeedItem]:
        if not content or not content.strip():
            raise ProviderResponseError("actusnews: empty response")
        try:
            root = etree.fromstring(content.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise ProviderResponseError("actusnews: response was not RSS XML") from exc
        if root.tag.split("}")[-1] != "rss":
            raise ProviderResponseError("actusnews: root element was not rss")
        channel = root.find("channel")
        if channel is None:
            raise ProviderResponseError(
                "actusnews: no channel element (page shape changed)"
            )
        raw_items = channel.findall("item")
        if not raw_items:
            raise ProviderResponseError("actusnews: feed carried no items")
        items = [item for element in raw_items if (item := self._parse_item(element)) is not None]
        if not items:
            raise ProviderResponseError("actusnews: items carried no usable releases")
        return items

    def _parse_item(self, element: Any) -> FeedItem | None:
        title = _clean(element.findtext("title"))
        link = _clean(element.findtext("link"))
        creator = _clean(element.findtext(_DC_CREATOR))
        published_at = _parse_datetime(element.findtext("pubDate"))
        release_id = link.split("/pr/", 1)[-1] if "/pr/" in link else ""
        headline = _strip_company_prefix(title, creator)
        if not release_id or not headline or not link or published_at is None:
            return None
        return FeedItem(
            provider=self.provider,
            release_id=release_id,
            language=_NATIVE_LANGUAGE,
            url=link,
            headline=headline,
            published_at=published_at,
            company_name=creator or None,
            exchange_hint=None,
            category=_COMPANY,
            raw={
                "title": title,
                "link": link,
                "guid": _clean(element.findtext("guid")),
                "creator": creator,
                "pubDate": element.findtext("pubDate"),
            },
        )


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _strip_company_prefix(title: str, creator: str) -> str:
    if creator:
        for separator in (" : ", " - "):
            prefix = f"{creator}{separator}"
            if title.startswith(prefix):
                return title[len(prefix) :].strip()
    return title


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
```

- [ ] **Step 8: Run the ActusNews test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_actusnews_feed.py -q`
Expected: PASS (6 passed).

- [ ] **Step 9: Commit**

```bash
git add backend/stockbrain/ingestion/globenewswire.py backend/stockbrain/ingestion/actusnews.py \
  backend/tests/unit/test_globenewswire_feed.py backend/tests/unit/test_actusnews_feed.py
git commit -m "feat(ingestion): GlobeNewswire country and Actusnews adapters"
```

---

## Task 6: Poll handler, service wiring and schedules

**Files:**
- Create: `backend/stockbrain/jobs/disclosure.py`
- Modify: `backend/stockbrain/services.py`
- Test: `backend/tests/integration/test_disclosure_handler.py`
- Test: `backend/tests/integration/test_disclosure_scheduler.py`

**Interfaces:**
- Consumes: Tasks 1–5 (`DisclosureFeed`, `group_releases`, `is_boilerplate`, `to_document`, the five adapters, `known_release_ids`, `ProviderName` and `JobType` additions, the settings).
- Produces:
  - `handle_disclosure_feed_poll(context: HandlerContext) -> None`
  - `ServiceContainer.disclosure_feeds: dict[str, DisclosureFeed]`
  - `ServiceContainer._build_disclosure_feeds()`, `_register_schedules` feed tasks, `_enqueue_disclosure_feed(name: str)`, `_disclosure_interval(provider)`, `_disclosure_enqueue_callback(name)`

This task registers the handler in `services.start()` rather than in `register_ingestion_handlers`, because Task 8 owns `jobs/handlers.py` for the language call site and the two run concurrently.

---

- [ ] **Step 1: Write the failing handler test**

Create `backend/tests/integration/test_disclosure_handler.py`:

```python
"""The disclosure-feed poll handler: known-id stopping, filtering, failure posture."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.sources import Source
from stockbrain.db.session import Database
from stockbrain.enums import JobType, ProviderStatus, SourceProvider
from stockbrain.errors import ProviderAuthError, ProviderResponseError
from stockbrain.ingestion.disclosure_feeds import FeedItem
from stockbrain.jobs.disclosure import handle_disclosure_feed_poll
from stockbrain.jobs.registry import HandlerContext
from stockbrain.observability.health import ProviderHealthRegistry, ProviderName
from stockbrain.services import ServiceContainer

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "disclosure_feeds_enabled": False,
        "discovery_enabled": True,
        "alpaca_news_enabled": False,
        "sec_enabled": False,
        "t212_metadata_enabled": False,
        "research_enabled": False,
        "classifier_enabled": False,
        "proposals_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _container(database: Database) -> ServiceContainer:
    return ServiceContainer(
        settings=_settings(database), database=database, health=ProviderHealthRegistry()
    )


def _item(
    release_id: str, *, headline: str = "Interim Results", url: str | None = None
) -> FeedItem:
    return FeedItem(
        provider=SourceProvider.INVESTEGATE,
        release_id=release_id,
        language="en",
        url=(
            url
            if url is not None
            else f"https://www.investegate.co.uk/announcement/rns/x/{release_id}"
        ),
        headline=headline,
        published_at=dt.datetime(2026, 9, 21, 11, 0, tzinfo=dt.UTC),
        company_name="Barclays",
        ticker="BARC",
        exchange_hint="London Stock Exchange",
        category="RNS",
    )


class FakeFeed:
    name = "investegate"
    provider = SourceProvider.INVESTEGATE
    native_language = "en"
    max_pages = 3
    allow_empty = False

    def __init__(
        self, pages: dict[int, list[FeedItem]], *, error: Exception | None = None
    ) -> None:
        self._pages = pages
        self._error = error
        self.pages_fetched: list[int] = []
        self.closed = False

    async def fetch_page(self, page: int) -> list[FeedItem]:
        self.pages_fetched.append(page)
        if self._error is not None:
            raise self._error
        return list(self._pages.get(page, []))

    async def aclose(self) -> None:
        self.closed = True


def _context(container: ServiceContainer, feed_name: str = "investegate") -> HandlerContext:
    return HandlerContext(
        job_id=uuid.uuid4(),
        job_type=JobType.DISCLOSURE_FEED_POLL.value,
        payload={"feed": feed_name},
        attempt=1,
        max_attempts=3,
        database=container.database,
        services=container,
    )


async def _count_sources(database: Database) -> int:
    async with database.session() as session:
        return int(
            (await session.execute(sa.select(sa.func.count()).select_from(Source))).scalar_one()
        )


async def test_first_poll_creates_a_source_per_release(clean_tables: Database) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: [_item("1"), _item("2")]})
    container.disclosure_feeds = {feed.name: feed}

    await handle_disclosure_feed_poll(_context(container))

    assert await _count_sources(clean_tables) == 2
    assert feed.closed is True
    assert container.health.get(ProviderName.INVESTEGATE).status is ProviderStatus.HEALTHY


async def test_a_second_poll_stops_at_page_one_and_creates_nothing(
    clean_tables: Database,
) -> None:
    container = _container(clean_tables)
    first = FakeFeed({1: [_item("1"), _item("2")], 2: [_item("3")]})
    container.disclosure_feeds = {first.name: first}
    await handle_disclosure_feed_poll(_context(container))
    assert await _count_sources(clean_tables) == 2

    second = FakeFeed({1: [_item("1"), _item("2")], 2: [_item("3")]})
    container.disclosure_feeds = {second.name: second}
    await handle_disclosure_feed_poll(_context(container))

    assert second.pages_fetched == [1]
    assert await _count_sources(clean_tables) == 2


async def test_boilerplate_is_filtered_before_ingest(clean_tables: Database) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: [_item("1", headline="Transaction in Own Shares"), _item("2")]})
    container.disclosure_feeds = {feed.name: feed}

    await handle_disclosure_feed_poll(_context(container))

    assert await _count_sources(clean_tables) == 1


async def test_a_malformed_item_is_skipped_and_counted(clean_tables: Database) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: [_item("bad", url=""), _item("good")]})
    container.disclosure_feeds = {feed.name: feed}

    await handle_disclosure_feed_poll(_context(container))

    assert await _count_sources(clean_tables) == 1
    assert container.health.get(ProviderName.INVESTEGATE).metrics["malformed"] == 1


async def test_an_auth_failure_marks_the_feed_down_and_stops(
    clean_tables: Database,
) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({}, error=ProviderAuthError("investegate: HTTP 403"))
    container.disclosure_feeds = {feed.name: feed}

    with pytest.raises(ProviderAuthError):
        await handle_disclosure_feed_poll(_context(container))

    assert feed.pages_fetched == [1]
    assert feed.closed is True
    assert container.health.get(ProviderName.INVESTEGATE).status is ProviderStatus.DOWN
    assert await _count_sources(clean_tables) == 0


async def test_an_empty_page_marks_the_feed_degraded(clean_tables: Database) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: []})
    container.disclosure_feeds = {feed.name: feed}

    with pytest.raises(ProviderResponseError):
        await handle_disclosure_feed_poll(_context(container))

    assert container.health.get(ProviderName.INVESTEGATE).status is ProviderStatus.DEGRADED


async def test_an_empty_inside_information_channel_is_a_healthy_no_op(
    clean_tables: Database,
) -> None:
    container = _container(clean_tables)
    feed = FakeFeed({1: []})
    feed.allow_empty = True
    container.disclosure_feeds = {feed.name: feed}

    await handle_disclosure_feed_poll(_context(container))

    assert await _count_sources(clean_tables) == 0
    assert container.health.get(ProviderName.INVESTEGATE).status is ProviderStatus.HEALTHY


async def test_an_unconfigured_feed_raises(clean_tables: Database) -> None:
    container = _container(clean_tables)
    with pytest.raises(RuntimeError):
        await handle_disclosure_feed_poll(_context(container, "nope"))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_handler.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stockbrain.jobs.disclosure'`.

- [ ] **Step 3: Implement the handler**

Create `backend/stockbrain/jobs/disclosure.py`:

```python
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
    finally:
        await feed.aclose()

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
```

- [ ] **Step 4: Run the handler test to verify it passes**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_handler.py -q`
Expected: PASS (8 passed) with the test database; skipped otherwise.

- [ ] **Step 5: Wire the container, schedules and registration**

In `backend/stockbrain/services.py`:

**(a) Imports.** Add this stdlib import between `import uuid` and
`from dataclasses import dataclass, field`:

```python
from collections.abc import Awaitable, Callable
```

Add `SourceProvider` to the `stockbrain.enums` import block. Add the feed imports to the ingestion import block so it reads:

```python
from stockbrain.ingestion.actusnews import ActusNewsFeed
from stockbrain.ingestion.alpaca_news import AlpacaNewsClient
from stockbrain.ingestion.brave import BraveSearchClient
from stockbrain.ingestion.cnmv import CNMVFeed
from stockbrain.ingestion.disclosure_feeds import DisclosureFeed
from stockbrain.ingestion.eqs import EQSFeed
from stockbrain.ingestion.exa import ExaSearchClient
from stockbrain.ingestion.firecrawl import FirecrawlClient
from stockbrain.ingestion.globenewswire import GlobeNewswireFeed
from stockbrain.ingestion.investegate import InvestegateFeed
from stockbrain.ingestion.provider_budget import ProviderCallBudget
from stockbrain.ingestion.sec_edgar import SecEdgarClient
from stockbrain.ingestion.service import IngestionOutcome, IngestionService
from stockbrain.ingestion.topics import seed_default_topics, seed_semantic_topic
```

Add to the jobs import block, before `from stockbrain.jobs.handlers import (...)`:

```python
from stockbrain.jobs.disclosure import handle_disclosure_feed_poll
```

**(b) Field.** After `sec: SecEdgarClient | None = field(default=None, init=False)` add:

```python
    disclosure_feeds: dict[str, DisclosureFeed] = field(default_factory=dict, init=False)
```

**(c) Build the feeds.** Inside `_build_providers`, immediately after the `self._build_web_discovery()` line, add:

```python
        self._build_disclosure_feeds()
```

**(d) The builder and enqueue helpers.** Insert this whole block immediately before
`def _register_schedules(self, scheduler: Scheduler) -> None:`:

```python
    def _build_disclosure_feeds(self) -> None:
        """Construct one adapter per enabled feed; nothing if the master is off."""
        settings = self.settings
        if not settings.disclosure_feeds_enabled:
            return
        feeds: dict[str, DisclosureFeed] = {}
        if settings.investegate_enabled:
            feed = InvestegateFeed(settings)
            feeds[feed.name] = feed
        if settings.eqs_enabled:
            feed = EQSFeed(settings)
            feeds[feed.name] = feed
        if settings.cnmv_enabled:
            for kind in ("ip", "oir"):
                feed = CNMVFeed(settings, kind=kind)
                feeds[feed.name] = feed
        if settings.globenewswire_enabled:
            for country in settings.globenewswire_countries:
                feed = GlobeNewswireFeed(settings, country=country)
                feeds[feed.name] = feed
        if settings.actusnews_enabled:
            feed = ActusNewsFeed(settings)
            feeds[feed.name] = feed
        self.disclosure_feeds = feeds

    def _disclosure_interval(self, provider: SourceProvider) -> float:
        return {
            SourceProvider.INVESTEGATE: self.settings.investegate_interval_seconds,
            SourceProvider.EQS: self.settings.eqs_interval_seconds,
            SourceProvider.CNMV: self.settings.cnmv_interval_seconds,
            SourceProvider.GLOBENEWSWIRE: self.settings.globenewswire_interval_seconds,
            SourceProvider.ACTUSNEWS: self.settings.actusnews_interval_seconds,
        }[provider]

    def _disclosure_enqueue_callback(self, name: str) -> Callable[[], Awaitable[None]]:
        async def _run() -> None:
            await self._enqueue_disclosure_feed(name)

        return _run

    async def _enqueue_disclosure_feed(self, name: str) -> None:
        """Enqueue one feed poll.  The master flag and the pause gate it first."""
        if not self.settings.disclosure_feeds_enabled:
            return
        if not self.settings.discovery_enabled or await self._discovery_paused():
            return
        async with self.database.transaction() as session:
            await self.queue.enqueue(
                session,
                JobType.DISCLOSURE_FEED_POLL,
                payload={"feed": name},
                # One outstanding poll per feed: a slow feed must not accumulate
                # a backlog of identical polls.
                dedupe_key=f"feed:{name}",
                priority=50,
            )
```

**(e) Schedules.** In `_register_schedules`, immediately after the `if self.sec is not None:` block (the one adding `sec_watchlist_refresh`, which ends with `initial_delay_seconds=30.0,`), add:

```python
        for name, feed in self.disclosure_feeds.items():
            scheduler.add(
                ScheduledTask(
                    name=f"disclosure_feed:{name}",
                    interval_seconds=self._disclosure_interval(feed.provider),
                    run=self._disclosure_enqueue_callback(name),
                    initial_delay_seconds=45.0,
                )
            )
```

**(f) Registration.** In `start()`, immediately after the `register_ingestion_handlers(...)` call, add:

```python
        self.registry.register(
            JobType.DISCLOSURE_FEED_POLL.value, handle_disclosure_feed_poll
        )
```

**(g) Shutdown.** In `stop()`, immediately after the existing `for client in (...)` loop, add:

```python
        for feed in self.disclosure_feeds.values():
            with contextlib.suppress(Exception):
                await feed.aclose()
```

- [ ] **Step 6: Write the failing scheduler test**

Create `backend/tests/integration/test_disclosure_scheduler.py`:

```python
"""Scheduling for the disclosure feeds: one task per enabled feed, master gate."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.models.system import Job
from stockbrain.db.session import Database
from stockbrain.enums import JobType
from stockbrain.jobs.scheduler import Scheduler
from stockbrain.observability.health import ProviderHealthRegistry
from stockbrain.services import ServiceContainer

pytestmark = pytest.mark.integration


def _settings(database: Database, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "web_auth_enabled": False,
        "database_url": database.engine.url.render_as_string(hide_password=False),
        "alpaca_news_enabled": False,
        "sec_enabled": False,
        "t212_metadata_enabled": False,
        "research_enabled": False,
        "classifier_enabled": False,
        "proposals_enabled": False,
        # These two services are built unconditionally and would otherwise add
        # their own scheduled tasks, making the exact task-set assertion flaky.
        "content_extraction_enabled": False,
        "memory_grade_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _container(database: Database, **overrides: Any) -> ServiceContainer:
    return ServiceContainer(
        settings=_settings(database, **overrides),
        database=database,
        health=ProviderHealthRegistry(),
    )


async def _jobs(database: Database) -> list[Job]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    sa.select(Job).where(Job.job_type == JobType.DISCLOSURE_FEED_POLL.value)
                )
            ).scalars()
        )


async def test_the_master_flag_off_builds_no_feeds(clean_tables: Database) -> None:
    container = _container(
        clean_tables, disclosure_feeds_enabled=False, investegate_enabled=True
    )
    assert container.disclosure_feeds == {}


async def test_an_enabled_feed_is_constructed_and_closed(clean_tables: Database) -> None:
    container = _container(
        clean_tables, disclosure_feeds_enabled=True, investegate_enabled=True
    )
    try:
        assert set(container.disclosure_feeds) == {"investegate"}
    finally:
        await container.stop()


async def test_one_task_per_enabled_feed(clean_tables: Database) -> None:
    container = _container(
        clean_tables,
        disclosure_feeds_enabled=True,
        investegate_enabled=True,
        eqs_enabled=True,
    )
    try:
        scheduler = Scheduler(clean_tables)
        container._register_schedules(scheduler)
        assert set(scheduler._tasks) == {
            "disclosure_feed:investegate",
            "disclosure_feed:eqs",
        }
        assert scheduler._tasks["disclosure_feed:investegate"].interval_seconds == 300.0
        assert scheduler._tasks["disclosure_feed:eqs"].interval_seconds == 300.0
    finally:
        await container.stop()


async def test_a_globe_newswire_country_gets_its_own_task(clean_tables: Database) -> None:
    container = _container(
        clean_tables,
        disclosure_feeds_enabled=True,
        globenewswire_enabled=True,
        globenewswire_countries=["France", "Canada"],
    )
    try:
        scheduler = Scheduler(clean_tables)
        container._register_schedules(scheduler)
        assert {
            "disclosure_feed:globenewswire_france",
            "disclosure_feed:globenewswire_canada",
        } <= set(scheduler._tasks)
    finally:
        await container.stop()


async def test_the_master_flag_off_enqueues_nothing(clean_tables: Database) -> None:
    container = _container(
        clean_tables, disclosure_feeds_enabled=False, investegate_enabled=True
    )
    await container._enqueue_disclosure_feed("investegate")
    assert await _jobs(clean_tables) == []


async def test_enqueue_uses_the_feed_dedupe_key_and_priority(clean_tables: Database) -> None:
    container = _container(
        clean_tables, disclosure_feeds_enabled=True, investegate_enabled=True
    )
    try:
        await container._enqueue_disclosure_feed("investegate")
    finally:
        await container.stop()
    jobs = await _jobs(clean_tables)
    assert len(jobs) == 1
    assert jobs[0].payload == {"feed": "investegate"}
    assert jobs[0].dedupe_key == "feed:investegate"
    assert jobs[0].priority == 50
```

- [ ] **Step 7: Run the scheduler test to verify it passes**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_scheduler.py -q`
Expected: PASS (6 passed) with the test database; skipped otherwise.

- [ ] **Step 8: Type-check the container edits**

Run: `cd backend && .venv/bin/mypy stockbrain/services.py stockbrain/jobs/disclosure.py`
Expected: `Success: no issues found`.

- [ ] **Step 9: Commit**

```bash
git add backend/stockbrain/jobs/disclosure.py backend/stockbrain/services.py \
  backend/tests/integration/test_disclosure_handler.py \
  backend/tests/integration/test_disclosure_scheduler.py
git commit -m "feat(jobs): disclosure-feed poll handler, schedules and health"
```

---

## Task 7: Identity hand-off (classifier metadata, impact backfill, resolution ISIN)

**Files:**
- Modify: `backend/stockbrain/intelligence/classifier.py`
- Modify: `backend/stockbrain/intelligence/service.py`
- Modify: `backend/stockbrain/instruments/service.py`
- Test: `backend/tests/integration/test_disclosure_identity.py`

**Interfaces:**
- Consumes: `FeedItem` metadata from Task 1 (the `to_document` keys `exchange_hint`, `exchange_hints`, `isin`, `company_name`, `symbols`); the existing `ResolutionRequest.isin_hint`.
- Produces: `ClassificationInput.exchange_hint: str | None`, `ClassificationInput.isin: str | None`; the classifier's rendered `SYMBOL_HINTS` value contains `exchange:`/`isin:`; exchange backfill on matching impacts; an impact-level ISIN hint reaching `ResolutionRequest`.

The prompt file and `PROMPT_VERSION` are untouched: the new facts are folded into the existing `SYMBOL_HINTS` substitution value, not into the template.

---

- [ ] **Step 1: Write the failing identity test**

Create `backend/tests/integration/test_disclosure_identity.py`:

```python
"""Provider-known identity reaches the classifier and the resolver."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from stockbrain.config import Settings
from stockbrain.db.base import utcnow
from stockbrain.db.models.companies import BrokerInstrument, EventCompanyImpact
from stockbrain.db.session import Database
from stockbrain.enums import (
    Broker,
    ResolutionMethod,
    ResolutionStatus,
    SourceCategory,
    SourceProvider,
)
from stockbrain.ingestion.base import RawSourceDocument
from stockbrain.ingestion.service import IngestionService
from stockbrain.instruments.normalize import instrument_name_key
from stockbrain.instruments.service import ResolutionService
from stockbrain.intelligence.classifier import ClassificationInput, EventClassifier
from stockbrain.intelligence.service import ClassificationService
from stockbrain.llm.base import CompletionResult, TokenUsage
from stockbrain.llm.telemetry import LlmTelemetry

pytestmark = pytest.mark.integration


CLASSIFICATION: dict[str, Any] = {
    "relevant_to_public_equities": True,
    "event_type": "REGULATORY",
    "canonical_title": "Barclays updates the market",
    "summary": "Barclays published a regulatory update.",
    "event_time": "2026-09-21T11:46:00Z",
    "novelty": 0.6,
    "importance": 0.7,
    "confidence": 0.8,
    "needs_corroboration": False,
    "topics": ["regulatory"],
    "rationale": "The release names both companies.",
    "companies": [
        {
            "company_name": "Barclays PLC",
            "ticker_hint": "BARC",
            "exchange_hint": None,
            "relationship": "Issuer",
            "impact_path": "direct",
            "direction": "unknown",
            "materiality": 0.5,
            "confidence": 0.7,
        },
        {
            "company_name": "HSBC Holdings plc",
            "ticker_hint": "HSBA",
            "exchange_hint": None,
            "relationship": "Peer",
            "impact_path": "indirect",
            "direction": "unknown",
            "materiality": 0.3,
            "confidence": 0.5,
        },
    ],
}


class ScriptedProvider:
    name = "scripted"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def complete(self, request: Any) -> CompletionResult:
        return CompletionResult(
            content=json.dumps(self._payload),
            model="deepseek-v4-flash",
            usage=TokenUsage(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                cache_hit_tokens=0,
                cache_miss_tokens=100,
            ),
            finish_reason="stop",
            provider_request_id="req-1",
            latency_ms=10,
            started_at=dt.datetime.now(dt.UTC),
            completed_at=dt.datetime.now(dt.UTC),
        )

    async def aclose(self) -> None:
        return None


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "log_level": "CRITICAL",
        "semantic_dedupe_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def _service(database: Database, payload: dict[str, Any]) -> ClassificationService:
    return ClassificationService(
        database,
        _settings(),
        classifier=EventClassifier(ScriptedProvider(payload), model="deepseek-v4-flash"),
        telemetry=LlmTelemetry(),
    )


async def _ingest_disclosure(
    database: Database,
    *,
    provider: SourceProvider,
    metadata: dict[str, Any],
    headline: str,
) -> uuid.UUID:
    result = await IngestionService(database).ingest(
        RawSourceDocument(
            provider=provider,
            provider_item_id="9782378",
            url="https://www.investegate.co.uk/announcement/rns/barclays--barc/x/9782378",
            source_name="Investegate",
            source_category=SourceCategory.REGULATOR,
            headline=headline,
            published_at=dt.datetime(2026, 9, 21, 11, 46, tzinfo=dt.UTC),
            symbols=list(metadata.get("symbols") or []),
            is_distinct_event=True,
            metadata=metadata,
        )
    )
    assert result.event_id is not None
    return result.event_id


async def test_classifier_sees_exchange_and_isin_template_values() -> None:
    classifier = EventClassifier(ScriptedProvider(CLASSIFICATION), model="deepseek-v4-flash")
    messages = classifier.build_messages(
        ClassificationInput(
            headline="Barclays update",
            body="body",
            provider="INVESTEGATE",
            symbol_hints=["BARC"],
            exchange_hint="London Stock Exchange",
            isin="GB0031348658",
        ),
        as_of=dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC),
    )
    assert "exchange: London Stock Exchange" in messages[1].content
    assert "isin: GB0031348658" in messages[1].content


async def test_a_matching_impact_gets_the_exchange_hint_and_a_peer_does_not(
    clean_tables: Database,
) -> None:
    event_id = await _ingest_disclosure(
        clean_tables,
        provider=SourceProvider.INVESTEGATE,
        metadata={
            "language": "en",
            "exchange_hint": "London Stock Exchange",
            "exchange_hints": ["London Stock Exchange", "London Stock Exchange AIM"],
            "company_name": "Barclays",
            "symbols": ["BARC"],
        },
        headline="Barclays: Interim Results",
    )
    service = _service(clean_tables, CLASSIFICATION)

    await service.classify_event(event_id)

    async with clean_tables.session() as session:
        impacts = {
            impact.company_key: impact
            for impact in (
                await session.execute(
                    sa.select(EventCompanyImpact).where(
                        EventCompanyImpact.event_id == event_id
                    )
                )
            ).scalars()
        }
    assert impacts["barclays"].exchange_hint == "London Stock Exchange"
    assert impacts["hsbc"].exchange_hint is None


async def test_the_source_isin_reaches_resolution_for_a_matching_impact(
    clean_tables: Database,
) -> None:
    """The instrument's name does not match the hint, so only the ISIN can resolve it."""
    async with clean_tables.transaction() as session:
        session.add(
            BrokerInstrument(
                broker=Broker.TRADING212,
                broker_ticker="SPIEp_EQ",
                name="Société Parisienne",
                short_name="SPIE",
                isin="FR0012757854",
                currency="EUR",
                instrument_type="STOCK",
                exchange="Euronext Paris",
                market_symbol="SPIE",
                name_key=instrument_name_key("Société Parisienne"),
                is_active=True,
                last_refreshed_at=utcnow(),
                last_seen_at=utcnow(),
            )
        )

    event_id = await _ingest_disclosure(
        clean_tables,
        provider=SourceProvider.EQS,
        metadata={
            "language": "en",
            "isin": "FR0012757854",
            "exchange_hint": None,
            "company_name": "SPIE SA",
            "symbols": [],
        },
        headline="SPIE: Final Terms",
    )
    payload = {
        **CLASSIFICATION,
        "companies": [
            {
                "company_name": "SPIE SA",
                "ticker_hint": None,
                "exchange_hint": None,
                "relationship": "Issuer",
                "impact_path": "direct",
                "direction": "unknown",
                "materiality": 0.5,
                "confidence": 0.7,
            }
        ],
    }
    service = _service(clean_tables, payload)
    await service.classify_event(event_id)

    async with clean_tables.session() as session:
        impact_id = (
            await session.execute(
                sa.select(EventCompanyImpact.id).where(
                    EventCompanyImpact.event_id == event_id
                )
            )
        ).scalar_one()

    outcome = await ResolutionService(clean_tables).resolve_impact(impact_id)
    assert outcome is not None
    assert outcome.status is ResolutionStatus.RESOLVED
    assert outcome.method is ResolutionMethod.ISIN_EXACT
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_identity.py -q`
Expected: FAIL — `ClassificationInput` has no `exchange_hint`/`isin` and the exchange backfill does not exist.

- [ ] **Step 3: Add the classifier template values**

In `backend/stockbrain/intelligence/classifier.py`, add to `ClassificationInput` after `symbol_hints`:

```python
    exchange_hint: str | None = None
    isin: str | None = None
```

In `EventClassifier.build_messages`, replace:

```python
        template = self.prompt
        hints = ", ".join(payload.symbol_hints or []) or "(none)"
```

with:

```python
        template = self.prompt
        hints = ", ".join(payload.symbol_hints or []) or "(none)"
        identity: list[str] = []
        if payload.exchange_hint:
            identity.append(f"exchange: {payload.exchange_hint}")
        if payload.isin:
            identity.append(f"isin: {payload.isin}")
        if identity:
            # The prompt file's metadata block is frozen at v1; provider-known
            # identity is folded into the symbol-hints value so the model sees
            # it without a prompt-version change.
            hints = f"{hints} ({'; '.join(identity)})"
```

- [ ] **Step 4: Backfill `exchange_hint` in the intelligence service**

In `backend/stockbrain/intelligence/service.py`:

**(a)** Add `from typing import Any` after `from dataclasses import dataclass`.
**(b)** Add `from stockbrain.instruments.normalize import normalize_ticker` after the `stockbrain.ingestion...`/`stockbrain.intelligence...` imports (isort places it after `stockbrain.errors` and before `stockbrain.intelligence.classifier`).
**(c)** In `_build_input`, add the two new fields to the returned `ClassificationInput`:

```python
            symbol_hints=symbols[:20],
            exchange_hint=_optional_str(metadata.get("exchange_hint")),
            isin=_optional_str(metadata.get("isin")),
            event_id=event.id,
```

**(d)** At the bottom of the module, add:

```python
def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
```

**(e)** In `_apply_classification`, replace:

```python
            written = await self._upsert_impacts(session, event_id, classification)
```

with:

```python
            source = await self._primary_source(session, event_id)
            source_metadata = (source.provider_metadata if source else {}) or {}
            written = await self._upsert_impacts(
                session, event_id, classification, source_metadata=source_metadata
            )
```

**(f)** Change the `_upsert_impacts` signature to:

```python
    async def _upsert_impacts(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        classification: ClassifiedEvent,
        *,
        source_metadata: dict[str, Any] | None = None,
    ) -> int:
```

and immediately after `if not classification.companies: return 0`, add:

```python
        metadata = source_metadata or {}
        raw_symbols = metadata.get("symbols")
        document_symbol = (
            normalize_ticker(str(raw_symbols[0]))
            if isinstance(raw_symbols, list) and len(raw_symbols) == 1
            else ""
        )
        document_exchange = str(metadata.get("exchange_hint") or "")
```

**(g)** In the `rows.append({...})` body inside the company loop, replace:

```python
                    "ticker_hint": company.ticker_hint,
                    "exchange_hint": company.exchange_hint,
```

with:

```python
                    "ticker_hint": company.ticker_hint,
                    "exchange_hint": _impact_exchange_hint(
                        company.ticker_hint,
                        company.exchange_hint,
                        document_symbol=document_symbol,
                        document_exchange=document_exchange,
                    ),
```

**(h)** At the bottom of the module, next to `_optional_str`, add:

```python
def _impact_exchange_hint(
    ticker_hint: str | None,
    exchange_hint: str | None,
    *,
    document_symbol: str,
    document_exchange: str,
) -> str | None:
    """Use the model's exchange when it gave one, else the feed's.

    The feed knows the venue; the model may not repeat it.  The backfill happens
    only when the document carried exactly one symbol and the impact's
    normalised ticker is that symbol.
    """
    if exchange_hint is not None:
        return exchange_hint
    if not document_symbol or not document_exchange:
        return None
    if normalize_ticker(ticker_hint) != document_symbol:
        return None
    return document_exchange
```

- [ ] **Step 5: Pass the source ISIN into `ResolutionRequest`**

In `backend/stockbrain/instruments/service.py`:

**(a)** Add `from typing import Any` after `import uuid`.
**(b)** Add to the db-model imports:

```python
from stockbrain.db.models.sources import EventSourceLink, Source
```

**(c)** Add `from stockbrain.intelligence.normalize import company_key` after the
`stockbrain.instruments.resolver` import block (isort places `intelligence` after
`instruments` and before `logging`).
**(d)** In `resolve_impact`, replace:

```python
            resolver = InstrumentResolver(session)
            outcome = await resolver.resolve(
                ResolutionRequest(
                    name_hint=impact.company_name_hint,
                    ticker_hint=impact.ticker_hint,
                    exchange_hint=impact.exchange_hint,
                    broker=self._broker,
                )
            )
```

with:

```python
            resolver = InstrumentResolver(session)
            source_metadata = await self._source_metadata(session, impact.event_id)
            outcome = await resolver.resolve(
                ResolutionRequest(
                    name_hint=impact.company_name_hint,
                    ticker_hint=impact.ticker_hint,
                    exchange_hint=impact.exchange_hint,
                    isin_hint=self._isin_hint_for_impact(impact, source_metadata),
                    broker=self._broker,
                )
            )
```

**(e)** Add the two static helpers immediately before `_link_company`:

```python
    @staticmethod
    async def _source_metadata(session: AsyncSession, event_id: uuid.UUID) -> dict[str, Any]:
        """The primary source's provider metadata for one event."""
        stmt = (
            sa.select(Source.provider_metadata)
            .join(EventSourceLink, EventSourceLink.source_id == Source.id)
            .where(EventSourceLink.event_id == event_id)
            .order_by(
                sa.case((EventSourceLink.relationship_type == "PRIMARY", 0), else_=1),
                Source.received_at.asc(),
            )
            .limit(1)
        )
        metadata = (await session.execute(stmt)).scalar_one_or_none()
        return dict(metadata or {})

    @staticmethod
    def _isin_hint_for_impact(
        impact: EventCompanyImpact, metadata: dict[str, Any]
    ) -> str | None:
        """The source's ISIN, but only for an impact the source actually names.

        An impact row has no ISIN column, so the hand-off happens here: the
        document's own identity travels with the impact when the impact's
        ticker or normalised company key matches what the feed printed.
        """
        isin = normalize_isin(str(metadata.get("isin") or ""))
        if not isin:
            return None
        raw_symbols = metadata.get("symbols")
        document_symbol = (
            normalize_ticker(str(raw_symbols[0]))
            if isinstance(raw_symbols, list) and len(raw_symbols) == 1
            else ""
        )
        ticker = normalize_ticker(impact.ticker_hint)
        if document_symbol and ticker and ticker == document_symbol:
            return isin
        document_key = company_key(str(metadata.get("company_name") or ""))
        if document_key and document_key == impact.company_key:
            return isin
        return None
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_disclosure_identity.py -q`
Expected: PASS (3 passed) with the test database; skipped otherwise.

- [ ] **Step 7: Re-run the existing classification and resolution suites**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_classification.py tests/integration/test_instrument_resolution.py -q`
Expected: PASS. The prompt version is unchanged, so the classifier fixtures still match.

- [ ] **Step 8: Type-check the identity edits**

Run: `cd backend && .venv/bin/mypy stockbrain/intelligence/classifier.py stockbrain/intelligence/service.py stockbrain/instruments/service.py`
Expected: `Success: no issues found`.

- [ ] **Step 9: Commit**

```bash
git add backend/stockbrain/intelligence/classifier.py \
  backend/stockbrain/intelligence/service.py \
  backend/stockbrain/instruments/service.py \
  backend/tests/integration/test_disclosure_identity.py
git commit -m "feat(identity): feed exchange/ISIN hints reach impacts and resolution"
```

---

## Task 8: Language hardening (extraction Accept-Language + name suffixes)

**Files:**
- Modify: `backend/stockbrain/extraction/base.py`
- Modify: `backend/stockbrain/extraction/local.py`
- Modify: `backend/stockbrain/extraction/firecrawl.py`
- Modify: `backend/stockbrain/instruments/normalize.py`
- Modify: `backend/stockbrain/jobs/handlers.py`
- Modify: `backend/tests/unit/test_local_extraction.py`
- Modify: `backend/tests/integration/test_content_extraction.py`
- Create: `backend/tests/unit/test_instruments_normalize.py`

**Interfaces:**
- Consumes: `Source.provider_metadata["language"]` written by Task 1's `to_document`.
- Produces: `ContentExtractor.extract(url: str, *, language: str = "en")`; `LocalContentExtractor` sends the per-request `Accept-Language`; `FirecrawlContentExtractor.extract` accepts and ignores it; `_NAME_SUFFIXES` gains the European legal forms.

`jobs/handlers.py` is owned here because this task owns the per-source-language call sites; Task 6 deliberately does not touch it.

---

- [ ] **Step 1: Write the failing extraction-language test**

Append to `backend/tests/unit/test_local_extraction.py`:

```python
async def test_the_request_language_is_sent_per_request() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["language"] = request.headers.get("accept-language", "")
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})

    extractor = _extractor(handler)
    try:
        await extractor.extract("https://example.com/article", language="de")
    finally:
        await extractor.aclose()
    assert seen["language"] == "de"


async def test_the_request_language_defaults_to_english() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["language"] = request.headers.get("accept-language", "")
        return httpx.Response(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})

    extractor = _extractor(handler)
    try:
        await extractor.extract("https://example.com/article")
    finally:
        await extractor.aclose()
    assert seen["language"] == "en"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_local_extraction.py -q -k "request_language"`
Expected: FAIL with `TypeError: extract() got an unexpected keyword argument 'language'`.

- [ ] **Step 3: Make extraction language-aware**

In `backend/stockbrain/extraction/base.py`, change the protocol method:

```python
    async def extract(self, url: str, *, language: str = "en") -> ExtractionResult: ...
```

In `backend/stockbrain/extraction/local.py`:

**(a)** In `__init__`, delete the client header line `"Accept-Language": "en",` (the language is now per request, not a client default).
**(b)** Change `extract`'s signature and first call:

```python
    async def extract(self, url: str, *, language: str = "en") -> ExtractionResult:
        """Fetch and extract one page.  Never raises for an ordinary failure.

        ``language`` is the source's own language from its provider metadata,
        default English; it is sent as ``Accept-Language`` on this request only.
        """
        try:
            fetched = await self._fetch(url, language=language)
```

**(c)** Change `_fetch`'s signature and the request build:

```python
    async def _fetch(self, url: str, *, language: str = "en") -> ExtractionResult:
```

```python
                request = self._client.build_request(
                    "GET", current, headers={"Accept-Language": language}
                )
```

In `backend/stockbrain/extraction/firecrawl.py`, change the signature and document the no-op:

```python
    async def extract(self, url: str, *, language: str = "en") -> ExtractionResult:
        """Scrape one page.  **Never retried.**

        ``language`` is accepted to satisfy the extractor interface but is not
        sent: Firecrawl is the paid fallback, and the free local extractor is
        the one that honours a source's language.  The SSRF check runs here too.
        Firecrawl fetches from its own infrastructure rather than from inside
        this network, so it is not an SSRF path in the usual sense -- but asking
        a paid third party to fetch ``http://postgres:5432`` is still a request
        StockBrain should never make, and it would still be billed.
        """
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_local_extraction.py -q`
Expected: PASS.

- [ ] **Step 5: Update the extraction call sites**

In `backend/stockbrain/jobs/handlers.py`, in `handle_content_extract`, immediately after
`url = source.canonical_url or source.original_url` inside the session block, add:

```python
        language = _source_language(source)
```

Change the local call:

```python
    result: ExtractionResult = await extractor.extract(url, language=language)
```

Change the fallback call:

```python
    fallback = await _firecrawl_fallback(context, source_id, url, failure, language)
```

Change `_firecrawl_fallback`'s signature and its call:

```python
async def _firecrawl_fallback(
    context: HandlerContext,
    source_id: uuid.UUID,
    url: str,
    failure: ExtractionFailure | None,
    language: str,
) -> ExtractionResult | None:
```

```python
    result: ExtractionResult = await fallback.extract(url, language=language)
```

Add the helper immediately before `_firecrawl_fallback`:

```python
def _source_language(source: Source) -> str:
    """The source's own language, defaulting to English.

    A disclosure feed records ``metadata.language`` on ``provider_metadata``;
    every other provider leaves it absent and gets ``en``.
    """
    metadata = source.provider_metadata or {}
    value = metadata.get("language")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "en"
```

Then update the existing fake in `backend/tests/integration/test_content_extraction.py` so its
`RecordingExtractor.extract` accepts the new keyword:

```python
    async def extract(self, url: str, *, language: str = "en") -> ExtractionResult:
        self.calls.append(url)
        self.languages.append(language)
        return self._result
```

and add `self.languages: list[str] = []` to `RecordingExtractor.__init__`:

```python
    def __init__(self, result: ExtractionResult) -> None:
        self._result = result
        self.calls: list[str] = []
        self.languages: list[str] = []
```

- [ ] **Step 6: Run the content-extraction suite**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration/test_content_extraction.py -q`
Expected: PASS with the test database; skipped otherwise.

- [ ] **Step 7: Write the failing suffix test**

Create `backend/tests/unit/test_instruments_normalize.py`:

```python
"""The European legal-form suffixes added for the non-US feeds."""

from __future__ import annotations

import pytest

from stockbrain.instruments.normalize import instrument_name_key

NEW_SUFFIXES = (
    "gmbh",
    "kgaa",
    "sarl",
    "sas",
    "bv",
    "srl",
    "sl",
    "sau",
    "sca",
    "scs",
    "oy",
    "aps",
)


@pytest.mark.parametrize("suffix", NEW_SUFFIXES)
def test_a_new_legal_suffix_is_stripped(suffix: str) -> None:
    assert instrument_name_key(f"Acme {suffix}") == "acme"


def test_a_single_word_suffix_is_not_stripped_away() -> None:
    # The suffix loop only fires while more than one word remains.
    assert instrument_name_key("SL") == "sl"
    assert instrument_name_key("SAS") == "sas"
```

- [ ] **Step 8: Run the test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_instruments_normalize.py -q`
Expected: FAIL for `gmbh` and friends (`instrument_name_key("Acme gmbh") == "acme gmbh"`).

- [ ] **Step 9: Add the suffixes**

In `backend/stockbrain/instruments/normalize.py`, extend `_NAME_SUFFIXES` (keep the set sorted
in spirit; add after `"asa"` and before `"adr"`):

```python
        "asa",
        "gmbh",
        "kgaa",
        "sarl",
        "sas",
        "bv",
        "srl",
        "sl",
        "sau",
        "sca",
        "scs",
        "oy",
        "aps",
        "adr",
```

- [ ] **Step 10: Run the test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/unit/test_instruments_normalize.py -q`
Expected: PASS (14 passed).

- [ ] **Step 11: Type-check and commit**

Run: `cd backend && .venv/bin/mypy stockbrain/extraction stockbrain/instruments/normalize.py stockbrain/jobs/handlers.py`
Expected: `Success: no issues found`.

```bash
git add backend/stockbrain/extraction/base.py backend/stockbrain/extraction/local.py \
  backend/stockbrain/extraction/firecrawl.py backend/stockbrain/instruments/normalize.py \
  backend/stockbrain/jobs/handlers.py backend/tests/unit/test_local_extraction.py \
  backend/tests/unit/test_instruments_normalize.py \
  backend/tests/integration/test_content_extraction.py
git commit -m "feat(extraction): per-source language and European name suffixes"
```

---

## Task 9: `.env.example`, opt-in live tests, final verification

**Files:**
- Modify: `.env.example`
- Modify: `backend/pyproject.toml` (only the ruff `per-file-ignores` entry for the new live test)
- Create: `backend/tests/integration/test_disclosure_feeds_live.py`

**Interfaces:**
- Consumes: every prior task.
- Produces: operator documentation and an opt-in, keyless live smoke test per adapter.

---

- [ ] **Step 1: Document the settings in `.env.example`**

Insert this section after the `# --- Subsystem toggles ---` block (immediately after
`SEC_ENABLED=true` and its comment, before the `RESEARCH_ENABLED` block):

```dotenv
# --- Disclosure feeds (keyless, non-US) --------------------------------------
# All default off.  Staged rollout: deploy with everything off, enable
# Investegate first, watch `sources` and provider health for a day, then the
# rest.  Test-time recipe: TELEGRAM_ENABLED=false, EXECUTION_MODE=research_only,
# T212_ENV=demo.
DISCLOSURE_FEEDS_ENABLED=false
# Contact placed in the shared User-Agent; blank falls back to SEC_CONTACT_EMAIL.
DISCLOSURE_FEED_USER_AGENT_CONTACT=
DISCLOSURE_FEED_TIMEOUT_SECONDS=20

# LSE/AIM regulatory news (Investegate front page): ~300-500 RNS/weekday, about
# 40% boilerplate that is filtered before ingest.
INVESTEGATE_ENABLED=false
INVESTEGATE_INTERVAL_SECONDS=300
INVESTEGATE_MAX_PAGES=3

# Xetra/Gettex/Vienna/SIX issuers that distribute through EQS: ~30-60/day.
EQS_ENABLED=false
EQS_INTERVAL_SECONDS=300

# Madrid.  Inside information (IP) is ~10-30/day; other relevant information
# (OIR) is ~50-100/day.  An empty IP feed is valid; an empty OIR feed is not.
CNMV_ENABLED=false
CNMV_INTERVAL_SECONDS=600

# Euronext FR/NL/BE/PT and Canada issuer wire: 20-80 releases per country/day.
GLOBENEWSWIRE_ENABLED=false
GLOBENEWSWIRE_COUNTRIES=France,Netherlands,Belgium,Portugal,Spain,Canada
GLOBENEWSWIRE_INTERVAL_SECONDS=900

# French issuers, English feed: ~10-20/day.
ACTUSNEWS_ENABLED=false
ACTUSNEWS_INTERVAL_SECONDS=900
```

- [ ] **Step 2: Add the ruff print exemption for the live test**

In `backend/pyproject.toml`, add to `[tool.ruff.lint.per-file-ignores]`:

```toml
# The live disclosure smoke test prints the observed contract; reporting what
# each feed actually returned is its entire purpose.
"tests/integration/test_disclosure_feeds_live.py" = ["T201"]
```

- [ ] **Step 3: Write the live smoke test**

Create `backend/tests/integration/test_disclosure_feeds_live.py`:

```python
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
```

- [ ] **Step 4: Confirm the live suite collects and is deselected by default**

Run: `cd backend && .venv/bin/python -m pytest tests/integration/test_disclosure_feeds_live.py -q`
Expected: `no tests ran` (deselected by `-m "not live"`), exit code 5 is pytest's "no tests collected" — that is the correct opt-in behaviour; the marker is documented in `pyproject.toml`.

Run: `cd backend && .venv/bin/python -m pytest tests/integration/test_disclosure_feeds_live.py --collect-only -q -m live`
Expected: six tests collected.

- [ ] **Step 5: Run the full unit suite**

Run: `cd backend && .venv/bin/python -m pytest tests/unit -q`
Expected: PASS. No network is reached.

- [ ] **Step 6: Run the full integration suite against the test database**

Run: `cd backend && DATABASE_URL_TEST=postgresql+asyncpg://stockbrain:$POSTGRES_PASSWORD@127.0.0.1:5432/stockbrain_test .venv/bin/python -m pytest tests/integration -q`
Expected: PASS. (This needs only the `stockbrain_test` database; do not start compose.)

- [ ] **Step 7: Lint and type-check**

Run: `cd backend && .venv/bin/ruff check .`
Expected: `All checks passed!`

Run: `cd backend && .venv/bin/mypy stockbrain tests`
Expected: `Success: no issues found`.

- [ ] **Step 8: Verify the must-not list**

Run: `git diff --stat -- prompts/`
Expected: no output — no prompt file changed.

Run: `grep -c "ALTER TYPE source_provider ADD VALUE IF NOT EXISTS" backend/alembic/versions/20260921_1200_disclosure_feeds.py`
Expected: `5`.

Run: `git log --oneline -9`
Expected: the nine task commits, each self-contained.

Run: `git diff --name-only HEAD~9..HEAD | sort -u`
Expected: only files from the owned-file map. Confirm `STOCKBRAIN_TECHNICAL_SPEC.md`,
`handoff.md`, `firecrawl_activity_logs.csv` and the LLM archive CSV are **not** present.

- [ ] **Step 9: Commit**

```bash
git add .env.example backend/pyproject.toml backend/tests/integration/test_disclosure_feeds_live.py
git commit -m "docs(disclosure): env documentation and opt-in live smoke tests"
```

---

## Self-Review (spec coverage)

| Spec section | Task |
|---|---|
| §4 architecture modules | 1 (`disclosure_feeds.py`), 2–5 (adapters), 6 (handler) |
| §5.1 `FeedItem` fields | 1 |
| §5.2 `to_document` mapping, regexes, `is_distinct_event`, metadata, `body=None` | 1 |
| §5.3 enum additions (source_provider; free-text JobType/ProviderName) | 1 |
| §6.1 walk-until-known, `max_pages`, `known_release_ids` | 1 (read), 6 (walk) |
| §6.2 boilerplate table, all four providers, IP never filtered | 1 (rules), 2–5 (fixture counts), 6 (filter before ingest) |
| §6.3 `group_releases`, English preference, provider_item_id dedupe | 1 (function), 1 (dedupe regression), 5 (SPIE fixture) |
| §6.4 scheduling, intervals, dedupe key, priority, pause gate | 6 |
| §6.5 shared HTTP helper, User-Agent, Accept-Language, timeout, no retry | 1 |
| §6.6 failure posture (0 items, 403/429, 5xx, malformed row) | 6 |
| §7 exchange backfill, ISIN hand-off, classifier metadata, resolver retry of AIM | 7; Investegate `exchange_hints` from 1 |
| §8 Accept-Language per source, name suffixes, prompts unchanged | 8; research language already English |
| §9 settings, defaults off, staged rollout docs, test recipe | 1 (settings), 6 (gating), 9 (docs) |
| §10 unit + integration + live tests | 1–8 (unit/integration), 9 (live) |
| §11 limitations | accepted, no code |
| §12 must-not list | Global Constraints + Task 9 Step 8 |

**Deviations, deliberately documented:**

1. **Handler location.** `handle_disclosure_feed_poll` lives in a new focused module
   `jobs/disclosure.py` and is registered in `services.start()`, because Task 8 owns
   `jobs/handlers.py` for the language call sites and tasks 6 and 8 run concurrently.
2. **GlobeNewswire French rule.** The spec's literal `Nombre d'actions et de droits de
   vote` does not match the fixture's `NOMBRE TOTAL D'ACTIONS…`; the rule is
   `Nombre (?:total )?d'actions et de droits de vote` so the fixture's real wording is
   filtered. The English rule is unchanged.
3. **CNMV empty channel.** The parser returns `[]` for a present channel with no items;
   `allow_empty=True` on the IP adapter turns that into a healthy no-op. The IP label is
   `"Información privilegiada"` and the OIR label `"Otra información relevante"`.
4. **GlobeNewswire Canada fixture.** The committed capture is a 404 XHTML page, not RSS,
   so it is pinned as the `ProviderResponseError` case; the TSX/TSXV regex is tested with
   an inline RSS string.
5. **EQS category source.** The URL path slug is authoritative because the fixture's
   `data-news-category="Directors' dealings"` attribute is malformed.
6. **ISIN hand-off location.** Because `event_company_impacts` has no ISIN column, the
   match runs where the `ResolutionRequest` is built (`instruments/service.py`), reading
   the event's primary source `provider_metadata`. The exchange backfill stays in
   `intelligence/service.py` as the spec describes.
7. **Investegate LSE/AIM retry is deferred.** `metadata.exchange_hints` carries both
   venues (Task 1), but the spec §7 retry cannot resolve today:
   `normalize_exchange("London Stock Exchange AIM")` returns `"lse"` through the
   existing alias table's first-word fallback, identical to
   `normalize_exchange("London Stock Exchange")`. A retry with the AIM value would
   therefore re-run the same narrowing and return the same `AMBIGUOUS`. Making the
   retry effective is a change to the resolver's exchange normalisation, which the
   dispatch's "no resolver change" constraint excludes; the plan records this as an
   accepted limitation rather than shipping a retry that provably cannot change the
   answer. If a future task adds an `aim` alias (with exact-token matching before the
   first-word fallback), the retry belongs in `instruments/service.py` beside the ISIN
   hand-off.
8. **Pre-existing dedupe bug fixed, not spec-driven.** `find_duplicate_source`'s
   content-hash layer ignored `is_distinct_event`, so two distinct body-less
   regulatory releases sharing a generic headline (e.g. "Interim Results") would
   collide and the second would be dropped as a duplicate. Task 1 fixes
   `backend/stockbrain/ingestion/dedupe.py` to skip that layer when the document is a
   distinct event with a stable provider identity, with its own regression test. This
   is an existing-code correctness fix surfaced by building feeds that produce
   exactly this document shape, not a requirement from the spec.
9. **CNMV headline carries the summary, not just the category.** `_headline`
   originally returned only the bold-tail category label (e.g. "Sobre
   suspensiones..."), dropping the issuer-specific detail that follows the
   `<BR/><BR/>` -- the classifier's only readable text for this provider, since
   CNMV bodies are always `None`. The fix drops only the bold timestamp and
   keeps everything else; the corrected fixture assertions include the real
   ERCROS suspension detail.
10. **`to_document` now prefixes the issuer name onto a headline that omits it,
    and validates its inputs for real.** Spec §5.2 gives `to_document`
    nominal validation (non-empty URL/headline); this plan tightens it to an
    absolute http(s) URL and a timezone-aware, UTC-normalised timestamp, and
    adds an issuer-name prefix (`"{company_name}: {headline}"`, skipped when
    the name is already present) so a generic category label -- EQS's
    "Release of a capital market information" with no ticker, for one -- still
    names the company to the classifier. This applies uniformly to all five
    providers from one shared function rather than touching each adapter.
11. **Investegate and EQS timestamps are converted from local wall-clock time,
    not labelled UTC.** Both pages print local time with no offset in the
    string (`21 Sep 2026 11:46 AM`, `21 September 2026 12:33`), unlike CNMV,
    GlobeNewswire and ActusNews, whose RSS `pubDate` fields carry their own
    timezone that `email.utils.parsedate_to_datetime` reads directly. Investegate
    is `Europe/London` (BST, UTC+1, in effect on the fixture's date); EQS is
    `Europe/Berlin` (CEST, UTC+2) -- confirmed by its own `data-current-time`
    attribute sitting two hours behind a same-moment item. Both adapters now
    localise via `zoneinfo` before converting to UTC; the fixture-derived test
    timestamps are corrected accordingly.

**Type-consistency check:** `FeedItem` (Task 1) fields are used with the same names in
Tasks 2–5 and 6; `FeedHttpClient` is constructed identically by all five adapters;
`to_document`/`is_boilerplate`/`group_releases` signatures match their call sites in
Task 6; `ClassificationInput.exchange_hint`/`isin` (Task 7) match `_build_input`;
`extract(url, *, language)` (Task 8) matches both call sites and the fake in
`test_content_extraction.py`; `known_release_ids` (Task 1) matches the handler's call.

**Placeholder scan:** every step carries the literal code or the exact edit; no
"TBD", "similar to", or "handle edge cases" remains.


