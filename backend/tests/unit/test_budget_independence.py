"""What a budget stop must never halt.

Two budgets exist — LLM spend and Firecrawl credits — and both are hard limits
that stop new paid work.  The property that makes them safe rather than
dangerous is what they *cannot* stop:

* knowing what the broker thinks the account holds;
* resolving an order whose fate is unknown;
* transmitting an order a human already authorized;
* the deterministic risk engine, which never had an opinion about money spent
  on models.

Every test here is structural — an import list or a function signature — because
a behavioural test would prove the current code path happens to be safe, and
these need to stay safe after somebody refactors.
"""

from __future__ import annotations

import inspect
import pathlib

from stockbrain.llm.budget import BudgetGuard

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "stockbrain"


def _import_lines(relative: str) -> str:
    source = (SOURCE_ROOT / relative).read_text()
    return "\n".join(
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    )


# ---------------------------------------------------------------------------
# The LLM budget
# ---------------------------------------------------------------------------
def test_the_llm_budget_is_consulted_only_by_llm_callers() -> None:
    """Structural, not aspirational.

    "A budget stop never halts monitoring" is only true if the modules that do
    the monitoring cannot ask the budget anything. Listed by module rather than
    asserted by behaviour, so a future caller has to change this test.
    """
    permitted = {
        "services.py",  # constructs it, and reports its state in health
        "intelligence/service.py",  # classification
        "intelligence/research_service.py",  # research
        "intelligence/research.py",
        "intelligence/research_transport.py",
        "observability/alerts.py",  # reads the state to *report* it
        "llm/budget.py",
        "llm/deepseek.py",
        "api/routes/discovery.py",  # renders it
        "api/routes/research.py",
    }
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(SOURCE_ROOT))
        if relative in permitted:
            continue
        imports = _import_lines(relative)
        if "llm.budget" in imports:
            offenders.append(relative)
    assert offenders == [], offenders


def test_no_execution_module_can_reach_the_llm_budget() -> None:
    """Once a proposal is authorized, sending it must not depend on DeepSeek.

    A run of the model budget is a cost decision. Letting it block a
    transmission would mean an authorized order's fate depended on how much had
    been spent on text generation that day.
    """
    for relative in (
        "execution/service.py",
        "execution/preflight.py",
        "execution/reconciliation.py",
        "execution/base.py",
        "broker/trading212_orders.py",
        "broker/trading212_account.py",
        "broker/account_state.py",
        "proposals/service.py",
        "risk/engine.py",
        "risk/rules.py",
        "risk/sizing.py",
        "control/state.py",
    ):
        imports = _import_lines(relative)
        assert "llm.budget" not in imports, relative
        assert "BudgetGuard" not in imports, relative
        assert "WorkPriority" not in imports, relative


def test_no_execution_module_can_reach_an_llm_at_all() -> None:
    """Not just the budget: no model, on any path that touches a broker.

    "No LLM makes a broker-safety decision" is a property of the import graph.
    The research layer contributes exactly two scalars to a proposal — an action
    and a confidence — and both are validated and bounded before the engine
    sees them.
    """
    for relative in (
        "execution/service.py",
        "execution/preflight.py",
        "execution/reconciliation.py",
        "execution/base.py",
        "risk/engine.py",
        "risk/rules.py",
        "risk/sizing.py",
        "risk/models.py",
        "broker/trading212_orders.py",
    ):
        imports = _import_lines(relative)
        for forbidden in ("stockbrain.llm", "stockbrain.intelligence", "deepseek"):
            assert forbidden not in imports, f"{relative} imports {forbidden}"


def test_the_hard_limit_stops_even_essential_llm_work() -> None:
    """And says why, rather than failing silently.

    Ingestion and deterministic deduplication keep running, so nothing is lost:
    classification resumes when the budget rolls over or the limit is raised.
    """
    source = (SOURCE_ROOT / "llm" / "budget.py").read_text()
    assert "HARD_EXCEEDED" in source
    # The docstring states the contract; the test asserts it is still stated,
    # because this is the paragraph a future reader will rely on.
    assert "ingestion" in source.lower()
    assert "broker reconciliation" in source.lower()

    signature = inspect.signature(BudgetGuard.check)
    assert "priority" in signature.parameters


# ---------------------------------------------------------------------------
# The Firecrawl budget
# ---------------------------------------------------------------------------
def test_the_firecrawl_budget_is_consulted_only_by_firecrawl_callers() -> None:
    """Budget exhaustion degrades one provider, not the application.

    Alpaca news continues, SEC EDGAR continues, already-ingested events keep
    being classified, research runs and the broker keeps being reconciled.
    """
    permitted = {
        "services.py",
        "jobs/handlers.py",
        "ingestion/firecrawl_budget.py",
        "ingestion/service.py",  # enqueues the content-fetch job
        "observability/alerts.py",
        "api/routes/discovery.py",
    }
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(SOURCE_ROOT))
        if relative in permitted:
            continue
        # Matched on the *import*, not on any mention: `api/schemas.py` names a
        # `FirecrawlBudgetResponse` and `ingestion/firecrawl.py` points at the
        # budget in a docstring, and neither can spend anything.
        if "ingestion.firecrawl_budget" in _import_lines(relative):
            offenders.append(relative)
    assert offenders == [], offenders


def test_no_execution_or_risk_module_can_reach_the_firecrawl_budget() -> None:
    for relative in (
        "execution/service.py",
        "execution/preflight.py",
        "execution/reconciliation.py",
        "proposals/service.py",
        "risk/engine.py",
        "broker/account_state.py",
    ):
        imports = _import_lines(relative)
        assert "firecrawl" not in imports.lower(), relative


def test_the_discovery_providers_are_independent_of_each_other() -> None:
    """A dead Firecrawl must not stop Alpaca news or SEC EDGAR.

    Each provider is its own client with its own rate limiter and its own health
    record; the only shared thing is the ingestion entry point, which is exactly
    what makes the deduplication rules identical across them.
    """
    for relative in ("ingestion/alpaca_news.py", "ingestion/sec_edgar.py"):
        imports = _import_lines(relative)
        assert "firecrawl" not in imports.lower(), relative


def test_alerting_reads_both_budgets_and_changes_neither() -> None:
    """The one module that legitimately sees both.

    It reports them. If it could *spend* against either, an alerting bug would
    become a cost bug.
    """
    source = (SOURCE_ROOT / "observability" / "alerts.py").read_text()
    assert "FirecrawlBudget" in source
    assert "BudgetGuard" in source
    # Reads only: no reservation, no work started.
    assert ".reserve(" not in source
    assert ".check(" not in source
    assert ".record_success(" not in source
    assert ".record_failure(" not in source
