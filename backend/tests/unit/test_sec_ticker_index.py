"""EDGAR's ticker index: one CIK owns many symbols."""

from __future__ import annotations

import httpx

from stockbrain.config import Settings
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.ingestion.sec_edgar import SecEdgarClient


async def test_ticker_index_keeps_every_share_class_and_adr() -> None:
    """Several tickers share one CIK, so the index must not be keyed by CIK.

    Inverting the CIK-keyed ``company_tickers()`` map dropped 2,407 of EDGAR's
    10,412 symbols, and which sibling survived depended on file order: TSM
    resolved as TSMWF and GOOGL as GOOGN, so every ADR and secondary share class
    silently lost its fundamentals.
    """
    payload = {
        "0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet"},
        "1": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet"},
        "2": {"cik_str": 1046179, "ticker": "TSM", "title": "TSMC"},
        "3": {"cik_str": 1046179, "ticker": "TSMWF", "title": "TSMC"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = SecEdgarClient(Settings(app_env="test", sec_contact_email="me@example.com"))
    client._www = ProviderHttpClient(
        provider="sec",
        base_url="",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://www.sec.gov/"
        ),
    )
    try:
        index = await client.ticker_to_cik()
    finally:
        await client.aclose()

    assert index == {
        "GOOGL": "0001652044",
        "GOOG": "0001652044",
        "TSM": "0001046179",
        "TSMWF": "0001046179",
    }


async def test_company_tickers_keeps_every_symbol_for_a_shared_cik() -> None:
    """The CIK-keyed map must not drop a company's other symbols.

    Overwriting per CIK left one ticker per company -- the last in file order --
    which under-reported EDGAR's 10,412 symbols as 8,005 in the health panel.
    """
    payload = {
        "0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet"},
        "1": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet"},
        "2": {"cik_str": 1652044, "ticker": "GOOGN", "title": "Alphabet"},
        "3": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = SecEdgarClient(Settings(app_env="test", sec_contact_email="me@example.com"))
    client._www = ProviderHttpClient(
        provider="sec",
        base_url="",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://www.sec.gov/"
        ),
    )
    try:
        companies = await client.company_tickers()
    finally:
        await client.aclose()

    assert set(companies) == {"0001652044", "0000320193"}
    assert companies["0001652044"]["tickers"] == ["GOOGL", "GOOG", "GOOGN"]
    # The primary is the first symbol listed, not whichever overwrote last.
    assert companies["0001652044"]["ticker"] == "GOOGL"
    assert sum(len(e["tickers"]) for e in companies.values()) == len(payload)
