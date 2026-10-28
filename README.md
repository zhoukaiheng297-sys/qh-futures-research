# QH Futures — multi-factor research system for Chinese commodity futures

[中文说明](README.zh-CN.md)

An end-to-end research system for 9 Chinese commodity futures (SHFE copper, aluminium, zinc, nickel, rebar, hot-rolled coil; DCE iron ore, coke, coking coal). It collects real market data every trading day, builds 25 factors, makes walk-forward out-of-sample forecasts for the next 5 / 10 / 20 trading days, and attaches a **statistics-based reliability rating** to every signal. It runs unattended as a daily pipeline and a passcode-protected web app.

**The headline result is negative, and the system reports it as such:** under strict out-of-sample testing the multi-factor model has **no robust edge** (26 of 27 product × horizon AUCs are below 0.5). Every live signal is therefore rated *low confidence*. A pre-registered forward test of a reduced model started on 2026-10-28 and only uses data that did not exist when it was designed.

| Overview | Product page |
|---|---|
| ![Overview: signal-day prices, live quotes and 5/10/20-day signals](docs/screenshots/overview.jpg) | ![Product page: settlement-based change, live quote card, reliability reasons](docs/screenshots/product.jpg) |
| **Charts and factor contributions** | **Backtest and forward test** |
| ![Price and open interest chart, factor contribution table](docs/screenshots/charts.jpg) | ![Pre-registered forward test and cost-adjusted backtest vs baselines](docs/screenshots/backtest.jpg) |

*Screenshots are from 2026-10-27 data. The UI is in Chinese. Signals shown are research output, not investment advice.*

## What it does

- **Data**: 10 public sources, collected through [AKShare](https://github.com/akfamily/akshare) with retries, a fetch log, resumable backfill and freshness tracking. Sources: exchange dailies for every contract, top-20 broker positions, SHFE warehouse receipts, spot prices and basis, LME / SGX futures, USD/CNY, news flashes, contract rules and fees. Data runs from 2019-01 to today (about 190k contract-day bars).
- **Continuous contracts**: the main contract is chosen by the previous day's open interest, rolls forward only, and is ratio back-adjusted so that returns have no fake jumps at rolls.
- **25 scored factors** in 8 groups:
  - trend
  - term structure and basis
  - inventory
  - positioning
  - inter-market (domestic vs offshore, plus an active buy/sell proxy)
  - industry margins (steel and coking spreads)
  - volatility
  - behavioural finance (overreaction, disposition effect, anchoring, crowding)

  Every factor uses only information available at the 16:30 Beijing-time cutoff.
- **Model**: rolling Spearman-IC weighting.
  - Weights are re-estimated monthly from the last 3 years of *already-known* labels.
  - ICs are shrunk by effective sample size, then blended half product / half sector.
  - Scores are calibrated into probabilities with isotonic regression.
  - Gradient boosting and an expanding-window variant are kept as comparators.
- **Reliability rating** (deterministic rules). Each signal shows:
  - within-year AUC ± standard error
  - hit rate vs. always betting the majority direction
  - a check against the recent up-share
  - factor agreement
  - the hit rate of its calibration bucket
  - spaced nearest-neighbour historical analogs

  The rating is downgraded for major events (FOMC, PMI, CPI, NFP), contract rolls, high volatility, prices near the limit, thin volume, and stale or missing data.
- **Backtest**: non-overlapping holds with fees and one-tick slippage, compared with always-long, 20-day momentum and a coin-flip band.
- **Web app** (FastAPI + vanilla JS / SVG):
  - prices follow trading-software conventions (change vs. previous settlement; the session is labelled)
  - live quotes from Sina, shared 60-second cache, kept separate from the signal-day data
  - charts, factor tables, reliability reasons, data-health page
  - passcode login with HMAC cookies and a per-IP attempt limit
- **LLM features (optional)**, using the Anthropic API:
  - Claude web search builds per-product news digests with citations
  - a second, schema-constrained call extracts structured items
  - an explanation writer may only use numbers from the results
  - without an API key a template is used
  - covered by offline tests with mocked HTTP

## Results (2019-11 → 2026-10, out of sample)

| Check | Result |
|---|---|
| Within-year AUC, 9 products × 3 horizons | 26 of 27 below 0.5 (range 0.36–0.51); the best (nickel 5-day, 0.508) is within one standard error |
| Single-factor hit rates vs. chance, 564 product × horizon × factor tests | 0 significantly positive (z > 2), 9 significantly negative; about 13 on each side would be expected by luck alone |
| Warehouse-receipt factor removed (ablation, SHFE products) | mean ΔAUC 0.002, largest 0.056, all within one standard error |
| Cost-adjusted strategy | positive in most cases, but mostly from the 2019–2026 uptrend; usually worse than always-long |

**Why the AUCs are below 0.5**: factor directions in commodities flip between regimes. Momentum and rising open interest worked in 2019–2021, while most factors turned mean-reverting after 2022. Weights estimated on the past therefore often point the wrong way in the following month. Flipping the signal after seeing this would be hindsight, so it was not done.

### Pre-registered forward test

Basis, positioning and inter-market factors looked slightly better than chance in hindsight. That is exactly when a backtest is not evidence, so a **forward test** was set up on 2026-10-28:

- **Model B** uses only those 3 groups (10 factors); everything else is identical to the main model A.
- Both models' predictions are written daily to an **append-only** table. Later retraining cannot change a logged forecast.
- **Decision rule, fixed in advance**: after 250 signal days, switch to B only if its 5- and 10-day AUC beats A by more than two standard errors.

The factor list is locked by a unit test (`futures_analyst/forward.py`).

## Engineering

- **Daily pipeline**: probe sources → pull the raw DB → backfill gaps → retrain both models → export the site DB → publish → freshness check. It runs on GitHub Actions (about 6 minutes); an example workflow is in [`deploy/daily-update.yml`](deploy/daily-update.yml).
- **Freshness alert**: any product × dataset more than 3 trading days behind fails the job, so GitHub emails the owner. The threshold was calibrated on two years of history: it would have fired once, on a real 7-day gap in coke positioning data.
- **Deployment**: the batch job and the site are separate. The batch publishes a small read-only SQLite file to a private Hugging Face dataset; the Docker web app on Hugging Face Spaces pulls it at startup and every hour ([`deploy_hf.py`](deploy_hf.py), [`Dockerfile`](Dockerfile)).
- **Tests**: 19 offline tests, run in CI on every push. They cover:
  - no look-ahead: factors recomputed on data truncated at day t must match the full-data values
  - walk-forward training uses only known labels
  - rolls without fake jumps
  - reliability rules
  - settlement convention on roll days
  - freshness check
  - the append-only forward log
  - web authentication and rate limiting
  - live quote parsing
  - LLM calls through mocked HTTP
- **Stack**: Python 3.12, pandas, NumPy, scikit-learn, AKShare, SQLite (WAL), FastAPI, vanilla JS / SVG, GitHub Actions, Hugging Face Spaces / Datasets, Anthropic SDK.

## Run locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip
cp .env.example .env                               # optional: passcode, API keys
.venv/bin/python main.py probe                     # check which data sources are reachable
.venv/bin/python main.py init                      # backfill history (1–2 h first time, resumable)
.venv/bin/python main.py train                     # walk-forward training, backtest, export
.venv/bin/python main.py report CU                 # latest signal and reasons in the terminal
.venv/bin/python main.py backtest                  # backtest vs baselines
.venv/bin/python -m uvicorn web.app:app --port 7860
.venv/bin/python -m pytest tests -q
```

## Layout

```
futures_analyst/   data sources, collection, continuous contracts, factors, model, reliability,
                   backtest, forward test, freshness check, news/LLM, export
web/               FastAPI app, live quotes, static front end (vanilla JS, SVG charts)
tests/             offline tests
deploy/            example GitHub Actions workflow for the daily update
main.py            CLI: probe / init / update / train / backtest / report / publish / check
```

## Notes

- This is a personal research project, built with Claude Code as an AI pair programmer; commits are co-authored accordingly.
- No data is included in this repository. Data comes from public sources via AKShare and is subject to their terms. Real-time exchange data generally requires a licence to redistribute, so the deployed site is passcode-protected and shared with a few people only.
- Nothing here is investment advice. The wording on the site is deliberately "signal leans long/short + historical hit rate", never a trading instruction.
