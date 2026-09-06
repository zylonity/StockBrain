"""Live provider verification for the discovery split.  Opt-in, minimal, cheap.

    pytest -m live -s tests/integration/test_web_discovery_live.py

Deselected by default -- ``addopts`` carries ``-m "not live"`` -- and skipped
when a key is absent, so a normal ``pytest`` run never spends anything.

What runs, at most, per invocation:

* **one** Brave search  ($0.005, and inside the $5 monthly credit)
* **one** Exa semantic search  ($0.007, and inside the $10 monthly credit)
* **one** local page fetch of a stable public URL  (free)

What never runs: any Firecrawl call.  Firecrawl charges for every request its
infrastructure processes, and a test that spends the operator's allowance on
each run is a test nobody runs.  A test below asserts that no live test in this
directory calls it.

What these print is the *contract*: the endpoint, the response shape, the field
names and the reported cost.  Never a key.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from stockbrain.config import Settings
from stockbrain.enums import WebDiscoveryKind
from stockbrain.extraction.local import LocalContentExtractor
from stockbrain.ingestion.brave import BraveSearchClient
from stockbrain.ingestion.exa import ExaSearchClient
from stockbrain.ingestion.web_search import WebSearchQuery

pytestmark = pytest.mark.live

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


def _live_settings(**overrides: object) -> Settings:
    """Settings from the repository ``.env``.

    Phase 4's bug 11: a live test that reads only ``os.environ`` can never run,
    because the credentials live in the git-ignored ``.env`` and nothing exports
    them.  ``_env_file`` is passed explicitly for that reason.
    """
    base: dict[str, object] = {"app_env": "local", "log_level": "WARNING"}
    base.update(overrides)
    if _ENV_PATH.is_file():
        return Settings(_env_file=str(_ENV_PATH), **base)  # type: ignore[arg-type]
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Brave: one routine search
# ---------------------------------------------------------------------------
async def test_one_brave_search_reports_its_real_contract() -> None:
    """One request, ten results, no page fetched.

    The assertions are about the *documented* shape rather than about the
    content: what came back is today's news and will not be there tomorrow, but
    the field names have to be what the adapter reads.
    """
    settings = _live_settings()
    if not settings.brave_api_key.get_secret_value():
        pytest.skip("BRAVE_API_KEY is not set")

    client = BraveSearchClient(settings)
    try:
        query = WebSearchQuery(
            query="grid transformer order backlog utility",
            kind=WebDiscoveryKind.ROUTINE,
            # Deliberately small. The price is per request rather than per
            # result, but a smaller answer is a smaller thing to read.
            limit=5,
            freshness_days=7,
        )
        print(f"\nbrave request params: {client.build_params(query)}")
        outcome = await client.search(query)
    finally:
        await client.aclose()

    print(f"brave results: {len(outcome.results)} (returned {outcome.results_returned})")
    for result in outcome.results[:3]:
        print(
            f"  [{result.result_kind}] {result.source_domain} "
            f"published={result.published_at} title={(result.title or '')[:70]!r}"
        )

    assert outcome.results, "Brave returned nothing for a broad thematic query"
    for result in outcome.results:
        assert result.url.startswith("http")
        assert result.provider.value == "BRAVE"
        # Metadata only: a search must never come back with a page body.
        assert result.snippet is None or len(result.snippet) < 2000
    # One request, one billable unit.
    assert outcome.billed_units_reported == 1
    # Brave reports no per-call price; the ledger prices it from the published
    # rate instead.
    assert outcome.cost_usd_reported is None


# ---------------------------------------------------------------------------
# Exa: one semantic search
# ---------------------------------------------------------------------------
async def test_one_exa_semantic_search_reports_its_real_contract() -> None:
    """One request, ten results, and **no contents**.

    The last part is the one that matters: ``contents`` is Exa's equivalent of
    Firecrawl's ``scrapeOptions``, and this asserts that the scheduled shape
    does not ask for it.
    """
    settings = _live_settings()
    if not settings.exa_api_key.get_secret_value():
        pytest.skip("EXA_API_KEY is not set")

    client = ExaSearchClient(settings)
    try:
        query = WebSearchQuery(
            query=(
                "public companies benefiting from transformer shortages caused by "
                "datacenter expansion"
            ),
            kind=WebDiscoveryKind.SEMANTIC,
            limit=5,
            freshness_days=90,
        )
        body = client.build_request(query)
        print(f"\nexa request body: {body}")
        # The assertion that keeps this test cheap, made before the call.
        assert "contents" not in body, "the scheduled Exa shape must not request contents"
        outcome = await client.search(query)
    finally:
        await client.aclose()

    print(f"exa results: {len(outcome.results)} (returned {outcome.results_returned})")
    print(f"exa reported cost: ${outcome.cost_usd_reported}")
    for result in outcome.results[:3]:
        print(
            f"  score={result.score} published={result.published_at} "
            f"id={(result.provider_result_id or '')[:60]!r} "
            f"title={(result.title or '')[:70]!r}"
        )

    assert outcome.results, "Exa returned nothing for a second-order query"
    for result in outcome.results:
        assert result.url.startswith("http")
        assert result.provider.value == "EXA"
        assert result.result_kind == "semantic"
    # ``costDollars`` is the provider's own price and is what the ledger
    # reconciles against. Five results is inside the base per-request price.
    assert outcome.cost_usd_reported is not None
    assert outcome.cost_usd_reported < 1, "one search should not cost a dollar"


# ---------------------------------------------------------------------------
# Local extraction: free, and the only extractor these tests exercise
# ---------------------------------------------------------------------------
async def test_local_extraction_reads_a_stable_public_page() -> None:
    """A live fetch of a page that is not going anywhere.

    Free, so unlike the two above there is no cost argument for keeping it
    small -- the reason it is one page is that one page proves the path.
    """
    settings = _live_settings()
    extractor = LocalContentExtractor(settings)
    try:
        result = await extractor.extract(
            "https://en.wikipedia.org/wiki/Electric_power_transmission"
        )
    finally:
        await extractor.aclose()

    print(f"\nlocal extraction: {result.method.value} status={result.status_code}")
    print(f"  bytes={result.bytes_read} characters={len(result.text or '')}")
    print(f"  title={result.title!r} canonical={result.canonical_url!r}")
    print(f"  failure={result.failure} detail={result.detail}")

    assert result.succeeded, f"local extraction failed: {result.failure} {result.detail}"
    assert result.text is not None
    assert len(result.text) >= settings.content_extract_min_chars
    # Script bodies are stripped, so nothing executable reaches a prompt.
    assert "<script" not in result.text.lower()


async def test_a_publisher_that_refuses_an_honest_agent_is_categorised_not_retried() -> None:
    """Measured, not assumed: ``https://www.sec.gov/`` answers **403** to this
    client.

    SEC EDGAR requires a contact address inside the User-Agent and refuses
    anything else, so a plain honest agent is turned away.  That is precisely
    the ``HTTP_ERROR`` case the paid fallback exists for -- and this test is how
    "the fallback has a real reason to exist" stops being an assumption.

    Note the shape of the answer: a categorised failure, returned rather than
    raised, with no retry. The filings themselves come from the SEC client,
    which does send the required User-Agent.
    """
    settings = _live_settings()
    extractor = LocalContentExtractor(settings)
    try:
        result = await extractor.extract("https://www.sec.gov/about")
    finally:
        await extractor.aclose()

    print(f"\nsec.gov via the generic extractor: {result.failure} status={result.status_code}")
    assert not result.succeeded
    assert result.failure is not None
    # And it *is* eligible for a paid fallback, which is the point.
    assert result.failure.fallback_eligible


# ---------------------------------------------------------------------------
# Firecrawl: deliberately not called
# ---------------------------------------------------------------------------
def test_no_live_firecrawl_call_is_made_by_this_suite() -> None:
    """An assertion about the test suite itself.

    Firecrawl charges for every request its infrastructure processes.  A test
    that calls it spends the operator's allowance on every run, and the Phase 2
    incident is what that costs.  The fallback extractor is verified against
    mocks instead.

    Stated as a test so that adding one is a deliberate act with a failing
    assertion attached, rather than an oversight.
    """
    # Parsed rather than grepped: this test names the forbidden constructors in
    # its own forbidden list, and a substring scan would flag itself.
    forbidden = {"FirecrawlClient", "FirecrawlContentExtractor"}
    live_dir = Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(live_dir.glob("test_*live*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden
            ):
                offenders.append(f"{path.name}: {node.func.id}()")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.endswith("extraction.firecrawl")
            ):
                offenders.append(f"{path.name}: imports {node.module}")
    assert offenders == [], "a live test appears to call Firecrawl: " + "; ".join(offenders)


def test_the_live_marker_is_deselected_by_default() -> None:
    """The promise the marker makes, asserted rather than assumed.

    Without ``-m "not live"`` in ``addopts`` these tests were only opt-in
    because no credentials were configured -- so the moment a real key landed in
    ``.env`` a plain ``pytest`` would start spending on every run.
    """
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    assert '-m "not live"' in pyproject
