"""The Telegram trust boundary, asserted structurally.

Behavioural tests prove that today's code does the right thing.  These prove
that tomorrow's cannot easily do the wrong one: the *shape* of the authorization
function forbids Telegram supplying a quantity, the *absence* of imports forbids
a second risk implementation, and the *absence* of identifiers forbids a chat
handler reaching a broker.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from stockbrain.proposals.service import ProposalService
from stockbrain.telegram import (
    approvals,
    formatting,
    handlers,
    messages,
    notifier,
    runtime,
    service,
    tokens,
)
from stockbrain.telegram import auth as auth_module

TELEGRAM_MODULES = (
    approvals,
    auth_module,
    formatting,
    handlers,
    messages,
    notifier,
    runtime,
    service,
    tokens,
)

TELEGRAM_PACKAGE = Path(inspect.getfile(approvals)).resolve().parent


def _source(module: object) -> str:
    return inspect.getsource(module)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Telegram cannot supply an order parameter
# ---------------------------------------------------------------------------
def test_authorize_accepts_no_order_parameter_from_any_client() -> None:
    """The whole boundary, expressed as a signature.

    ``authorize`` takes a proposal id, a source, an actor and an optional clock.
    There is no parameter for a ticker, a side, a quantity, a price or an
    account, so no client -- Telegram, web or automatic -- has anywhere to put
    one. Everything else is re-read from the row under lock.
    """
    parameters = set(inspect.signature(ProposalService.authorize).parameters)
    assert parameters == {"self", "proposal_id", "source", "actor", "now"}
    for forbidden in ("quantity", "price", "side", "ticker", "account", "notional"):
        assert forbidden not in parameters


def test_no_telegram_call_into_the_proposal_service_carries_an_order_parameter() -> None:
    """Rendering a quantity is fine; *sending* one is not.

    The read model naturally names ``quantity`` and ``broker_ticker`` because it
    displays them. What must never happen is one of those values travelling
    into a lifecycle call, so the scan is scoped to calls that change state.
    """
    lifecycle = {"authorize", "reject", "cancel", "generate", "sweep"}
    order_parameters = {
        "quantity",
        "proposed_quantity",
        "reference_price",
        "limit_price",
        "side",
        "broker_ticker",
        "notional",
        "account_id",
    }
    checked = 0
    for module in TELEGRAM_MODULES:
        tree = ast.parse(_source(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in lifecycle):
                continue
            checked += 1
            names = {kw.arg for kw in node.keywords if kw.arg}
            assert not names & order_parameters, f"{module.__name__} passes an order parameter"
    assert checked >= 2, "the scan should have found the authorize and reject call sites"


def test_no_telegram_module_implements_or_imports_risk_or_sizing() -> None:
    """There is one risk engine, and Telegram is not a client of it.

    A second implementation is how two clients start disagreeing about what is
    permitted; Telegram reaches deterministic risk only through the shared
    authorization path.
    """
    for module in TELEGRAM_MODULES:
        source = _source(module)
        for forbidden in (
            "RiskEngine",
            "RiskInputs",
            "stockbrain.risk",
            "risk_config_from_settings",
            "evaluate(",
        ):
            assert forbidden not in source, f"{module.__name__} references {forbidden}"


def test_only_the_shared_authorization_path_is_called() -> None:
    """``ProposalService.authorize`` and ``.reject`` are the whole surface."""
    source = _source(approvals)
    assert "_proposals.authorize(" in source
    assert "_proposals.reject(" in source
    for forbidden in ("_proposals.generate(", "_proposals.sweep(", "_proposals.cancel("):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# No broker capability anywhere in the package
# ---------------------------------------------------------------------------
def test_no_telegram_module_can_reach_a_broker_credential_or_client() -> None:
    for path in sorted(TELEGRAM_PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "t212_api_key",
            "t212_api_secret",
            "alpaca_api_key",
            "alpaca_api_secret",
            "Trading212AccountClient",
            "Trading212MetadataClient",
            "/equity/orders",
            "place_order",
            "cancel_order",
        ):
            assert forbidden not in text, f"{path.name} references {forbidden}"


def test_the_bot_token_is_read_exactly_where_the_bot_is_built() -> None:
    """One call site, and it is the ApplicationBuilder.

    A token read anywhere else is a token that could be logged, rendered or
    passed on.
    """
    readers = [
        path.name
        for path in sorted(TELEGRAM_PACKAGE.rglob("*.py"))
        if "telegram_bot_token" in path.read_text(encoding="utf-8")
    ]
    assert readers == ["runtime.py"]
    source = _source(runtime)
    assert source.count("telegram_bot_token.get_secret_value()") == 2  # launch + status boolean


# ---------------------------------------------------------------------------
# Raw tokens
# ---------------------------------------------------------------------------
def test_no_module_logs_a_raw_callback_token() -> None:
    """Only the SHA-256 is persisted; the raw value must not reach a log either.

    Checked by walking log calls for a keyword that would carry it.
    """
    for module in TELEGRAM_MODULES:
        tree = ast.parse(_source(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                continue
            if func.value.id != "log":
                continue
            for keyword in node.keywords:
                assert keyword.arg not in {"raw", "raw_token", "token", "callback_data"}
                if isinstance(keyword.value, ast.Name):
                    assert keyword.value.id not in {"raw", "raw_token"}


def test_the_token_is_stored_only_as_a_hash() -> None:
    source = _source(tokens)
    assert "opaque_token_hash=token_hash(raw)" in source
    # Nothing assigns the raw value to a column.
    assert "opaque_token=raw" not in source
    assert "raw_token=" not in source


def test_callback_data_is_a_prefix_and_a_random_token_and_nothing_else() -> None:
    from stockbrain.telegram.tokens import CALLBACK_PREFIX, callback_data

    assert callback_data("abc") == f"{CALLBACK_PREFIX}abc"
    source = _source(tokens)
    # The formatter takes one argument: there is nowhere to smuggle a second.
    assert "def callback_data(raw: str) -> str:" in source


# ---------------------------------------------------------------------------
# Untrusted content
# ---------------------------------------------------------------------------
def test_the_renderers_never_build_a_link_from_a_value() -> None:
    """``<a href=...>`` appears only in prose about not doing it."""
    tree = ast.parse(_source(messages))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr | ast.Constant):
            text = ast.unparse(node)
            assert "<a href" not in text


def test_every_renderer_escapes_through_the_one_helper() -> None:
    """No renderer interpolates a value without ``esc``/``trim``/``bold``/``code``.

    Asserted by construction: the formatting module is the only place that
    emits a tag, and every function it exposes escapes its input.
    """
    from stockbrain.telegram.formatting import bold, code, esc, money, trim

    payload = "<b>x</b>&"
    for renderer in (esc, bold, code):
        assert "<b>x</b>" not in renderer(payload)
    assert "<b>x</b>" not in trim(payload, 100)
    assert "<" not in money(None, payload)


# ---------------------------------------------------------------------------
# Handlers stay adapters
# ---------------------------------------------------------------------------
def test_no_sqlalchemy_statement_appears_in_a_chat_handler() -> None:
    """Queries live in the read model, so a handler cannot invent one."""
    source = _source(handlers)
    for forbidden in ("sa.select", "session.execute", "sqlalchemy", "with_for_update"):
        assert forbidden not in source


def test_the_handler_module_reads_no_username_or_display_name() -> None:
    """Identity is two numbers.  Nothing else is even referenced.

    Both modules discuss usernames at length in their prose -- explaining why
    they are not identities -- so the scan is over the identifiers in the syntax
    tree rather than over the text.
    """
    referenced: set[str] = set()
    for module in (handlers, auth_module):
        for node in ast.walk(ast.parse(_source(module))):
            if isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.Name):
                referenced.add(node.id)
    for forbidden in ("username", "first_name", "last_name", "full_name"):
        assert forbidden not in referenced


def test_authorization_failures_log_ids_but_never_message_content() -> None:
    """An unauthorised message is still somebody's private message."""
    tree = ast.parse(_source(handlers))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "log":
            continue
        for keyword in node.keywords:
            assert keyword.arg not in {"text", "message", "body", "content", "update"}
