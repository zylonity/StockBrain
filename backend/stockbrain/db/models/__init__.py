"""SQLAlchemy models.

Importing this package registers every table on :data:`stockbrain.db.base.Base`
metadata, which is what Alembic autogenerate relies on.
"""

from stockbrain.db.base import Base
from stockbrain.db.models.companies import (
    BrokerInstrument,
    Company,
    CompanyAlias,
    EventCompanyImpact,
)
from stockbrain.db.models.portfolio import BrokerOrder, PortfolioSnapshot, Position
from stockbrain.db.models.proposals import (
    ACTIVE_PROPOSAL_STATUSES,
    ApprovalAction,
    ExecutionAttempt,
    TradeProposal,
)
from stockbrain.db.models.research import LlmCall, ResearchRun, Thesis
from stockbrain.db.models.sources import Event, EventSourceLink, Source
from stockbrain.db.models.system import (
    AppSetting,
    AuditLog,
    DiscoveryQuery,
    DiscoveryTopic,
    Job,
    Notification,
    ProviderHealthRecord,
    User,
)

__all__ = [
    "ACTIVE_PROPOSAL_STATUSES",
    "AppSetting",
    "ApprovalAction",
    "AuditLog",
    "Base",
    "BrokerInstrument",
    "BrokerOrder",
    "Company",
    "CompanyAlias",
    "DiscoveryQuery",
    "DiscoveryTopic",
    "Event",
    "EventCompanyImpact",
    "EventSourceLink",
    "ExecutionAttempt",
    "Job",
    "LlmCall",
    "Notification",
    "PortfolioSnapshot",
    "Position",
    "ProviderHealthRecord",
    "ResearchRun",
    "Source",
    "Thesis",
    "TradeProposal",
    "User",
]
