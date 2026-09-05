"""Event price reaction.

The question is "how much has this moved since the event became public?", and
the answer's *provenance* matters as much as its value.  These tests pin the
cases where an honest "I cannot tell you" is the correct output: no bar after
the event, no live quote, no data at all, a provider that is down.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

from stockbrain.enums import BarTimeframe, MarketSession, PriceSource, ReactionStatus
from stockbrain.errors import ProviderUnavailable
from stockbrain.market_data.base import Bar, ProviderCapability, Quote, Trade
from stockbrain.market_data.reaction import PriceReactionCalculator
from stockbrain.market_data.sessions import session_from_us_clock

EVENT_TIME = dt.datetime(2026, 9, 4, 14, 0, tzinfo=dt.UTC)  # 10:00 ET, Friday
NOW = dt.datetime(2026, 9, 4, 14, 30, tzinfo=dt.UTC)


def _bar(minute: int, open_: str, close: str) -> Bar:
    return Bar(
        symbol="AAPL",
        timestamp=EVENT_TIME + dt.timedelta(minutes=minute),
        open=Decimal(open_),
        high=Decimal(close),
        low=Decimal(open_),
        close=Decimal(close),
        volume=1000,
        feed="iex",
    )


def _quote(price: str, *, age_ms: int = 200) -> Quote:
    return Quote(
        symbol="AAPL",
        provider="stub",
        feed="iex",
        price_source=PriceSource.ALPACA_IEX,
        provider_timestamp=NOW,
        received_at=NOW + dt.timedelta(milliseconds=age_ms),
        bid=Decimal(price),
        ask=Decimal(price),
    )


class StubProvider:
    """A market-data provider whose answers the test dictates."""

    name = "stub"

    def __init__(
        self,
        bars: list[Bar] | None = None,
        quote: Quote | None = None,
        *,
        bars_error: Exception | None = None,
        quote_error: Exception | None = None,
    ) -> None:
        self._bars = bars or []
        self._quote = quote
        self._bars_error = bars_error
        self._quote_error = quote_error

    async def latest_quote(self, symbol: str) -> Quote:
        if self._quote_error is not None:
            raise self._quote_error
        assert self._quote is not None
        return self._quote

    async def latest_trade(self, symbol: str) -> Trade:  # pragma: no cover - unused
        raise NotImplementedError

    async def bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        start: dt.datetime,
        end: dt.datetime,
        *,
        limit: int | None = None,
    ) -> Sequence[Bar]:
        if self._bars_error is not None:
            raise self._bars_error
        return [bar for bar in self._bars if start <= bar.timestamp <= end]

    async def capability(self, *, refresh: bool = False) -> ProviderCapability:
        return ProviderCapability(provider=self.name)


async def test_a_move_during_the_regular_session_is_measured_from_the_first_bar_after() -> None:
    provider = StubProvider(
        bars=[
            _bar(-1, "100.00", "100.50"),
            _bar(0, "100.50", "102.00"),
            _bar(5, "102.00", "103.00"),
        ],
        quote=_quote("105.00"),
    )
    reaction = await PriceReactionCalculator(provider).compute("AAPL", EVENT_TIME, now=NOW)

    assert reaction.status is ReactionStatus.OK
    assert reaction.price_at_event == Decimal("100.50")
    assert reaction.price_at_event_basis == "first_bar_at_or_after_event"
    assert reaction.reference_price == Decimal("105.00")
    assert reaction.reference_price_basis == "quote_mid"
    assert reaction.absolute_move == Decimal("4.50")
    assert reaction.percent_move == Decimal("4.4776")
    assert reaction.elapsed_seconds == 1800.0
    assert reaction.session_at_event is MarketSession.REGULAR


async def test_a_pre_market_event_is_labelled_pre_market() -> None:
    event_time = dt.datetime(2026, 9, 4, 11, 0, tzinfo=dt.UTC)  # 07:00 ET
    provider = StubProvider(
        bars=[
            Bar(
                symbol="AAPL",
                timestamp=event_time + dt.timedelta(minutes=1),
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("101"),
                volume=10,
                feed="iex",
            )
        ],
        quote=_quote("102.00"),
    )
    reaction = await PriceReactionCalculator(provider).compute(
        "AAPL", event_time, now=NOW, session=session_from_us_clock(event_time)
    )
    assert reaction.session_at_event is MarketSession.PRE_MARKET
    assert reaction.session_source == "us_clock"
    assert reaction.session_holiday_aware is False
    assert reaction.status is ReactionStatus.OK


async def test_an_after_hours_event_is_labelled_after_hours() -> None:
    event_time = dt.datetime(2026, 9, 4, 21, 0, tzinfo=dt.UTC)  # 17:00 ET
    provider = StubProvider(bars=[], quote=_quote("102.00"))
    reaction = await PriceReactionCalculator(provider).compute(
        "AAPL", event_time, now=event_time + dt.timedelta(minutes=10)
    )
    assert reaction.session_at_event is MarketSession.AFTER_HOURS


async def test_no_bar_after_the_event_falls_back_and_says_so() -> None:
    """Reaching backwards for a price measures a different thing.

    The fallback is offered because it is still useful context, but it is
    labelled and carries a note, so nothing later mistakes it for a reaction.
    """
    provider = StubProvider(bars=[_bar(-5, "100.00", "100.20")], quote=_quote("100.20"))
    reaction = await PriceReactionCalculator(provider).compute("AAPL", EVENT_TIME, now=NOW)

    assert reaction.price_at_event == Decimal("100.20")
    assert reaction.price_at_event_basis == "last_bar_before_event"
    assert any("must not be read as a reaction" in note for note in reaction.notes)


async def test_no_bars_at_all_reports_no_price_at_event() -> None:
    """A market that was shut when the news landed has no price to compare against."""
    provider = StubProvider(bars=[], quote=_quote("100.00"))
    reaction = await PriceReactionCalculator(provider).compute("AAPL", EVENT_TIME, now=NOW)
    assert reaction.status is ReactionStatus.NO_PRICE_AT_EVENT
    assert reaction.absolute_move is None
    assert reaction.percent_move is None


async def test_no_data_at_all_reports_no_data() -> None:
    provider = StubProvider(bars=[], quote_error=ProviderUnavailable("no quote"))
    reaction = await PriceReactionCalculator(provider).compute("AAPL", EVENT_TIME, now=NOW)
    assert reaction.status is ReactionStatus.NO_DATA


async def test_a_missing_quote_falls_back_to_the_last_bar_close() -> None:
    provider = StubProvider(
        bars=[_bar(0, "100.00", "101.00"), _bar(10, "101.00", "104.00")],
        quote_error=ProviderUnavailable("no quote"),
    )
    reaction = await PriceReactionCalculator(provider).compute("AAPL", EVENT_TIME, now=NOW)
    assert reaction.status is ReactionStatus.OK
    assert reaction.reference_price == Decimal("104.00")
    assert reaction.reference_price_basis == "last_bar_close"
    assert any("no live quote" in note for note in reaction.notes)


async def test_a_provider_outage_is_reported_not_swallowed() -> None:
    provider = StubProvider(bars_error=ProviderUnavailable("alpaca: HTTP 503"))
    reaction = await PriceReactionCalculator(provider).compute("AAPL", EVENT_TIME, now=NOW)
    assert reaction.status is ReactionStatus.PROVIDER_UNAVAILABLE
    assert reaction.absolute_move is None


async def test_an_event_timestamped_in_the_future_is_refused() -> None:
    provider = StubProvider(bars=[_bar(0, "100", "101")], quote=_quote("101"))
    reaction = await PriceReactionCalculator(provider).compute(
        "AAPL", NOW + dt.timedelta(hours=1), now=NOW
    )
    assert reaction.status is ReactionStatus.NO_DATA
    assert any("future" in note for note in reaction.notes)


async def test_the_move_is_decimal_arithmetic_end_to_end() -> None:
    provider = StubProvider(bars=[_bar(0, "0.10", "0.10")], quote=_quote("0.30"))
    reaction = await PriceReactionCalculator(provider).compute("AAPL", EVENT_TIME, now=NOW)
    assert reaction.absolute_move == Decimal("0.20")
    assert isinstance(reaction.absolute_move, Decimal)
    assert reaction.percent_move == Decimal("200.0000")


async def test_a_stale_reference_quote_still_records_its_age() -> None:
    """Phase 4 reports the age; the sizing guard is what refuses to use it."""
    provider = StubProvider(bars=[_bar(0, "100", "100")], quote=_quote("110", age_ms=90_000))
    reaction = await PriceReactionCalculator(provider).compute("AAPL", EVENT_TIME, now=NOW)
    assert reaction.reference_quote_age_ms == 90_000
    assert reaction.status is ReactionStatus.OK
