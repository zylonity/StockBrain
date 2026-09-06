"""The configuration surface, and the small part of it that is runtime state.

``GET /settings`` renders every setting an operator might need to reason about,
with what it does, what changing it affects, and whether it can be changed at
all.  It is deliberately the only settings *read*, and there is deliberately no
generic settings *write*: a ``PUT /settings/{key}`` would be a way to turn on
live execution over HTTP, and the four gates exist precisely so that turning on
live execution is an act performed on the host with a restart behind it.

The handful of genuinely runtime values each keep their own typed route:

* the pause and the kill switch -- ``/api/v1/system/pause``, ``/resume``,
  ``/kill-switch``;
* the discovery hold -- ``/api/v1/discovery/pause`` and ``/resume``;
* the notification categories -- ``PUT /api/v1/system/telegram/preferences``.

Each writes one known key, records an audit row or a log line naming the actor,
and cannot be induced to write another key by its request body.

No secret is ever rendered.  A secret row carries ``configured`` and nothing
else, and the renderer refuses a ``SecretStr`` structurally rather than by
convention.
"""

from __future__ import annotations

from fastapi import APIRouter

from stockbrain.api.dependencies import DatabaseDep, DbSession, ServicesDep, SettingsDep
from stockbrain.api.schemas import (
    NotificationCategoryResponse,
    NotificationPreferencesRequest,
    NotificationPreferencesResponse,
    SettingGroupResponse,
    SettingResponse,
    SettingsResponse,
)
from stockbrain.api.settings_model import build_settings_view
from stockbrain.control.state import ControlStateService
from stockbrain.db.base import utcnow
from stockbrain.db.models.system import AppSetting
from stockbrain.services import DISCOVERY_PAUSED_KEY
from stockbrain.telegram.preferences import (
    LOCKED_CATEGORIES,
    NOTIFICATION_CATEGORY_DETAIL,
    NotificationCategory,
    NotificationPreferences,
)

router = APIRouter(prefix="/api/v1/system", tags=["system"])

#: The same server-side constant the control routes use.  An actor a client
#: could choose would be an audit trail a client could forge.
WEB_ACTOR = "web:owner"


@router.get(
    "/settings",
    response_model=SettingsResponse,
    summary="This deployment's configuration, described for an operator",
)
async def settings_view(
    settings: SettingsDep, database: DatabaseDep, session: DbSession
) -> SettingsResponse:
    control = await ControlStateService(database).snapshot()
    paused_row = await session.get(AppSetting, DISCOVERY_PAUSED_KEY)
    discovery_paused = bool(paused_row and paused_row.value.get("paused"))

    groups = build_settings_view(
        settings,
        runtime_values={
            "trading_halted": "halted" if control.trading_halted else "running",
            "discovery_hold": "held" if discovery_paused else "running",
            "notification_preferences": "see the categories below",
        },
    )
    return SettingsResponse(
        generated_at=utcnow(),
        groups=[
            SettingGroupResponse(
                key=group.key,
                title=group.title,
                description=group.description,
                warning=group.warning,
                blockers=list(group.blockers),
                settings=[
                    SettingResponse(
                        key=item.key,
                        label=item.label,
                        value=item.value,
                        mutability=item.mutability.value,
                        description=item.description,
                        env_var=item.env_var,
                        unit=item.unit,
                        impact=item.impact,
                        control=item.control,
                        configured=item.configured,
                    )
                    for item in group.settings
                ],
            )
            for group in groups
        ],
    )


@router.get(
    "/telegram/preferences",
    response_model=NotificationPreferencesResponse,
    summary="Which notification categories are delivered",
)
async def notification_preferences(
    database: DatabaseDep, settings: SettingsDep, services: ServicesDep
) -> NotificationPreferencesResponse:
    preferences = _preferences(database, services)
    return await _render(preferences, settings, refresh=False)


@router.put(
    "/telegram/preferences",
    response_model=NotificationPreferencesResponse,
    summary="Enable or disable notification categories",
)
async def update_notification_preferences(
    body: NotificationPreferencesRequest,
    database: DatabaseDep,
    settings: SettingsDep,
    services: ServicesDep,
) -> NotificationPreferencesResponse:
    """Apply a partial update to the per-category switches.

    Unknown category names are ignored rather than rejected, so a client built
    against an older or newer build still saves the categories it does know.  A
    locked category is forced back on; the response says so plainly rather than
    failing the whole save.
    """
    changes: dict[NotificationCategory, bool] = {}
    for name, value in body.categories.items():
        try:
            changes[NotificationCategory(name)] = bool(value)
        except ValueError:
            continue
    preferences = _preferences(database, services)
    await preferences.update(changes, actor=WEB_ACTOR)
    return await _render(preferences, settings, refresh=True)


def _preferences(database: DatabaseDep, services: ServicesDep) -> NotificationPreferences:
    """The container's instance when there is one, so its cache is the one invalidated.

    A second instance would read the same row and hold a second cache, and the
    delivery path would keep using its stale copy for a few seconds after a
    save -- which is exactly the window an operator would test in.
    """
    existing = getattr(services, "notification_preferences", None) if services else None
    if isinstance(existing, NotificationPreferences):
        return existing
    return NotificationPreferences(database)


async def _render(
    preferences: NotificationPreferences,
    settings: SettingsDep,
    *,
    refresh: bool,
) -> NotificationPreferencesResponse:
    snapshot = await preferences.snapshot(refresh=refresh)
    blockers = list(settings.telegram_blockers)
    if not settings.telegram_notifications_enabled:
        blockers.append("TELEGRAM_NOTIFICATIONS_ENABLED is false")
    if not settings.telegram_notification_targets:
        blockers.append("no Telegram chat is configured to notify")
    return NotificationPreferencesResponse(
        notifications_enabled=settings.telegram_notifications_enabled,
        delivery_available=not blockers,
        blockers=list(dict.fromkeys(blockers)),
        updated_at=snapshot.updated_at,
        updated_by=snapshot.updated_by,
        categories=[
            NotificationCategoryResponse(
                category=category.value,
                label=NOTIFICATION_CATEGORY_DETAIL[category].label,
                description=NOTIFICATION_CATEGORY_DETAIL[category].description,
                volume=NOTIFICATION_CATEGORY_DETAIL[category].volume,
                enabled=snapshot.enabled(category),
                locked=category in LOCKED_CATEGORIES,
            )
            for category in NotificationCategory
        ],
    )
