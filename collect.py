"""
nBarsi — coletor diário de dados de mercado.

Roda no GitHub Actions (ou localmente). Não depende de IA.
Fontes: yfinance (cotações, proventos, indicadores), API SGS do Banco Central (CDI).
Saída: data/market.json, um arquivo único consumido pelo artefato nBarsi.

Uso:  python collect.py            # universo completo
      python collect.py ITUB4 TAEE11   # só alguns tickers (teste)
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "market.json"
YEARS = 15          # profundidade do histórico semanal (retroteste de até 15 anos)
DIV_YEARS = 5       # janela das estatísticas de proventos
START = (pd.Timestamp.today().normalize() - pd.DateOffset(years=YEARS + 1)).date()


def clean(v, nd=4):
    """Converte para float JSON-safe (NaN/inf -> None)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def weekly_grid() -> pd.DatetimeIndex:
    end = pd.Timestamp.today().normalize()
    start = end - pd.DateOffset(years=YEARS)
    grid = pd.date_range(start, end, freq="W-FRI")
    if len(grid) == 0 or grid[-1] < end:
        grid = grid.append(pd.DatetimeIndex([end]))
    return grid


def on_grid(s: pd.Series, grid: pd.DatetimeIndex) -> list:
    """Último valor disponível em cada data da grade (forward fill)."""
    s = s.dropna()
    if s.empty:
        return [None] * len(grid)
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    out = s.reindex(s.index.union(grid)).ffill().reindex(grid)
    out[out.index < s.index[0]] = float("nan")
    return [clean(v, 2) for v in out.tolist()]


# ---------------------------------------------------------------- CDI ----
def fetch_cdi(grid: pd.DatetimeIndex) -> dict:
    """Série 12 do SGS: CDI diário (% ao dia). Monta índice acumulado na grade semanal."""
    start = grid[0] - pd.Timedelta(days=10)
    frames = []
    cur = start
    today = pd.Timestamp.today().normalize()
    while cur <= today:  # a API limita janelas longas; pede de 5 em 5 anos
        end = min(cur + pd.DateOffset(years=2), today)
        url = ("https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados?formato=json"
               f"&dataInicial={cur:%d/%m/%Y}&dataFinal={end:%d/%m/%Y}")
        for attempt in range(5):  # a API do BCB oscila (502/503); tenta de novo com espera crescente
            try:
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(5 * (attempt + 1))
        frames.append(pd.DataFrame(r.json()))
        cur = end + pd.Timedelta(days=1)
    df = pd.concat(frames)
    df["data"] = pd.to_datetime(df["data"], format="%d/%m/%Y")
    df["valor"] = df["valor"].astype(float)
    df = df.drop_duplicates("data").set_index("data").sort_index()
    idx = (1 + df["valor"] / 100).cumprod()
    last_daily = df["valor"].iloc[-1]
    annual = ((1 + last_daily / 100) ** 252 - 1) * 100
    selic = None
    try:
        r = requests.get("https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json", timeout=30)
        selic = float(r.json()[0]["valor"])
    except Exception:
        pass
    return {"index": on_grid(idx * 100, grid), "annual_now": clean(annual, 2),
            "selic_meta": selic, "last_date": df.index[-1].date().isoformat()}


# ------------------------------------------------------------- ativos ----
def history_for(ticker: str, aliases: list[str]):
    for t in [ticker, *aliases]:
        tk = yf.Ticker(f"{t}.SA")
        h = tk.history(start=START.isoformat(), interval="1d", auto_adjust=False, actions=True)
        if h is not None and not h.empty and h["Close"].dropna().shape[0] > 20:
            return t, tk, h
    return None, None, None


def dividend_stats(divs: pd.Series, price: float | None) -> dict:
    """Proventos por ação (data-com), por ano civil e últimos 12 meses."""
    today = pd.Timestamp.today().normalize()
    this_year = today.year
    by_year = {str(y): 0.0 for y in range(this_year - DIV_YEARS, this_year)}
    if not divs.empty:
        for y, v in divs.groupby(divs.index.year).sum().items():
            if str(y) in by_year:
                by_year[str(y)] = float(v)
    last12 = float(divs[divs.index > today - pd.DateOffset(years=1)].sum()) if not divs.empty else 0.0
    vals = list(by_year.values())
    paid_years = sum(1 for v in vals if v > 0)
    avg5 = sum(vals) / len(vals) if vals else 0.0
    cagr = None
    if vals and vals[0] > 0 and vals[-1] > 0:
        cagr = (vals[-1] / vals[0]) ** (1 / (len(vals) - 1)) - 1
    cuts = sum(1 for a, b in zip(vals, vals[1:]) if a > 0 and b < a * 0.8)
    return {
        "dps_by_year": {k: clean(v) for k, v in by_year.items()},
        "dps_12m": clean(last12),
        "dy_12m": clean(last12 / price) if price else None,
        "dps_avg_5y": clean(avg5),
        "dy_avg_5y_on_price": clean(avg5 / price) if price else None,
        "div_years_paid": paid_years,
        "div_cuts_20pct": cuts,
        "dps_cagr": clean(cagr),
    }


def fundamentals(tk) -> dict:
    out = {}
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    out["roe"] = clean(info.get("returnOnEquity"))
    out["pl"] = clean(info.get("trailingPE"), 2)
    out["pvp"] = clean(info.get("priceToBook"), 2)
    out["payout"] = clean(info.get("payoutRatio"))
    out["margem_liquida"] = clean(info.get("profitMargins"))
    out["market_cap"] = clean(info.get("marketCap"), 0)
    ebitda, debt, cash = info.get("ebitda"), info.get("totalDebt"), info.get("totalCash")
    if ebitda and ebitda > 0 and debt is not None:
        out["div_liq_ebitda"] = clean((debt - (cash or 0)) / ebitda, 2)
    else:
        out["div_liq_ebitda"] = None
    out["long_name"] = info.get("longName")
    # consistência de lucro: anos com lucro líquido positivo nas demonstrações anuais
    try:
        inc = tk.income_stmt
        row = None
        for k in ("Net Income", "Net Income Common Stockholders"):
            if k in inc.index:
                row = inc.loc[k].dropna()
                break
        if row is not None and len(row):
            row = row.sort_index()
            out["lucro_anos"] = int(len(row))
            out["lucro_anos_positivos"] = int((row > 0).sum())
            out["lucro_ultimo"] = clean(row.iloc[-1], 0)
            if len(row) >= 2 and row.iloc[0] > 0 and row.iloc[-1] > 0:
                out["lucro_cagr"] = clean((row.iloc[-1] / row.iloc[0]) ** (1 / (len(row) - 1)) - 1)
    except Exception:
        pass
    return out


def collect_asset(a: dict, grid) -> dict:
    used, tk, h = history_for(a["ticker"], a.get("aliases", []))
    if h is None:
        return {"status": "sem_dados"}
    h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
    close = h["Close"].dropna()
    price = float(close.iloc[-1])
    divs = h["Dividends"][h["Dividends"] > 0] if "Dividends" in h else pd.Series(dtype=float)
    adj = h["Adj Close"].dropna() if "Adj Close" in h else close
    r12 = None
    one_year = close.index[-1] - pd.DateOffset(years=1)
    past = adj[adj.index <= one_year]
    if not past.empty:
        r12 = float(adj.iloc[-1] / past.iloc[-1] - 1)
    vol = h["Volume"].dropna().tail(63)
    liq = float((vol * close.reindex(vol.index)).mean()) if not vol.empty else None
    rec = {
        "status": "ok",
        "source_ticker": used,
        "price": clean(price, 2),
        "price_date": close.index[-1].date().isoformat(),
        "ret_12m_total": clean(r12),
        "liquidez_media_3m": clean(liq, 0),
        "close": on_grid(close, grid),
        "divs": [[d.date().isoformat(), clean(v, 6)] for d, v in divs.items()
                 if d >= grid[0]],
    }
    rec.update(dividend_stats(divs, price))
    rec.update(fundamentals(tk))
    return rec


def collect_benchmark(symbol: str, grid, use_adj: bool) -> list:
    h = yf.Ticker(symbol).history(start=START.isoformat(), interval="1d", auto_adjust=False)
    if h is None or h.empty:
        return [None] * len(grid)
    h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
    col = "Adj Close" if use_adj and "Adj Close" in h else "Close"
    return on_grid(h[col], grid)


def main(only: list[str]):
    uni = json.loads((ROOT / "universe.json").read_text(encoding="utf-8"))
    grid = weekly_grid()
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    errors = []
    assets = {}
    for a in uni["assets"]:
        if only and a["ticker"] not in only:
            continue
        try:
            rec = collect_asset(a, grid)
        except Exception as e:  # mantém o dado anterior se a coleta falhar
            errors.append(f"{a['ticker']}: {e}")
            rec = (prev.get("assets", {}).get(a["ticker"]) or {}) | {"status": "erro_coleta"}
        rec.update({k: a[k] for k in ("name", "sector", "profile", "group")})
        assets[a["ticker"]] = rec
        print(f"{a['ticker']:7s} {rec.get('status')} {rec.get('price')}", flush=True)
        time.sleep(0.4)
    bench = {}
    try:
        bench["IBOV"] = collect_benchmark(uni["benchmarks"]["IBOV"], grid, use_adj=False)
        bench["IDIV_proxy"] = collect_benchmark(uni["benchmarks"]["IDIV_proxy"], grid, use_adj=True)
    except Exception as e:
        errors.append(f"benchmarks: {e}")
    try:
        cdi = fetch_cdi(grid)
    except Exception as e:
        errors.append(f"cdi: {e}")
        cdi = prev.get("cdi")
        if not cdi or len(cdi.get("index") or []) != len(grid):  # série anterior desalinhada: não reaproveita
            cdi = None
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dates": [d.date().isoformat() for d in grid],
        "cdi": cdi,
        "benchmarks": bench,
        "benchmark_labels": {"IBOV": "Ibovespa", "IDIV_proxy": "DIVO11 (proxy do IDIV)"},
        "assets": assets,
        "errors": errors,
        "sources": {
            "precos_proventos_indicadores": "Yahoo Finance via yfinance (não oficial; proventos por data-com, podem incluir JCP bruto)",
            "cdi": "Banco Central, SGS série 12",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(payload, encoding="utf-8")
    # versão em JS, publicada como arquivo do artefato (a página não pode fazer fetch externo)
    (OUT.parent / "market.js").write_text("window.NBARSI_MARKET=" + payload + ";\n", encoding="utf-8")
    ok = sum(1 for v in assets.values() if v.get("status") == "ok")
    print(f"\n{ok}/{len(assets)} ativos ok · {len(errors)} erros · {OUT.stat().st_size/1024:.0f} KB")


if __name__ == "__main__":
    main([t.upper() for t in sys.argv[1:]])
