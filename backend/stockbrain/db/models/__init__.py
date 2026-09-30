"""SQLAlchemy models.

Importing this package registers every table on :data:`stockbrain.db.base.Base`
metadata, which is what Alembic autogenerate relies on.
"""

from stockbrain.db.base import Base
from stockbrain.db.models.companies import (
    BrokerExchange,
    BrokerInstrument,
    BrokerWorkingSchedule,
    Company,
    CompanyAlias,
    EventCompanyImpact,
)
from stockbrain.db.models.memory import ThesisOutcome, ThesisOutcomeGrade
from stockbrain.db.models.news_catalyst import NewsCatalystDeadline
from stockbrain.db.models.news_history import (
    DATASET_CANDLES,
    DATASET_NEWS,
    NewsHistoryArticle,
    NewsHistoryBackfillRun,
    NewsHistoryCandle,
    NewsHistoryCoverage,
)
from stockbrain.db.models.portfolio import BrokerOrder, PortfolioSnapshot, Position
from stockbrain.db.models.proposals import (
    ACTIVE_PROPOSAL_STATUSES,
    ApprovalAction,
    ExecutionAttempt,
    RiskEvaluation,
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
    ProviderCall,
    ProviderHealthRecord,
    User,
)

__all__ = [
    "ACTIVE_PROPOSAL_STATUSES",
    "DATASET_CANDLES",
    "DATASET_NEWS",
    "AppSetting",
    "ApprovalAction",
    "AuditLog",
    "Base",
    "BrokerExchange",
    "BrokerInstrument",
    "BrokerOrder",
    "BrokerWorkingSchedule",
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
    "NewsCatalystDeadline",
    "NewsHistoryArticle",
    "NewsHistoryBackfillRun",
    "NewsHistoryCandle",
    "NewsHistoryCoverage",
    "Notification",
    "PortfolioSnapshot",
    "Position",
    "ProviderCall",
    "ProviderHealthRecord",
    "ResearchRun",
    "RiskEvaluation",
    "Source",
    "Thesis",
    "ThesisOutcome",
    "ThesisOutcomeGrade",
    "TradeProposal",
    "User",
]
