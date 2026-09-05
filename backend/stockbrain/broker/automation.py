"""Whether a broker permits StockBrain to authorize proposals without a human.

Automation is a **broker capability**, not a StockBrain preference.  Each broker
advertises its own answer for its own environment, so adding a second broker
means adding a function here rather than editing a condition buried in the
proposal service, and one broker's permission never becomes another's.

Trading 212's answer is shaped by its API Terms, verified 2026-09-04 and
recorded in ``docs/sources.md``:

* clause **4.2(a)** expressly prohibits using the API for *Algorithmic
  Trading*, defined as a computer automatically determining order parameters --
  whether to initiate, timing, price, quantity or subsequent management -- with
  limited or no human intervention;
* clause **6.6** requires prior written consent for high-speed or automated
  mass data entry;
* clause **6.7** requires a customised interface to be tested before live
  deployment and makes its use subject to prior written consent.

An AUTOMATIC execution policy against the **live** environment is exactly the
activity clause 4.2(a) names.  So live automation requires an explicit
configuration flag asserting that the required broker permission has actually
been obtained.  The flag records a fact about the operator's relationship with
their broker; it is not a switch that makes the rule go away, and there is
deliberately no general "ignore broker rules" bypass anywhere in this module.

The **demo** (paper) environment trades no real money and is the recommended
path for exercising automatic authorization end to end.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stockbrain.enums import Broker

if TYPE_CHECKING:
    from stockbrain.config import Settings

__all__ = [
    "BrokerAutomationCapability",
    "automation_capability",
    "trading212_automation_capability",
]


@dataclass(frozen=True, slots=True)
class BrokerAutomationCapability:
    """What one broker permits in one environment."""

    broker: Broker
    environment: str
    automation_supported: bool
    blockers: tuple[str, ...] = ()
    detail: str = ""

    @property
    def permitted(self) -> bool:
        """Defined as "no blockers remain", so the flag and the list of reasons
        can never disagree -- a banner that lists a blocker beside "permitted"
        would be worse than either answer alone."""
        return self.automation_supported and not self.blockers

    def as_dict(self) -> dict[str, object]:
        return {
            "broker": self.broker.value,
            "environment": self.environment,
            "automation_supported": self.automation_supported,
            "permitted": self.permitted,
            "blockers": list(self.blockers),
            "detail": self.detail,
        }


def trading212_automation_capability(settings: Settings) -> BrokerAutomationCapability:
    """Trading 212's automation answer for the configured environment."""
    from stockbrain.config import BrokerEnvironment

    environment = settings.t212_env.value
    blockers: list[str] = []

    if not settings.broker_credentials_present:
        blockers.append(
            "Trading 212 API credentials are not configured, so no account state can be read"
        )

    if settings.t212_env is BrokerEnvironment.LIVE:
        if not settings.t212_automated_trading_consent_confirmed:
            blockers.append(
                "T212_AUTOMATED_TRADING_CONSENT_CONFIRMED is false: Trading 212's API Terms "
                "clause 4.2(a) prohibit Algorithmic Trading, and clauses 6.6/6.7 require prior "
                "written consent before a customised interface determines order parameters "
                "automatically in the live environment"
            )
        detail = (
            "Live automatic authorization requires recorded Trading 212 written consent for "
            "automated order determination, in addition to every existing live-execution gate."
        )
    else:
        detail = (
            "Demo (paper) trading risks no real funds and is the supported path for exercising "
            "automatic authorization end to end."
        )

    return BrokerAutomationCapability(
        broker=Broker.TRADING212,
        environment=environment,
        automation_supported=True,
        blockers=tuple(blockers),
        detail=detail,
    )


#: One entry per broker.  A future broker advertises its own automation policy
#: by registering here; it never inherits Trading 212's answer.
_CAPABILITIES: dict[Broker, Callable[[Settings], BrokerAutomationCapability]] = {
    Broker.TRADING212: trading212_automation_capability,
}


def automation_capability(
    settings: Settings, broker: Broker = Broker.TRADING212
) -> BrokerAutomationCapability:
    """What ``broker`` permits under this configuration.

    An unregistered broker is reported as *not* supporting automation, which is
    the fail-closed default: a broker whose policy nobody has written down has
    not granted permission.
    """
    factory = _CAPABILITIES.get(broker)
    if factory is None:
        return BrokerAutomationCapability(
            broker=broker,
            environment="unknown",
            automation_supported=False,
            blockers=(f"{broker.value} has not declared an automation capability",),
            detail="A broker with no declared automation policy is treated as forbidding it.",
        )
    return factory(settings)
