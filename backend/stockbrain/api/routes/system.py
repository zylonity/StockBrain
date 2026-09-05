"""System posture and execution control.

``/execution-status`` backs the GUI's permanent execution banner.  It never
returns a credential -- only whether one is configured -- and it reports the
exact reasons live execution is unavailable so the state is never a mystery.

``/control`` exposes the durable pause and kill switch, and the three routes
that change them.  They are the web half of the same controls Telegram drives,
writing the same ``app_settings`` rows through the same service, so the two
clients cannot hold different opinions about whether trading is halted.

Neither control closes a position or cancels a broker order.  That is not a
promise about intent: no order, cancel or amend path exists anywhere in this
process, and a test scans every module to keep it that way.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from stockbrain.api.dependencies import DatabaseDep, ServicesDep, SettingsDep
from stockbrain.api.schemas import (
    ControlChangeRequest,
    ControlFlagResponse,
    ControlStateResponse,
    ExecutionStatusResponse,
    FxStatusResponse,
    KillSwitchRequest,
    TelegramStatusResponse,
    WebSecurityResponse,
)
from stockbrain.control.state import ControlSnapshot, ControlState, ControlStateService
from stockbrain.enums import ProviderStatus
from stockbrain.observability.metrics import METRICS

router = APIRouter(tags=["system"])

#: Every web-originated control change is attributed to the owner's account.
#: A server-side constant on purpose: an actor a client could *choose* would be
#: an audit trail a client could forge. Phase 9 added authentication in front of
#: these routes, and since there is exactly one account the constant remains the
#: honest attribution -- it names the only identity that can reach here.
WEB_ACTOR = "web:owner"

_CONTROL_NOTICE = (
    "Pausing and the kill switch stop new proposals and every authorization path. They "
    "never liquidate a position and never cancel or modify a broker order: StockBrain has "
    "no order, cancel or amend path in this phase."
)

_DEMO_NOTICE = (
    "Trading 212 live execution is disabled. StockBrain is operating against the demo "
    "environment. Trading 212's API Terms prohibit Algorithmic Trading and require prior "
    "written consent before a customised interface is used in the live environment, so live "
    "submission stays hard-disabled until every gate in the configuration is deliberately set."
)

_LIVE_NOTICE = (
    "LIVE execution is enabled. Every order still requires two explicit human confirmations "
    "and a fresh risk, broker and quote revalidation immediately before submission."
)


@router.get(
    "/api/v1/system/execution-status",
    response_model=ExecutionStatusResponse,
    summary="Broker execution posture",
)
async def execution_status(settings: SettingsDep) -> ExecutionStatusResponse:
    permitted = settings.live_execution_permitted
    return ExecutionStatusResponse(
        broker="trading212",
        broker_environment=settings.t212_env.value,
        execution_mode=settings.execution_mode.value,
        live_execution_permitted=permitted,
        broker_credentials_configured=settings.broker_credentials_present,
        blockers=settings.execution_blockers,
        notice=_LIVE_NOTICE if permitted else _DEMO_NOTICE,
    )


@router.get(
    "/api/v1/system/fx",
    response_model=FxStatusResponse,
    summary="Foreign-exchange source and freshness",
)
async def fx_status(services: ServicesDep, settings: SettingsDep) -> FxStatusResponse:
    """What can currently size a cross-currency trade, and how fresh it is.

    Makes one probe request for the configured pair, which is the only honest
    way to answer "is the entitlement there" -- Alpaca's forex endpoint answers
    HTTP 403 on a plan that covers IEX equities, and that is a fact about the
    subscription rather than about the credential.
    """
    probe_pair = f"{settings.fx_probe_base_currency}{settings.fx_probe_quote_currency}"
    base = FxStatusResponse(
        provider=settings.fx_provider.value,
        grade=None,
        configured=settings.fx_configured,
        available=False,
        blockers=settings.fx_blockers,
        max_age_seconds=settings.fx_max_age_seconds,
        reference_max_age_seconds=settings.fx_reference_max_age_seconds,
        allow_reference_grade=settings.fx_allow_reference_grade,
        max_rate_drift_pct=settings.fx_max_rate_drift_pct,
        probe_pair=probe_pair,
    )
    if services is None:
        return base.model_copy(
            update={"blockers": [*base.blockers, "the service container is not running"]}
        )

    capability = await services.fx.capability()
    resolution = await services.fx.resolve(
        from_currency=settings.fx_probe_base_currency,
        to_currency=settings.fx_probe_quote_currency,
    )
    rate = resolution.rate
    # Both sets -- configuration reasons and this-moment reasons -- because a
    # panel showing only one would say "available" while the rate it would have
    # used was four days old. Deduplicated with order preserved: the three
    # sources overlap when the reason is the same, and one blocker printed
    # three times reads as three problems.
    blockers = list(dict.fromkeys([*base.blockers, *capability.blockers, *resolution.blockers]))
    return base.model_copy(
        update={
            "grade": capability.grade.value if capability.grade else None,
            "available": capability.available and resolution.usable,
            "blockers": blockers,
            "detail": capability.detail,
            "probe_rate": rate.rate if rate else None,
            "probe_age_seconds": rate.age_seconds(resolution.resolved_at) if rate else None,
            "probe_provider_timestamp": rate.provider_timestamp if rate else None,
        }
    )


@router.get(
    "/api/v1/system/web-security",
    response_model=WebSecurityResponse,
    summary="Web authentication and CSRF posture",
)
async def web_security(settings: SettingsDep) -> WebSecurityResponse:
    """Whether this deployment's HTTP surface is actually protected.

    Reported, not assumed. A deployment relying on network trust is a supported
    configuration; a deployment that *thinks* it has authentication and does not
    is the failure this endpoint exists to make visible.
    """
    from stockbrain.api.auth import (
        CSRF_HEADER_NAME,
        PUBLIC_PATHS,
        cookie_secure,
        web_auth_blockers,
    )

    blockers = web_auth_blockers(settings)
    return WebSecurityResponse(
        auth_enabled=settings.web_auth_enabled,
        auth_effective=not blockers,
        blockers=blockers,
        trusted_network_acknowledged=settings.web_trusted_network_acknowledged,
        session_ttl_seconds=settings.web_session_ttl_seconds,
        cookie_secure=cookie_secure(settings),
        cookie_samesite="strict",
        csrf_header=CSRF_HEADER_NAME,
        public_paths=sorted(PUBLIC_PATHS),
        cors_allow_origins=list(settings.cors_allow_origins),
    )


@router.get(
    "/api/v1/system/control",
    response_model=ControlStateResponse,
    summary="Durable execution control state",
)
async def control_state(database: DatabaseDep) -> ControlStateResponse:
    """Read the pause and kill switch straight from PostgreSQL.

    Read per request rather than from a cached flag: the state can be changed
    from Telegram, from this API, or by another process against the same
    database, and a cache is what would let those disagree.
    """
    return _control_response(await ControlStateService(database).snapshot())


@router.post(
    "/api/v1/system/pause",
    response_model=ControlStateResponse,
    summary="Pause new proposals and all authorization",
)
async def pause(body: ControlChangeRequest, database: DatabaseDep) -> ControlStateResponse:
    snapshot = await ControlStateService(database).pause(
        actor=WEB_ACTOR, source="HUMAN_WEB", reason=body.reason
    )
    return _control_response(snapshot)


@router.post(
    "/api/v1/system/resume",
    response_model=ControlStateResponse,
    summary="Lift a pause",
)
async def resume(body: ControlChangeRequest, database: DatabaseDep) -> ControlStateResponse:
    """Lift the pause.  Deliberately does **not** release the kill switch.

    If ``/resume`` also cleared an emergency stop, the emergency stop would be
    one routine action away from being undone by somebody who only meant to
    restart normal work.
    """
    snapshot = await ControlStateService(database).resume(
        actor=WEB_ACTOR, source="HUMAN_WEB", reason=body.reason
    )
    return _control_response(snapshot)


@router.post(
    "/api/v1/system/kill-switch",
    response_model=ControlStateResponse,
    summary="Engage or release the emergency kill switch",
)
async def kill_switch(body: KillSwitchRequest, database: DatabaseDep) -> ControlStateResponse:
    """Engage or release the emergency stop (spec section 20).

    Engaging is deliberately unconfirmed -- every consequence is in the safe
    direction -- while releasing is the explicit second act ``/resume`` refuses
    to perform.
    """
    control = ControlStateService(database)
    if body.engaged:
        snapshot = await control.engage_kill_switch(
            actor=WEB_ACTOR, source="HUMAN_WEB", reason=body.reason
        )
    else:
        snapshot = await control.release_kill_switch(
            actor=WEB_ACTOR, source="HUMAN_WEB", reason=body.reason
        )
    return _control_response(snapshot)


@router.get(
    "/api/v1/system/telegram",
    response_model=TelegramStatusResponse,
    summary="Telegram bot health",
)
async def telegram_status(settings: SettingsDep, services: ServicesDep) -> TelegramStatusResponse:
    """Report the bot's health without reporting anything about the bot's token.

    A deployment with no bot answers ``DISABLED`` with the exact reasons, which
    is the same wording the startup log and ``/status`` in the chat use.
    """
    runtime = getattr(services, "telegram", None) if services else None
    if runtime is None:
        blockers = settings.telegram_blockers or ["the Telegram runtime is not started"]
        return TelegramStatusResponse(
            status=ProviderStatus.DISABLED if settings.telegram_blockers else ProviderStatus.DOWN,
            bot_configured=bool(settings.telegram_bot_token.get_secret_value()),
            transport="long_polling",
            webhook_configured=False,
            polling=False,
            authorized_users=len(settings.telegram_allowed_user_ids),
            authorized_chats=len(settings.telegram_allowed_chat_ids),
            notification_targets=len(settings.telegram_notification_targets),
            group_chats_allowed=settings.telegram_allow_group_chats,
            blockers=blockers,
        )
    payload = runtime.status()
    try:
        return TelegramStatusResponse.model_validate(payload)
    except ValueError as exc:  # pragma: no cover - the runtime builds this dict
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="telegram status could not be rendered",
        ) from exc


def _control_response(snapshot: ControlSnapshot) -> ControlStateResponse:
    return ControlStateResponse(
        trading_halted=snapshot.trading_halted,
        blockers=snapshot.blockers,
        paused=_flag(snapshot.paused),
        kill_switch=_flag(snapshot.kill_switch),
        notice=_CONTROL_NOTICE,
    )


def _flag(state: ControlState) -> ControlFlagResponse:
    return ControlFlagResponse(
        flag=state.flag.value,
        active=state.active,
        changed_at=state.changed_at,
        actor=state.actor,
        source=state.source,
        reason=state.reason,
    )


@router.get("/metrics", include_in_schema=False, summary="Prometheus metrics")
async def metrics() -> Response:
    return Response(content=METRICS.render(), media_type="text/plain; version=0.0.4")
