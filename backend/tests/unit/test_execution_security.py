"""The Phase 8 boundary, asserted structurally.

Behavioural tests prove today's code does the right thing; these prove
tomorrow's cannot easily do the wrong one.  The property that matters is narrow
and absolute: **the only thing that can spend money is one method on one
adapter, driven from a persisted proposal row.**

Everything here is a scan of source or a check on a signature, so it fails when
somebody *adds* a path rather than when somebody exercises one.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from stockbrain.broker import trading212_orders
from stockbrain.execution import base, fingerprint, models, preflight, reconciliation, service
from stockbrain.execution.service import ExecutionService
from stockbrain.jobs import handlers as job_handlers
from stockbrain.proposals.service import ProposalService
from stockbrain.telegram import approvals as telegram_approvals
from stockbrain.telegram import handlers as telegram_handlers
from stockbrain.telegram import notifier as telegram_notifier

EXECUTION_MODULES = (base, fingerprint, models, preflight, reconciliation, service)
ROOT = Path(inspect.getfile(service)).resolve().parent.parent


def source_of(module: object) -> str:
    return inspect.getsource(module)  # type: ignore[arg-type]


def referenced_names(module: object) -> set[str]:
    """Every identifier a module names, ignoring its prose entirely.

    These modules discuss orders, cancellation and retries at length in their
    docstrings -- explaining why they do not do them -- so a substring scan
    would fail on the documentation.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source_of(module))):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    return names


# ---------------------------------------------------------------------------
# No LLM, no chat message and no HTTP body can reach the broker
# ---------------------------------------------------------------------------
def test_no_llm_module_can_reach_the_execution_layer() -> None:
    """A successful prompt injection has nothing to reach for.

    The research and classification modules do not import the execution
    package, do not name the order adapter, and do not know a broker ticker is
    executable.
    """
    import stockbrain.intelligence.classifier as classifier
    import stockbrain.intelligence.research as research
    import stockbrain.intelligence.research_service as research_service
    import stockbrain.intelligence.service as intelligence_service
    import stockbrain.llm.deepseek as deepseek

    for module in (
        deepseek,
        classifier,
        intelligence_service,
        research,
        research_service,
    ):
        text = source_of(module)
        for forbidden in (
            "stockbrain.execution",
            "ExecutionService",
            "ExecutionCommand",
            "trading212_orders",
            "submit_market_order",
            "BrokerExecutionProvider",
        ):
            assert forbidden not in text, f"{module.__name__} references {forbidden}"


def test_no_telegram_module_can_reach_the_execution_layer() -> None:
    """Telegram remains a UI.

    It can read execution state through the proposal read model, and it can
    authorize a proposal through the shared service. It cannot transmit, and it
    cannot name a quantity, a side or a ticker in a call.
    """
    for module in (telegram_handlers, telegram_approvals, telegram_notifier):
        text = source_of(module)
        for forbidden in (
            "ExecutionService",
            "ExecutionCommand",
            "submit_market_order",
            "trading212_orders",
            "stockbrain.execution",
        ):
            assert forbidden not in text, f"{module.__name__} references {forbidden}"


def test_no_api_schema_accepts_an_order_parameter() -> None:
    """The frontend cannot substitute an execution field.

    Every mutating request body in the system is either a free-text reason, a
    boolean direction, or empty. A body that could name a quantity would be a
    body that could size a trade.
    """
    from stockbrain.api.routes.proposals import ActionRequest
    from stockbrain.api.schemas import (
        ControlChangeRequest,
        KillSwitchRequest,
        ReconciliationTriggerRequest,
    )

    for model in (
        ActionRequest,
        ControlChangeRequest,
        KillSwitchRequest,
        ReconciliationTriggerRequest,
    ):
        fields = set(model.model_fields)
        assert fields <= {"reason", "engaged"}, f"{model.__name__} accepts {fields}"
        # `extra="forbid"` means an unexpected key is rejected rather than ignored.
        assert model.model_config.get("extra") == "forbid", model.__name__


def test_the_execution_command_is_built_from_the_proposal_row_alone() -> None:
    """One function builds it, and it takes no caller-supplied trade data."""
    signature = inspect.signature(service._command_from)
    assert list(signature.parameters) == ["proposal", "settings"]

    body = inspect.getsource(service._command_from)
    # Every field comes off `proposal.` except the session flag, which is a
    # deployment setting rather than a property of the trade.
    for field in ("broker_ticker", "side", "quantity", "broker_environment"):
        assert f"proposal.{field}" in body or f"proposal.proposed_{field}" in body


def test_authorize_and_execute_accept_no_order_parameters() -> None:
    """The whole boundary, expressed as two signatures."""
    assert set(inspect.signature(ProposalService.authorize).parameters) == {
        "self",
        "proposal_id",
        "source",
        "actor",
        "now",
    }
    assert set(inspect.signature(ExecutionService.execute).parameters) == {
        "self",
        "proposal_id",
        "now",
    }


# ---------------------------------------------------------------------------
# Exactly one transmitter, and it never retries
# ---------------------------------------------------------------------------
def test_only_the_order_adapter_posts_to_a_broker() -> None:
    posting = sorted(
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if "client.post(" in path.read_text(encoding="utf-8")
    )
    assert posting == ["broker/trading212_orders.py"], posting


def test_the_order_post_bypasses_the_retrying_helper() -> None:
    """``ProviderHttpClient`` collapses every transport failure into one error.

    That erases the only distinction that matters here -- whether the bytes left
    -- so the order path drives httpx directly and classifies the exception
    itself.
    """
    text = source_of(trading212_orders)
    assert "self._client.post(" in text
    assert 'request_json("POST"' not in text
    assert "retry_safe=True" not in text


def test_no_execution_module_retries_a_mutation() -> None:
    """No loop, no tenacity, no backoff around the submit call."""
    for module in EXECUTION_MODULES:
        names = referenced_names(module)
        for forbidden in ("retry", "retrying", "tenacity", "backoff", "reattempt"):
            assert forbidden not in names, f"{module.__name__} references {forbidden}"

    submit_calls = [
        node
        for node in ast.walk(ast.parse(source_of(service)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit"
    ]
    assert len(submit_calls) == 1, "there must be exactly one call site that transmits"

    # ...and it is not inside a loop.
    tree = ast.parse(source_of(service))
    for node in ast.walk(tree):
        if isinstance(node, ast.For | ast.While):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "submit"
                ):
                    raise AssertionError("the transmission call site is inside a loop")


def test_no_cancel_amend_or_modify_capability_exists() -> None:
    """Phase 8 adds placement and nothing else.

    Cancellation races a fill and its failure is a second unknown on top of the
    first; a kill switch that cancelled would be making a trading decision
    rather than stopping one.
    """
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in (
            "cancel_order",
            "modify_order",
            "amend_order",
            "replace_order",
            "client.delete(",
            "/equity/orders/limit",
            "/equity/orders/stop",
        ):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == []


def test_the_provider_interface_offers_no_arbitrary_request_method() -> None:
    """Four operations, and no escape hatch.

    A ``request``/``call``/``post`` method on the interface would make every
    future broker endpoint reachable without review.
    """
    members = {
        name
        for name, _ in inspect.getmembers(base.BrokerExecutionProvider)
        if not name.startswith("_")
    }
    # `broker` is a class-level annotation rather than a member, so it does not
    # appear here; the five operations plus `environment` are the whole surface.
    # `count_pending` joined in Phase 9 to enforce Trading 212's documented
    # 50-pending-orders-per-ticker limit; it is a *read*, and adding it did not
    # add a mutation.
    assert members == {
        "environment",
        "reserve_slot",
        "count_pending",
        "submit",
        "fetch_order",
        "find_candidates",
    }
    for forbidden in ("request", "call", "post", "get", "delete", "put", "patch"):
        assert forbidden not in members


# ---------------------------------------------------------------------------
# One recovery path, and no secrets anywhere
# ---------------------------------------------------------------------------
def test_only_one_call_site_takes_the_presend_recovery_transition() -> None:
    """``EXECUTING -> APPROVED`` exists for exactly one situation.

    The state machine permits it; the narrowness is enforced here. If a second
    code path started re-arming proposals, this fails.
    """
    users = sorted(
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if "PRESEND_RECOVERY_TARGET" in path.read_text(encoding="utf-8")
        and "state_machine.py" not in str(path)
    )
    assert users == ["execution/service.py"], users

    # And it is only ever reached from a proven pre-send failure.
    retract = inspect.getsource(ExecutionService._retract_unsent)
    assert "PRESEND_RECOVERY_TARGET" in retract
    assert "sent_to_broker = False" in retract


def test_the_send_flag_is_only_retracted_in_that_one_place() -> None:
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if "sent_to_broker = False" in path.read_text(encoding="utf-8")
    )
    assert offenders == ["execution/service.py"], offenders


def test_no_execution_module_reads_a_broker_credential() -> None:
    """Only the adapter builds the Authorization header, and it logs nothing."""
    for module in EXECUTION_MODULES:
        text = source_of(module)
        for forbidden in ("t212_api_key", "t212_api_secret", "get_secret_value", "Basic "):
            assert forbidden not in text, f"{module.__name__} references {forbidden}"

    adapter = source_of(trading212_orders)
    assert adapter.count("get_secret_value()") == 2  # the key and the secret, once each
    assert "_auth_headers" in adapter


def test_the_persisted_request_payload_carries_no_header_or_credential() -> None:
    """``execution_attempts.request_payload`` is read by the API and the GUI."""
    payload_builder = inspect.getsource(service._request_payload)
    assert "command.as_dict()" in payload_builder

    fields = set(models.ExecutionCommand.__dataclass_fields__)
    assert not fields & {"headers", "authorization", "api_key", "secret", "token"}

    stripper = inspect.getsource(service._safe_payload)
    for fragment in ("authorization", "token", "secret", "password", "api_key", "cookie"):
        assert fragment in stripper


def test_no_execution_module_logs_a_credential_shaped_field() -> None:
    for module in EXECUTION_MODULES:
        for node in ast.walk(ast.parse(source_of(module))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            if func.value.id != "log":
                continue
            for keyword in node.keywords:
                assert keyword.arg not in {
                    "headers",
                    "authorization",
                    "api_key",
                    "secret",
                    "token",
                    "password",
                    "request_payload",
                    "body",
                }


# ---------------------------------------------------------------------------
# The job layer
# ---------------------------------------------------------------------------
def test_the_execution_job_handler_is_a_thin_adapter() -> None:
    """No risk arithmetic, no HTTP and no SQL in a job handler."""
    text = inspect.getsource(job_handlers.handle_execute_proposal)
    assert "execution.execute(" in text
    for forbidden in ("sa.select", "session", "httpx", "submit_market_order", "RiskEngine"):
        assert forbidden not in text


def test_the_reconciliation_job_handler_cannot_transmit() -> None:
    text = inspect.getsource(job_handlers.handle_reconcile_execution)
    assert "reconciliation.reconcile(" in text
    for forbidden in ("execute(", "submit", "post"):
        assert forbidden not in text


def test_reconciliation_never_names_the_submit_operation() -> None:
    """The module that resolves ambiguity must not be able to end it by sending."""
    names = referenced_names(reconciliation)
    assert "submit" not in names
    assert "reserve_slot" not in names
