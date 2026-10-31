"""
nBarsi — coletor da central de notícias.

Roda no GitHub Actions depois do collect.py. Não depende de IA.
Fontes:
  - CVM, dados abertos (IPE e FCA): fatos relevantes, comunicados, avisos aos acionistas e documentos de proventos.
  - Google Notícias (RSS): manchetes recentes por ativo e por tema macro.
  - Banco Central (SGS): Selic meta, IPCA 12 meses, dólar PTAX.
  - Yahoo Finance: Brent e minério de ferro; agenda (próximo resultado, data-com).
  - timeline.json: linha do tempo macro curada.
  - data/news_curated.json: classificação de impacto, explicações de inflexões e sugestões de proventos,
    escritas pela tarefa diária do Claude.
Saídas: data/news_raw.json (coleta) e data/news.js (window.NBARSI_NEWS, publicado no artefato).

Uso:  python news.py            # coleta tudo e gera news.js
      python news.py --build    # só junta raw + curadoria + linha do tempo em news.js (sem rede)
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

import pandas as pd
import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
RAW = DATA / "news_raw.json"
CUR = DATA / "news_curated.json"
OUTJS = DATA / "news.js"
UA = {"User-Agent": "Mozilla/5.0 (nBarsi coletor pessoal; +https://github.com/esouzasp/nbarsi-data)"}
CVM = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
FEED_DAYS = 120          # notícias e documentos guardados no feed
PROV_YEARS = 6           # anos de documentos de proventos (conciliação)
ZZ_ASSET, ZZ_INDEX = 0.25, 0.15   # limiar do zigue-zague (inflexões)

# nomes genéricos demais para busca: termo alternativo
QUERY = {
    "B3SA3": '"B3SA3" OR "ações da B3"', "TIMS3": '"TIM Brasil" OR TIMS3', "VALE3": '"Vale" VALE3',
    "AXIA3": '"Axia Energia" OR Eletrobras OR AXIA3', "CURY3": '"Cury" CURY3', "RANI3": '"Irani" RANI3',
    "WEGE3": '"WEG" WEGE3', "ITSA4": 'Itaúsa OR ITSA4', "ISAE4": '"ISA Energia" OR ISAE4 OR "Isa Cteep"',
    "PSSA3": '"Porto Seguro" PSSA3 OR "Porto Seguro" ações', "LEVE3": '"Mahle" LEVE3 OR "Metal Leve"',
}
MACRO_Q = {
    "juros": "Copom Selic", "bolsa": "Ibovespa fechamento", "cambio": "dólar real fechamento",
    "petroleo": "petróleo Brent", "minerio": "minério de ferro preço", "eletricas": "setor elétrico Aneel tarifa",
    "proventos": "dividendos JCP anúncio", "fiscal": "arcabouço fiscal governo",
}
PROV_RE = re.compile(r"dividend|juros sobre (o )?capital|\bjcp\b|\bjscp\b|provento|remunera[çc][ãa]o aos acionistas|bonifica", re.I)
FEED_CATS = ("Fato Relevante", "Comunicado ao Mercado", "Aviso aos Acionistas")


def log(*a):
    print(*a, flush=True)


def get(url, tries=4, timeout=60, **kw):
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(4 * (i + 1))


def hid(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def clean(v, nd=4):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else round(f, nd)


# --------------------------------------------------------------- CVM ----
def cvm_csv(url: str, name_part: str) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(get(url, timeout=120).content))
    names = [n for n in z.namelist() if name_part in n]
    if not names:
        raise RuntimeError(f"{name_part} não encontrado em {url}")
    with z.open(names[0]) as f:
        return pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)


def col(df: pd.DataFrame, *keys) -> str:
    for k in keys:
        for c in df.columns:
            if c.lower() == k.lower():
                return c
    for k in keys:
        for c in df.columns:
            if k.lower() in c.lower():
                return c
    raise KeyError(f"coluna {keys} ausente: {list(df.columns)}")


def cvm_ticker_map(tickers: list[str], year: int) -> dict:
    """CNPJ de cada ticker pelo FCA (valores mobiliários). Casa pelo radical de 4 letras."""
    df = None
    for y in (year, year - 1):
        try:
            df = cvm_csv(f"{CVM}/FCA/DADOS/fca_cia_aberta_{y}.zip", "valor_mobiliario")
            break
        except Exception as e:
            log("FCA", y, e)
    if df is None:
        return {}
    c_cnpj, c_cod = col(df, "CNPJ_Companhia"), col(df, "Codigo_Negociacao", "Negociacao")
    try:
        c_nome = col(df, "Nome_Empresarial", "Nome")
    except KeyError:
        c_nome = None
    df = df.dropna(subset=[c_cod])
    out = {}
    for t in tickers:
        root = t[:4]
        hit = df[df[c_cod].str.upper().str.startswith(root)]
        if hit.empty:
            continue
        cnpjs = hit[c_cnpj].value_counts()
        out[t] = {"cnpj": cnpjs.index[0], "nome_cvm": hit[c_nome].iloc[0] if c_nome else None,
                  "codigos": sorted(set(hit[c_cod].str.upper()))}
    return out


def cvm_ipe(tmap: dict, year: int, errors: list) -> tuple[list, dict, list]:
    by_cnpj = {}
    for t, v in tmap.items():
        by_cnpj.setdefault(v["cnpj"], []).append(t)
    feed, prov, agenda = [], {}, []
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=FEED_DAYS)
    for y in range(year - PROV_YEARS + 1, year + 1):
        try:
            df = cvm_csv(f"{CVM}/IPE/DADOS/ipe_cia_aberta_{y}.zip", "ipe_cia_aberta")
        except Exception as e:
            errors.append(f"cvm ipe {y}: {e}")
            continue
        C = {k: col(df, k) for k in ("CNPJ_Companhia", "Categoria", "Tipo", "Especie", "Assunto", "Data_Entrega",
                                      "Data_Referencia", "Link_Download")}
        try:
            C["Protocolo_Entrega"] = col(df, "Protocolo_Entrega")
            C["Versao"] = col(df, "Versao")
        except KeyError:
            pass
        df = df[df[C["CNPJ_Companhia"]].isin(by_cnpj.keys())].copy()
        if "Versao" in C:  # só a última versão de cada protocolo
            df["_v"] = pd.to_numeric(df[C["Versao"]], errors="coerce").fillna(1)
            df = df.sort_values("_v").drop_duplicates(C["Protocolo_Entrega"], keep="last")
        df["_d"] = pd.to_datetime(df[C["Data_Entrega"]], errors="coerce")
        for _, r in df.iterrows():
            txt = " ".join(str(r[C[k]]) for k in ("Tipo", "Especie", "Assunto") if pd.notna(r[C[k]]))
            cat = str(r[C["Categoria"]])
            d = r["_d"]
            if pd.isna(d):
                continue
            base = {"date": d.date().isoformat(), "categoria": cat, "assunto": txt.strip()[:300],
                    "url": r[C["Link_Download"]], "ref": str(r[C["Data_Referencia"]])[:10]}
            for t in by_cnpj[r[C["CNPJ_Companhia"]]]:
                item = {**base, "tickers": [t]}
                if PROV_RE.search(txt) or (cat == "Aviso aos Acionistas" and "provent" in txt.lower()):
                    prov.setdefault(t, []).append({**base})
                if d >= cutoff and cat in FEED_CATS:
                    feed.append({**item, "id": "cvm-" + hid(base["url"] + t), "kind": "cvm", "source": "CVM"})
                if d >= cutoff and "Calend" in cat:
                    agenda.append({"date": base["date"], "ticker": t, "kind": "calendario",
                                   "title": "Calendário de eventos corporativos publicado", "url": base["url"]})
        log(f"IPE {y}: {len(df)} documentos das empresas do universo")
    for t in prov:
        prov[t] = sorted(prov[t], key=lambda x: x["date"], reverse=True)[:60]
    return feed, prov, agenda


# ------------------------------------------------------ Google News ----
def gnews(q: str, days: int) -> list:
    url = ("https://news.google.com/rss/search?q=" + quote(f"{q} when:{days}d")
           + "&hl=pt-BR&gl=BR&ceid=BR:pt-419")
    root = ET.fromstring(get(url, timeout=30).content)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        try:
            d = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            continue
        source = src.text.strip() if src is not None and src.text else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        out.append({"title": title, "url": link, "source": source, "date": d.date().isoformat(),
                    "ts": d.isoformat(timespec="minutes")})
    return out


def headlines(uni: dict, errors: list) -> list:
    items = []
    for a in uni["assets"]:
        t = a["ticker"]
        q = QUERY.get(t) or f'"{a["name"]}" OR {t}'
        try:
            for h in gnews(q, 7)[:15]:
                items.append({**h, "id": "gn-" + hid(h["title"].lower()), "kind": "manchete", "tickers": [t]})
        except Exception as e:
            errors.append(f"gnews {t}: {e}")
        time.sleep(1.0)
    for tema, q in MACRO_Q.items():
        try:
            for h in gnews(q, 3)[:12]:
                items.append({**h, "id": "gn-" + hid(h["title"].lower()), "kind": "macro", "tickers": [], "tema": tema})
        except Exception as e:
            errors.append(f"gnews {tema}: {e}")
        time.sleep(1.0)
    return items


def merge_feed(old: list, new: list) -> list:
    cutoff = (pd.Timestamp.today().normalize() - pd.Timedelta(days=FEED_DAYS)).date().isoformat()
    by = {}
    for it in old + new:
        k = it["id"]
        if k in by:  # mesma manchete em vários ativos: une os tickers
            by[k]["tickers"] = sorted(set(by[k].get("tickers", [])) | set(it.get("tickers", [])))
            continue
        by[k] = dict(it)
    out = [v for v in by.values() if v.get("date", "") >= cutoff]
    return sorted(out, key=lambda x: (x.get("date", ""), x.get("ts", "")), reverse=True)


# ------------------------------------------------------------- macro ----
def sgs(code: int, start: pd.Timestamp, step_years=5) -> pd.Series:
    frames, cur, today = [], start, pd.Timestamp.today().normalize()
    while cur <= today:
        end = min(cur + pd.DateOffset(years=step_years), today)
        url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados?formato=json"
               f"&dataInicial={cur:%d/%m/%Y}&dataFinal={end:%d/%m/%Y}")
        frames.append(pd.DataFrame(get(url).json()))
        cur = end + pd.Timedelta(days=1)
    df = pd.concat(frames)
    df["data"] = pd.to_datetime(df["data"], format="%d/%m/%Y")
    return df.drop_duplicates("data").set_index("data")["valor"].astype(float).sort_index()


def on_dates(s: pd.Series, dates: list[str]) -> list:
    grid = pd.DatetimeIndex(pd.to_datetime(dates))
    s = s.dropna()
    if s.empty:
        return [None] * len(grid)
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    out = s.reindex(s.index.union(grid)).ffill().reindex(grid)
    out[out.index < s.index[0]] = float("nan")
    return [clean(v, 3) for v in out.tolist()]


def macro(dates: list[str], errors: list) -> dict:
    start = pd.Timestamp(dates[0]) - pd.Timedelta(days=40)
    series, labels = {}, {}
    for key, code, lab, step in (("selic", 432, "Selic meta (% a.a.)", 10), ("ipca12", 13522, "IPCA 12 meses (%)", 10),
                                 ("usd", 1, "Dólar PTAX (R$)", 5)):
        try:
            series[key] = on_dates(sgs(code, start, step), dates)
            labels[key] = lab
        except Exception as e:
            errors.append(f"sgs {code}: {e}")
    try:
        import yfinance as yf
        for key, sym, lab in (("brent", "BZ=F", "Petróleo Brent (US$)"), ("minerio", "TIO=F", "Minério de ferro 62% (US$/t)")):
            try:
                h = yf.Ticker(sym).history(start=start.date().isoformat(), interval="1d", auto_adjust=False)
                if h is not None and not h.empty:
                    series[key] = on_dates(h["Close"], dates)
                    labels[key] = lab
                else:
                    errors.append(f"yahoo {sym}: sem dados")
            except Exception as e:
                errors.append(f"yahoo {sym}: {e}")
    except Exception as e:
        errors.append(f"yfinance: {e}")
    return {"series": series, "labels": labels}


def agenda_yahoo(uni: dict, errors: list) -> list:
    out = []
    try:
        import yfinance as yf
    except Exception as e:
        errors.append(f"yfinance: {e}")
        return out
    today = pd.Timestamp.today().normalize()
    for a in uni["assets"]:
        t = a["ticker"]
        try:
            cal = yf.Ticker(f"{t}.SA").calendar or {}
        except Exception:
            continue
        if not isinstance(cal, dict):
            continue
        ed = cal.get("Earnings Date")
        eds = ed if isinstance(ed, (list, tuple)) else [ed] if ed else []
        for d in eds[:1]:
            d = pd.Timestamp(d)
            if d >= today - pd.Timedelta(days=3):
                out.append({"date": d.date().isoformat(), "ticker": t, "kind": "resultado",
                            "title": "Divulgação de resultado (estimada)"})
        for k, lab in (("Ex-Dividend Date", "Data ex de provento"), ("Dividend Date", "Pagamento de provento")):
            d = cal.get(k)
            if d:
                d = pd.Timestamp(d)
                if d >= today - pd.Timedelta(days=3):
                    out.append({"date": d.date().isoformat(), "ticker": t, "kind": "provento", "title": lab})
        time.sleep(0.3)
    return out


# --------------------------------------------------------- inflexões ----
def zigzag(dates: list[str], vals: list, th: float) -> list:
    """Topos e fundos alternados com variação mínima th entre eles (série semanal)."""
    pts = [(d, v) for d, v in zip(dates, vals) if v]
    if len(pts) < 10:
        return []
    piv, trend = [], 0
    hi = lo = pts[0]
    for d, v in pts[1:]:
        if trend >= 0:
            if v > hi[1]:
                hi = (d, v)
            if v <= hi[1] * (1 - th):
                if trend == 1 or not piv:
                    piv.append(("topo", *hi))
                trend, lo = -1, (d, v)
        if trend <= 0:
            if v < lo[1]:
                lo = (d, v)
            if v >= lo[1] * (1 + th):
                if trend == -1 or not piv:
                    piv.append(("fundo", *lo))
                trend, hi = 1, (d, v)
    out, prev = [], None
    for tp, d, v in piv:
        out.append({"date": d, "type": tp, "price": clean(v, 2),
                    "move": clean(v / prev - 1, 3) if prev else None})
        prev = v
    if prev and pts[-1][0] != out[-1]["date"]:  # perna em curso, ainda sem confirmação
        d, v = pts[-1]
        out.append({"date": d, "type": "atual", "price": clean(v, 2), "move": clean(v / prev - 1, 3)})
    return out


def inflections(market: dict) -> dict:
    dates, out = market["dates"], {}
    for t, a in market.get("assets", {}).items():
        if a.get("close"):
            z = zigzag(dates, a["close"], ZZ_ASSET)
            if z:
                out[t] = z
    ib = (market.get("benchmarks") or {}).get("IBOV")
    if ib:
        out["IBOV"] = zigzag(dates, ib, ZZ_INDEX)
    return out


# ------------------------------------------------------------- build ----
def build():
    raw = json.loads(RAW.read_text(encoding="utf-8")) if RAW.exists() else {}
    cur = json.loads(CUR.read_text(encoding="utf-8")) if CUR.exists() else {}
    tl = json.loads((ROOT / "timeline.json").read_text(encoding="utf-8"))
    extra = cur.get("timeline_extra") or []
    events = sorted(tl["events"] + extra, key=lambda e: e["date"])
    ag = list(raw.get("agenda") or [])
    today = datetime.now(timezone.utc).date().isoformat()
    for y, ds in (tl.get("copom") or {}).items():
        for d in ds:
            if d >= today:
                ag.append({"date": d, "ticker": None, "kind": "copom", "title": "Decisão do Copom (Selic)"})
    ag = sorted({(a["date"], a.get("ticker"), a["kind"], a["title"]): a for a in ag}.values(), key=lambda a: a["date"])
    payload = {
        "generated_at": raw.get("generated_at"), "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feed": raw.get("feed") or [], "cvm_proventos": raw.get("cvm_proventos") or {}, "cvm_map": raw.get("cvm_map") or {},
        "macro": raw.get("macro") or {}, "macro_dates": raw.get("macro_dates") or [], "agenda": ag,
        "inflections": raw.get("inflections") or {}, "timeline": events, "episodios": tl.get("episodios") or [],
        "curated": {k: cur.get(k) for k in ("items", "inflections", "suggestions", "summary", "updated_at")},
        "errors": raw.get("errors") or [], "sources": {
            "cvm": "CVM, dados abertos (IPE e FCA)", "manchetes": "Google Notícias (RSS), últimos 7 dias por ativo",
            "macro": "Banco Central (SGS 432, 13522, 1) e Yahoo Finance (BZ=F, TIO=F)",
            "agenda": "Yahoo Finance (estimativas) e calendário oficial do Copom",
            "curadoria": "tarefa diária do Claude (impacto, inflexões, sugestões de proventos)"},
    }
    js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    OUTJS.write_text("window.NBARSI_NEWS=" + js + ";\n", encoding="utf-8")
    log(f"news.js: {len(js)/1024:.0f} KB · feed {len(payload['feed'])} · agenda {len(ag)} · eventos {len(events)}")


def main():
    uni = json.loads((ROOT / "universe.json").read_text(encoding="utf-8"))
    market = json.loads((DATA / "market.json").read_text(encoding="utf-8"))
    old = json.loads(RAW.read_text(encoding="utf-8")) if RAW.exists() else {}
    errors = []
    year = pd.Timestamp.today().year
    tickers = [a["ticker"] for a in uni["assets"]]
    tmap = {}
    try:
        tmap = cvm_ticker_map(tickers, year)
        log(f"CVM: {len(tmap)}/{len(tickers)} tickers mapeados")
    except Exception as e:
        errors.append(f"cvm fca: {e}")
    cfeed, prov, cagenda = [], {}, []
    if tmap:
        try:
            cfeed, prov, cagenda = cvm_ipe(tmap, year, errors)
        except Exception as e:
            errors.append(f"cvm ipe: {e}")
    if not prov:
        prov = old.get("cvm_proventos") or {}
    hl = headlines(uni, errors)
    feed = merge_feed(old.get("feed") or [], cfeed + hl)
    raw = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feed": feed, "cvm_proventos": prov, "cvm_map": tmap or old.get("cvm_map") or {},
        "macro": macro(market["dates"], errors), "macro_dates": market["dates"],
        "agenda": cagenda + agenda_yahoo(uni, errors), "inflections": inflections(market), "errors": errors,
    }
    RAW.write_text(json.dumps(raw, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"feed {len(feed)} · proventos CVM {sum(len(v) for v in prov.values())} · erros {len(errors)}")
    for e in errors[:20]:
        log("  erro:", e)
    build()


if __name__ == "__main__":
    if "--build" in sys.argv:
        build()
    else:
        main()
