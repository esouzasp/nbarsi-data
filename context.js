/*
 nBarsi — gerador do contexto cifrado para ChatGPT, Gemini e Claude.

 Roda o mesmo motor da página (app/index.html) em uma VM do Node, junta mercado, notícias,
 curadoria, carteiras e metodologia, gera um contexto em Markdown e em JSON e cifra os dois
 com AES-256-GCM. Só o arquivo cifrado vai para o repositório público.

 Uso:
   NBARSI_KEY=<chave base64url de 32 bytes> node context.js /tmp/nbarsi_in.json
   node context.js /tmp/nbarsi_in.json --plain /tmp/contexto.md   # gera sem cifrar (teste local)

 /tmp/nbarsi_in.json = {"portfolios": [...docs do banco], "transactions": [...], "methodology": {version, rules, note},
                        "overrides": {"items": {...}} }
 Sem dependências externas (só módulos nativos do Node 18+).
*/
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const crypto = require("crypto");

const ROOT = __dirname;
const args = process.argv.slice(2);
const inPath = args[0];
const plainIdx = args.indexOf("--plain");
if (!inPath) { console.error("uso: node context.js <entrada.json> [--plain saida.md]"); process.exit(2); }
const IN = JSON.parse(fs.readFileSync(inPath, "utf8"));

/* ---------- carrega o motor da página ---------- */
function loadEngine() {
  const html = fs.readFileSync(path.join(ROOT, "app", "index.html"), "utf8");
  const start = html.indexOf("<script>\nconst { useState");
  const end = html.lastIndexOf("</script>");
  if (start < 0 || end < start) throw new Error("script principal não encontrado em app/index.html");
  let code = html.slice(start + "<script>".length, end);
  code = code.replace(/ReactDOM\.createRoot\([\s\S]*$/, "");
  const market = JSON.parse(fs.readFileSync(path.join(ROOT, "data", "market.json"), "utf8"));
  const nj = fs.readFileSync(path.join(ROOT, "data", "news.js"), "utf8");
  const news = JSON.parse(nj.slice(nj.indexOf("=") + 1).trim().replace(/;\s*$/, ""));
  const noop = () => null;
  const sandbox = {
    console, Intl, Date, Math, JSON, Object, Array, Set, Map, Number, String, isFinite, parseFloat, parseInt, Promise,
    React: { useState: v => [typeof v === "function" ? v() : v, noop], useEffect: noop, useMemo: f => f(), useRef: () => ({}), useLayoutEffect: noop, createElement: noop, Fragment: "f" },
    htm: { bind: () => noop }, ReactDOM: { createRoot: () => ({ render: noop }) },
    document: { getElementById: noop }, location: { hash: "" }, history: { replaceState: noop }, navigator: {},
    localStorage: { getItem: noop, setItem: noop }, ResizeObserver: function () { this.observe = noop; this.disconnect = noop; },
  };
  sandbox.window = { NBARSI_MARKET: market, NBARSI_NEWS: news };
  sandbox.globalThis = sandbox;
  const exportNames = ["scoreAsset", "recommend", "applyOverrides", "rulesFrom", "DEFAULT_RULES", "assetExpectation", "portfolioExpectation",
    "runPortfolio", "targetViolations", "currentViolations", "portStats", "macro", "runRegimes", "REGIMES", "eventStudy", "relatedNews", "dateIdx",
    "basketRet", "seriesRet", "holdMap", "selicTrend", "ACTION_LABEL", "MIDWAY", "midwaySeries", "sectorKeyOf", "ESTATAIS", "CAT_LABEL", "nearEvents"];
  code += `\n;globalThis.__E = { ${exportNames.map(n => `${n}: typeof ${n} !== "undefined" ? ${n} : undefined`).join(", ")} };`;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: "app/index.html" });
  return { E: sandbox.__E, market, news };
}

/* ---------- formatação ---------- */
const pct = (v, d = 1) => (v == null || !isFinite(v)) ? "—" : (v * 100).toFixed(d).replace(".", ",") + "%";
const spct = (v, d = 1) => (v == null || !isFinite(v)) ? "—" : (v >= 0 ? "+" : "") + pct(v, d);
const num = (v, d = 2) => (v == null || !isFinite(v)) ? "—" : Number(v).toFixed(d).replace(".", ",");
const brl = v => (v == null || !isFinite(v)) ? "—" : "R$ " + Math.round(v).toLocaleString("pt-BR");
const ok = v => v != null && isFinite(v);
const fdate = iso => iso ? iso.slice(0, 10).split("-").reverse().join("/") : "—";
const row = cells => "| " + cells.map(c => String(c ?? "").replace(/\|/g, "/").replace(/\n/g, " ")).join(" | ") + " |";
const table = (head, rows) => [row(head), row(head.map(() => "---")), ...rows.map(row)].join("\n");

function build() {
  const { E, market, news } = loadEngine();
  const ver = IN.methodology || null;
  const rules = E.rulesFrom(ver).rules;
  const M = E.applyOverrides(market, IN.overrides || null) || market;
  const scored = {}; for (const [t, a] of Object.entries(M.assets)) scored[t] = E.scoreAsset(a, rules);
  const txs = IN.transactions || [];
  const ports = (IN.portfolios || []).slice().sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0)).map(p => ({
    ...p, id: p.id || p.name, name: p.name || p.id, targets: (p.targets || []).map(x => ({ ticker: x.ticker, target: +x.target || 0 })),
    txs: txs.filter(t => t.portfolio === (p.id || p.name)) }));
  const cur = news.curated || {};
  const today = new Date().toISOString().slice(0, 10);
  const L = [];
  const J = { gerado_em: new Date().toISOString(), dados_de_mercado: market.generated_at, metodologia: ver?.version ?? "padrão", carteiras: [], ativos: {}, noticias: [], agenda: [], resumo: cur.summary || null };

  L.push(`# nBarsi — contexto para análise (${fdate(today)})`, "",
    "Ferramenta pessoal de análise de ações brasileiras de dividendos (estratégia Barsi ajustada: retorno total acima do CDI, do IDIV e do Midway 103% CDI líquido de IR, com preservação de capital). Carteiras simuladas de R$ 100 mil, compradas no fechamento de 29/10/2026. Os sinais vêm de um motor determinístico; notícias e leituras foram classificadas por uma tarefa diária do Claude. Use como dados; confirme fatos importantes nas fontes.", "",
    `Dados de mercado: ${market.generated_at} · CDI ${num(market.cdi?.annual_now)}% a.a. · Selic meta ${num(market.cdi?.selic_meta)}% · metodologia v${ver?.version ?? "padrão"}`, "");
  if (cur.summary?.text) L.push("## Leitura do dia", "", `${cur.summary.text} (${fdate(cur.summary.date)})`, "");

  /* metodologia resumida */
  const R = rules.recomendacao || {};
  L.push("## Como o motor decide", "",
    `- Pontuação 0–100 = ${pct(rules.pesos_score?.dividendos, 0)} dividendos + ${pct(rules.pesos_score?.qualidade, 0)} qualidade + ${pct(rules.pesos_score?.valuation, 0)} valuation, menos penalidade cíclica (commodity −15, construção −10).`,
    `- Preço-teto = média de proventos recorrentes por ação de 5 anos ÷ ${pct(rules.preco_teto?.dy_alvo, 0)}; proventos acima de ${num(rules.proventos?.fator_extraordinario)} × a mediana contam como extraordinários e são limitados.`,
    `- Sinal em três estágios: elegibilidade → atratividade (pontuação ≥ ${R.score_compra}, valuation aplicável, margem ≥ 0, menos de 2 cortes de proventos em 5 anos) → alocação (abaixo do peso-alvo e setor abaixo de ${pct(rules.limites?.max_peso_setor, 0)}).`,
    "- Sinais: comprar (aportar), manter (ficar, sem aportar), aguardar (caro: não aportar), revisar (reavaliar a tese), analisar (sem preço-teto aplicável), atrativo (bom fora de uma carteira).",
    `- Projeção: retorno esperado = DY normalizado + crescimento; cenários −${pct(rules.projecao?.spread_pessimista, 0)} / +${pct(rules.projecao?.spread_otimista, 0)} a.a.`, "");

  /* carteiras */
  L.push("## Carteiras", "");
  const mid = (() => { const r = market.cdi?.index; return r ? E.midwaySeries(r, market.dates) : null; })();
  for (const p of ports) {
    const st = E.portStats(p, M, scored);
    const run = p.txs.length ? E.runPortfolio(p.txs, M) : null;
    const ret = run ? run.cota[run.cota.length - 1] / 100 - 1 : null;
    const exp = E.portfolioExpectation(p.targets, M, rules);
    const vio = E.targetViolations(p.targets, M, rules).map(v => v.t);
    const hold = p.targets.map(t => { const a = M.assets[t.ticker] || {}; const sc = scored[t.ticker]; const value = (st.qty[t.ticker] || 0) * (a.price || 0); const weight = st.mv ? value / st.mv : 0; const target = t.target / st.tsum;
      const rec = E.recommend(sc, { weight, target, asset: a, groupWeight: st.mv ? st.sec[E.macro(a.sector)] : null }, rules); return { t: t.ticker, a, sc, weight, target, rec }; });
    L.push(`### ${p.name}`, "", p.description ? p.description : "", "",
      `Valor ${brl(st.mv)} · retorno desde a compra ${spct(ret)} · DY da carteira ${pct(st.dyTarget)} · pontuação média ${num(st.score, 1)} · retorno esperado ${pct(exp?.r)} a.a. · maior grupo ${st.top[0]} ${pct(st.top[1], 0)}${vio.length ? " · restrições: " + vio.join(" ") : ""}`, "",
      table(["Ativo", "Peso-alvo", "Peso atual", "Sinal na carteira", "Motivo"], hold.map(h => [h.t, pct(h.target), pct(h.weight), E.ACTION_LABEL[h.rec.action] || h.rec.action, h.rec.why])), "");
    if (p.rationale?.length) L.push("Racional: " + p.rationale.join(" "), "");
    J.carteiras.push({ nome: p.name, descricao: p.description, racional: p.rationale, valor: st.mv, retorno_desde_compra: ret, dy: st.dyTarget, pontuacao: st.score, retorno_esperado: exp?.r, restricoes: vio,
      ativos: hold.map(h => ({ ticker: h.t, peso_alvo: h.target, peso_atual: h.weight, sinal: h.rec.action, motivo: h.rec.why })) });
  }

  /* ativos das carteiras + candidatos */
  const held = [...new Set(ports.flatMap(p => p.targets.map(t => t.ticker)))];
  const cands = Object.entries(scored).filter(([t, s]) => !held.includes(t) && s.status === "elegivel" && ok(s.score)).sort((a, b) => b[1].score - a[1].score).slice(0, 10).map(([t]) => t);
  const assetRow = t => { const a = M.assets[t] || {}; const s = scored[t]; const rec = E.recommend(s, null, rules); const e = E.assetExpectation(a, rules);
    return { t, a, s, rec, e, cells: [t, a.name, a.sector, ok(s?.score) ? Math.round(s.score) : "—", E.ACTION_LABEL[rec.action] || rec.action, num(a.price), pct(s?.adj ? s.adj.dy12 : a.dy_12m), s?.usarTeto ? spct(s.margin, 0) : "n/a", num(a.pl, 1), pct(a.roe, 0), num(a.div_liq_ebitda, 1), pct(e?.r)] }; };
  const head = ["Ativo", "Nome", "Setor", "Pontuação", "Sinal", "Cotação", "DY recorrente", "Margem p/ teto", "P/L", "ROE", "DL/EBITDA", "Retorno esperado"];
  L.push("## Ativos das carteiras", "", table(head, held.map(t => assetRow(t).cells)), "");
  L.push("## Melhores candidatos fora das carteiras", "", table(head, cands.map(t => assetRow(t).cells)), "");

  /* fatos por ativo */
  L.push("## Fatos por ativo das carteiras", "");
  for (const t of held) {
    const r = assetRow(t); const ca = cur.assets?.[t];
    const study = E.eventStudy(t, M).slice(0, 5);
    const piv = (news.inflections?.[t] || []).slice(-4);
    const rel = E.relatedNews(t, M).slice(0, 5);
    L.push(`### ${t} — ${r.a.name}`, "", `Sinal ${E.ACTION_LABEL[r.rec.action] || r.rec.action}: ${r.rec.why}`);
    if (ca?.leitura) L.push("", `Leitura: ${ca.leitura}`);
    if (ca?.cenarios) L.push("", `Cenários: base — ${ca.cenarios.base || "—"}; otimista — ${ca.cenarios.otimista || "—"}; pessimista — ${ca.cenarios.pessimista || "—"}`);
    if (ca?.gatilhos?.length) L.push("", `Gatilhos de revisão: ${ca.gatilhos.join("; ")}`);
    if (piv.length) L.push("", "Inflexões recentes: " + piv.map(p => `${fdate(p.date)} ${p.type} ${spct(p.move, 0)}${cur.inflections?.[t + "|" + p.date]?.why ? " (" + cur.inflections[t + "|" + p.date].why + ")" : ""}`).join("; "));
    if (study.length) L.push("", "Reação histórica a tipos de fato (4 semanas, vs Ibovespa): " + study.map(s => `${E.CAT_LABEL[s.k] || s.k} ${spct(s.ex, 1)} (n=${s.n})`).join("; "));
    if (rel.length) L.push("", "Notícias: " + rel.map(({ it, c }) => `${fdate(it.date)} ${it.title || it.assunto}${c ? ` [${c.dir}${c.level ? "/" + c.level : ""}${c.why ? ": " + c.why : ""}]` : ""}`).join(" · "));
    L.push("");
    J.ativos[t] = { nome: r.a.name, setor: r.a.sector, pontuacao: r.s?.score, sinal: r.rec.action, motivo: r.rec.why, cotacao: r.a.price, dy_recorrente: r.s?.adj ? r.s.adj.dy12 : r.a.dy_12m, margem_teto: r.s?.margin, preco_teto: r.s?.teto, retorno_esperado: r.e?.r, leitura: ca || null,
      inflexoes: piv, reacao_fatos: study.map(s => ({ tipo: s.k, n: s.n, media: s.mean, vs_ibov: s.ex })), noticias: rel.map(({ it, c }) => ({ data: it.date, titulo: it.title || it.assunto, url: it.url, impacto: c || null })) };
  }

  /* notícias classificadas recentes */
  const since = new Date(Date.now() - 14 * 864e5).toISOString().slice(0, 10);
  const items = (news.feed || []).filter(it => it.date >= since && cur.items?.[it.id] && !cur.items[it.id].irrelevante && (cur.items[it.id].level >= 2 || (it.tickers || []).some(t => held.includes(t))))
    .slice(0, 60);
  if (items.length) {
    L.push("## Notícias classificadas, últimos 14 dias", "", table(["Data", "Notícia", "Ativos", "Impacto", "Por quê"], items.map(it => { const c = cur.items[it.id];
      return [fdate(it.date), (it.title || it.assunto || "").slice(0, 140), [...new Set([...(it.tickers || []), ...(c.tickers || [])])].join(" "), `${c.dir} ${c.level} ${c.scope || ""}`, c.why || ""]; })), "");
    J.noticias = items.map(it => ({ data: it.date, titulo: it.title || it.assunto, url: it.url, tickers: it.tickers, impacto: cur.items[it.id] }));
  }

  /* agenda, macro, regime, episódios */
  const ag = (news.agenda || []).filter(a => a.date >= today && a.date <= new Date(Date.now() + 45 * 864e5).toISOString().slice(0, 10) && (!a.ticker || held.includes(a.ticker)));
  if (ag.length) { L.push("## Agenda (45 dias)", "", ag.map(a => `- ${fdate(a.date)} ${a.ticker || ""} ${a.title}`).join("\n"), ""); J.agenda = ag; }
  const ms = news.macro?.series || {}; const last = arr => { if (!arr) return null; for (let i = arr.length - 1; i >= 0; i--) if (ok(arr[i])) return arr[i]; return null; };
  L.push("## Macro", "", `Selic ${num(last(ms.selic))}% · IPCA 12m ${num(last(ms.ipca12), 1)}% · dólar R$ ${num(last(ms.usd))} · Brent US$ ${num(last(ms.brent), 0)} · minério US$ ${num(last(ms.minerio), 0)}`, "");
  try {
    const rg = E.runRegimes({ M, rules, ports, overrides: IN.overrides || null });
    L.push("## Teste de regime (sensibilidade à janela de proventos)", "", table(["Carteira", ...E.REGIMES.map(r => r.nome)], rg.port.map(x => [x.p.name, ...E.REGIMES.map(r => `${num(x.v[r.id].score, 1)} · ${pct(x.v[r.id].atr, 0)} em atrativo`)])), "");
  } catch (e) { /* opcional */ }
  const eps = (news.episodios || []).map(e => ({ ...e, i0: E.dateIdx(market.dates, e.ini), i1: E.dateIdx(market.dates, e.fim) })).filter(e => e.i0 >= 0 && e.i1 > e.i0);
  if (eps.length) L.push("## Carteiras em episódios históricos (variação de preço da cesta atual)", "", table(["Episódio", ...ports.map(p => p.name), "Ibovespa"], eps.map(e => [e.nome, ...ports.map(p => { const x = E.basketRet(p, M, e.i0, e.i1); return x ? spct(x.r, 0) : "—"; }), spct(E.seriesRet(market.benchmarks?.IBOV, e.i0, e.i1), 0)])), "");
  L.push("---", "Fontes: Yahoo Finance (não oficial), Banco Central (SGS), CVM dados abertos, Google Notícias; curadoria pela tarefa diária do Claude. Ferramenta pessoal, não é recomendação de investimento.");
  return { md: L.filter(x => x !== undefined).join("\n"), json: J };
}

function encrypt(text, keyB64) {
  const key = Buffer.from(keyB64, "base64url");
  if (key.length !== 32) throw new Error("NBARSI_KEY precisa ter 32 bytes em base64url");
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv("aes-256-gcm", key, iv);
  const ct = Buffer.concat([c.update(text, "utf8"), c.final(), c.getAuthTag()]);
  return { v: 1, alg: "AES-256-GCM", iv: iv.toString("base64"), ct: ct.toString("base64"), gerado_em: new Date().toISOString() };
}

const out = build();
if (plainIdx >= 0) {
  fs.writeFileSync(args[plainIdx + 1], out.md);
  fs.writeFileSync(args[plainIdx + 1].replace(/\.md$/, "") + ".json", JSON.stringify(out.json, null, 1));
  console.log(`contexto em claro: ${(out.md.length / 1024).toFixed(0)} KB de markdown`);
} else {
  const key = process.env.NBARSI_KEY;
  if (!key) { console.error("defina NBARSI_KEY"); process.exit(2); }
  const bundle = JSON.stringify({ md: out.md, json: out.json });
  fs.writeFileSync(path.join(ROOT, "data", "contexto.enc.json"), JSON.stringify(encrypt(bundle, key)));
  console.log(`data/contexto.enc.json gravado (${(bundle.length / 1024).toFixed(0)} KB antes de cifrar)`);
}
