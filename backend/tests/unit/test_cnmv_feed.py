"""The CNMV non-standard RSS parser against the committed fixtures."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stockbrain.config import Settings
from stockbrain.enums import SourceProvider
from stockbrain.errors import ProviderResponseError
from stockbrain.ingestion.cnmv import CNMVFeed
from stockbrain.ingestion.disclosure_feeds import is_boilerplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "disclosure_feeds"
OIR = FIXTURES / "cnmv_oir.xml"
IP_EMPTY = FIXTURES / "cnmv_ip_empty.xml"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "web_auth_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _oir() -> list[Any]:
    return CNMVFeed(_settings(), kind="oir").parse_page(OIR.read_text(encoding="utf-8"))


def test_oir_yields_seventeen_releases() -> None:
    assert len(_oir()) == 17


def test_first_oir_row_fields() -> None:
    first = _oir()[0]
    assert first.provider is SourceProvider.CNMV
    assert first.release_id == "42823"
    assert first.language == "es"
    assert first.url.endswith("Resultado-OIR.aspx?nreg=42823")
    assert first.headline == (
        "Sobre suspensiones, levantamientos y exclusiones de negociación "
        "SOCIEDAD RECTORA DE LA BOLSA DE VALORES DE BARCELONA anuncia la "
        "exclusión de negociación de las acciones de ERCROS, S.A. con efectos "
        "del 22/09/2026, inclusive."
    )
    assert first.published_at == dt.datetime(2026, 9, 21, 10, 33, 40, tzinfo=dt.UTC)
    assert first.company_name == "ERCROS, S.A. (ERCROS)"
    assert first.category == "Otra información relevante"
    assert first.exchange_hint == "Bolsa de Madrid"


def test_last_oir_row_fields() -> None:
    last = _oir()[-1]
    assert last.release_id == "42807"
    assert last.headline == (
        "Sobre instrumentos financieros La Sociedad comunica que va a proceder "
        "a la amortización anticipada total de la emisión de cédulas "
        "territoriales denominada “Cédulas Territoriales – Marzo 2022” "  # noqa: RUF001 - literal fixture text
        "con código ISIN ES0413211A67."
    )
    assert last.company_name == "BANCO BILBAO VIZCAYA ARGENTARIA, S.A."
    assert last.published_at == dt.datetime(2026, 9, 18, 15, 39, 10, tzinfo=dt.UTC)


def test_boilerplate_count_matches_the_fixture() -> None:
    items = _oir()
    assert sum(1 for item in items if is_boilerplate(item)) == 6


def test_inside_information_is_never_filtered_by_the_oir_rules() -> None:
    items = _oir()
    # The three ERCROS suspensions are real events, not warrant-issuer noise.
    assert sum(1 for item in items if "Sobre suspensiones" in item.headline) == 3
    assert all(
        not is_boilerplate(item)
        for item in items
        if item.release_id in {"42823", "42822", "42821"}
    )


def test_an_empty_inside_information_channel_is_valid() -> None:
    feed = CNMVFeed(_settings(), kind="ip")
    assert feed.allow_empty is True
    assert feed.parse_page(IP_EMPTY.read_text(encoding="utf-8")) == []


def test_oir_is_not_allowed_to_be_empty() -> None:
    assert CNMVFeed(_settings(), kind="oir").allow_empty is False


def test_a_missing_channel_raises_provider_response_error() -> None:
    with pytest.raises(ProviderResponseError):
        CNMVFeed(_settings(), kind="oir").parse_page("<rss><Other/></rss>")


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        CNMVFeed(_settings(), kind="other")
