# nbarsi-data

Coletor diário de dados de mercado do **nBarsi**, o agente pessoal de análise de ações de dividendos.

## Como funciona

1. **GitHub Actions** (`.github/workflows/collect.yml`) roda `collect.py` de segunda a sexta às 18:30 (Brasília).
2. `collect.py` coleta cotações, proventos e indicadores de ~50 empresas (`universe.json`, setores BEST + IDIV) via yfinance, e o CDI pela API SGS do Banco Central.
3. O resultado vai para `data/market.json` e `data/market.js` e é commitado no repositório.
4. Uma tarefa agendada do Claude lê `data/market.js` e publica o arquivo no artefato nBarsi, que calcula pontuações, gráficos e recomendações.

## Rodar localmente

```bash
pip install -r requirements.txt
python collect.py                 # universo completo
python collect.py ITUB4 TAEE11    # teste rápido
```

## Editar o universo

`universe.json` lista ticker, nome, setor, perfil de análise (`banco`, `seguradora`, `utility`, `saneamento`, `telecom`, `industria`, `commodity`, `construcao`, `consumo`, `holding`, `outro`) e grupo (`BEST`/`IDIV`). `aliases` são tickers antigos tentados se o principal não tiver dados.

## Limitações conhecidas

- yfinance não é fonte oficial. Proventos vêm por data-com e podem misturar dividendos e JCP bruto.
- Indicadores (ROE, P/L, P/VP, payout, dívida) são os do Yahoo Finance; confirme os relevantes no RI da empresa.
- O IDIV não está disponível no Yahoo; o DIVO11 é usado como aproximação.
- Próximo passo natural: fundamentos pela base de dados abertos da CVM (DFP/ITR), com data de entrega para backtest sem viés.
