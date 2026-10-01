"""Page Alpaca/Benzinga news per symbol and keep EPS-vs-estimate headlines (resumable)."""
import json, os, re, sys, time
import httpx
sys.path.insert(0, "backend")
from stockbrain.config import Settings

UNIVERSE = [s for s in """AAPL ABBV ABT ACN ADBE AIG AMD AMGN AMT AMZN AVGO AXP BA BAC BK BKNG BLK BMY BRK.B C
CAT CHTR CL CMCSA COF COP COST CRM CSCO CVS CVX DE DHR DIS DUK EMR F FDX GD GE GILD GM GOOG GOOGL
GS HD HON IBM INTC INTU ISRG JNJ JPM KO LIN LLY LMT LOW MA MCD MDLZ MDT MET META MMM MO MRK MS MSFT
NEE NFLX NKE NVDA ORCL PEP PFE PG PM PYPL QCOM RTX SBUX SCHW SO SPG T TGT TMO TMUS TSLA TXN UNH UNP
UPS USB V VZ WFC WMT XOM PLTR UBER ANET""".split()]
PATTERN = re.compile(r"EPS.{0,40}\$?-?\d.*\b(beat|beats|miss|misses|in-line|inline|in line)\b.*estimate", re.I)
out_path, done_path = sys.argv[1], sys.argv[1] + ".done"
done = set(open(done_path).read().split()) if os.path.exists(done_path) else set()
s = Settings()
h = {"APCA-API-KEY-ID": s.alpaca_api_key.get_secret_value(), "APCA-API-SECRET-KEY": s.alpaca_api_secret.get_secret_value()}
with httpx.Client(base_url="https://data.alpaca.markets", headers=h, timeout=60) as client, open(out_path, "a") as out:
    for sym in UNIVERSE:
        if sym in done:
            continue
        token, pages, kept = None, 0, 0
        while True:
            params = {"symbols": sym.replace(".", "/") if "." in sym else sym, "start": "2019-01-01T00:00:00Z",
                      "end": "2026-09-30T00:00:00Z", "limit": 50, "sort": "asc", "include_content": "false"}
            if token:
                params["page_token"] = token
            for attempt in range(6):
                try:
                    r = client.get("/v1beta1/news", params=params)
                    if r.status_code == 429:
                        time.sleep(10 * (attempt + 1)); continue
                    r.raise_for_status(); body = r.json(); break
                except httpx.HTTPError:
                    time.sleep(5 * (attempt + 1))
            else:
                print("FAILED", sym, "page", pages, flush=True); body = None
            if body is None:
                break
            for it in body.get("news", []):
                hl = it.get("headline") or ""
                if PATTERN.search(hl):
                    out.write(json.dumps({"symbol": sym, "t": it.get("created_at"), "h": hl, "symbols": it.get("symbols")}) + "\n")
                    kept += 1
            pages += 1
            token = body.get("next_page_token")
            time.sleep(0.34)
            if not token:
                break
        out.flush()
        if body is not None:
            open(done_path, "a").write(sym + "\n")
        print(sym, "pages", pages, "kept", kept, flush=True)
print("done")
