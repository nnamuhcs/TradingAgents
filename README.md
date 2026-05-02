# TradingAgents on K8s

Multi-agent LLM trading framework with **three** ways to run: a live terminal dashboard (TUI), a non-interactive batch script, and a **k8s-native WebUI** with charts and run history. Forked from [TauricResearch/TradingAgents](https://github.com/tauricresearch/tradingagents) with added **GitHub Copilot provider support**, **K8s manifests**, **Market Scanner** integration, and env-driven config.

## What It Does

A multi-agent AI trading system that mirrors a real trading firm:

- **Fundamentals Analyst** — company financials, balance sheet, cash flow
- **Market / Technical Analyst** — MACD, RSI, SMA, EMA, ATR
- **Social / Sentiment Analyst** — social media and market mood
- **News Analyst** — global news and macro events
- **Bull / Bear Researchers** — structured debate on the analysis
- **Trader** — drafts the trading plan
- **Risk Management (Aggressive / Neutral / Conservative)** — debates portfolio risk
- **Portfolio Manager** — final call with position sizing

All agents powered by LLMs (Claude Opus 4.7, GPT-5.4, Gemini, Grok, etc.) via your existing **GitHub Copilot subscription** — no extra API costs.

---

## Three ways to run

There are three complementary ways to use this framework. The K8s manifests support all three side-by-side in the same namespace.

### 🌐 WebUI (browser, k8s-native) — recommended for daily use

Best for: any browser-equipped device, multi-user, run history, charts.

- Single-page web app (FastAPI + SSE + Plotly), Postgres-backed run history
- Form-driven: pick ticker(s) **or** ask the Market Scanner to pick the top 5/10/20
- Live event stream (Server-Sent Events) shows agents working in real time
- 4-pane Plotly chart per result (candlestick + volume + RSI + MACD)
- Multi-symbol queue: enter `NVDA, AAPL, MSFT` to analyze them sequentially

### 🎛️ Interactive TUI (live Rich dashboard)

Best for: terminal lovers, demos in screen-share, full live-updating Rich layout.

- Walks you through ticker, date, language, analysts, depth, and effort with arrow-key prompts
- Renders a **live Rich dashboard** — analyst statuses, message stream, tool calls, current report
- Step 1 also offers the **Market Scanner branch** (top 5/10/20 + Pick ONE / Run ALL / Pick MULTIPLE)

### ⚙️ Non-interactive (script / CronJob)

Best for: scheduled batch runs, CI/CD, headless servers.

- Reads everything from environment variables (ConfigMap + Secret)
- Writes a JSON file per run with the decision and timestamp
- Runs as a one-shot K8s `Job` or a daily `CronJob` (`9:30 AM ET, Mon–Fri`)
- Includes a separate **Market Scanner** entry point (`main_scanner.py`) for batch scanning

---

## Quick Start — Local (no K8s)

```bash
git clone https://github.com/nnamuhcs/TradingAgents.git
cd TradingAgents

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Auth: any GitHub PAT with Copilot access (or `gh auth token`)
echo "GITHUB_TOKEN=$(gh auth token)" > .env
```

### TUI (interactive)

```bash
# Pre-fill provider+models so the TUI auto-skips Steps 6–7
LLM_PROVIDER=github-copilot \
DEEP_THINK_LLM=claude-opus-4.7 \
QUICK_THINK_LLM=claude-opus-4.7 \
tradingagents
```

### Non-interactive (script)

```bash
python main_copilot.py NVDA 2025-05-01
# multi-symbol:
python main_copilot.py AAPL,MSFT,GOOG 2025-05-01
```

### Market Scanner (AI Stock Discovery)

The scanner automatically finds the best stocks to analyze using a 4-layer AI pipeline:

1. **Quant Screening** — Scores all S&P 500 stocks on relative strength, volume breakouts, price breakouts, and momentum (RSI/MACD). Narrows ~500 to top 30.
2. **Event-Driven** — Boosts stocks with upcoming earnings, analyst upgrades, and news catalysts.
3. **Smart Money** — Checks insider buying/selling and institutional holder activity.
4. **LLM Synthesis** — Claude analyzes all 30 candidates with full signal data, picks 5–10 with conviction levels (high/medium/low) and per-stock reasoning.

```bash
# Full pipeline: scan -> pick stocks -> run full analysis on each
python main_scanner.py

# Scan only (just get stock picks, no deep analysis)
python main_scanner.py --scan-only

# Limit how many picks get full analysis
python main_scanner.py --max-picks 3

# Via env vars (for K8s)
SCANNER_MODE=scan-only python main_scanner.py
SCANNER_MAX_PICKS=5 python main_scanner.py
```

Example output:

```
1. *** AVGO   [HIGH]   - AI/semis leader, +34% 1m, RSI 70, institutions accumulating
2. *** GOOGL  [HIGH]   - Mega-cap breakout, AI/Gemini momentum, +20% 1m
3. **  AMD    [MEDIUM] - Earnings catalyst, +66% 1m, AI accelerator demand
...
```

---

## Deploy to Kubernetes

### Prerequisites

- A K8s cluster (Kind, Minikube, **k3s/k3d**, EKS, GKE, AKS — Docker Desktop K8s also works)
- `kubectl` configured
- Docker (for building the images)

### One-Command Deploy (TUI + batch only)

```bash
./k8s/deploy.sh --build --token "$(gh auth token)"
```

This builds the worker image, creates the namespace, secret, configmap, PVC, CronJob, **and a long-lived `ta-shell` pod** for running the interactive TUI inside the cluster.

### Add the WebUI

```bash
# Build the WebUI image
docker build -f Dockerfile.webui -t tradingagents-webui:latest .

# Distribute it like the worker image (see table below)

# Apply Postgres + WebUI
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/webui-deployment.yaml
# Optional: expose via Ingress (edit host first)
# kubectl apply -f k8s/webui-ingress.yaml

# Open it
kubectl -n tradingagents port-forward svc/tradingagents-webui 8000:80
# -> browse to http://localhost:8000
```

### Image distribution by cluster type

| Cluster | After `docker build -t IMAGE:latest .` |
|---|---|
| **Docker Desktop K8s** | Nothing — image is auto-visible to the cluster |
| **Kind** | `kind load docker-image IMAGE:latest --name <cluster>` |
| **Minikube** | `minikube image load IMAGE:latest` |
| **k3s / k3d** | `docker save IMAGE:latest -o /tmp/img.tar && sudo k3s ctr images import /tmp/img.tar` |
| **Cloud (EKS/GKE/AKS)** | `docker tag` + `docker push` to your registry, then update `image:` in the manifests |

---

## ▶ Running on K8s — WebUI mode (browser, recommended)

```bash
kubectl -n tradingagents port-forward svc/tradingagents-webui 8000:80
```

Then open **http://localhost:8000** in any browser.

### What you can do in the browser

The single-page app has three tabs and a few persistent UI elements:

#### Top — scrolling market ticker (always visible)
A Bloomberg/CNBC-style horizontal marquee fed by Yahoo's WebSocket (real-time when the WS is healthy, falls back to a 60s yfinance poll otherwise). The bar combines four sources:

- **Anchors** (always shown) — SPY, QQQ, DIA, IWM, ^VIX, ^IXIC/^GSPC/^DJI, GLD/SLV/USO/TLT/HYG, EURUSD=X, USDJPY=X, BTC-USD, ETH-USD
- **Pinned** — your custom watchlist (click `⚙ Watchlist` in the nav, add tickers like `AVGO` or `^VIX`, persisted in browser localStorage)
- **Recent** — symbols from your last 30 days of runs, auto-pulled from Postgres
- **Top movers** — top gainers (green) + top losers (red) from S&P 500 + bellwethers, recomputed every minute

Hover the marquee → it pauses so you can read a value. Status bar at the bottom shows **● LIVE** (green) when WS is healthy, **○ POLL 60s** (yellow) on fallback.

#### New Run tab
Form fields:

- **Ticker source**: *manual* (type comma-separated symbols, up to 5), *Scanner: top 3 picks*, or *Scanner: top 5 picks*
- **Symbols**: comma-separated; warns yellow if you type more than 5
- **Analysis date**: defaults to today
- **Analysts**: 4 checkboxes (Market / Social / News / Fundamentals)
- **Research depth**: *Shallow / Medium / Deep* (1 / 3 / 5 debate rounds — same as the TUI)
- **Language**: 13 options (English, 简体中文, 繁體中文, 日本語, 한국어, Español, Français, Deutsch, Português, Italiano, Русский, العربية, हिन्दी)
- **Advanced**: provider, deep model, quick model, anthropic effort

Click **Start Analysis**. The Live Output panel comes alive:

1. **Symbol tabs** appear once the runner knows what symbols to analyze (immediate for manual, after the scanner finishes for scan-N). Each tab has a coloured dot: yellow pulsing for in_progress, green for done. Auto-flips to the current symbol; you can click any tab to inspect it manually.

2. **Scanner Funnel** — only when you used a scanner ticker source. A 4-row visualization fills in live as the scanner progresses through its layers:
   ```
   L1  Quant Screening  ████████████████████  503 → 30   Top 30 score range 12.3–8.1
   L2  Event Catalysts  ████                   30 → 14   14 of 30 have catalysts
   L3  Smart Money      ████                   30 →  9   9 of 30 flagged
   L4  LLM Synthesis    ████                   30 →  5   Selected 5 with conviction
   ```

3. **Three-pane TUI mirror**:
   - **Left — Agents**: 12-row checklist grouped by stage (Analysts / Research / Trader / Risk Mgmt). Each agent has a coloured dot and matching text colour.
   - **Middle — Messages & Tools**: live stream with type-coloured borders (Agent=teal, Data=azure, User=magenta, Tool=yellow, System=grey). Auto-scrolls.
   - **Right — Current Report**: tabbed Markdown rendering of the 7 report sections (market / sentiment / news / fundamentals / investment_plan / trader_plan / final_decision). Auto-switches to the latest section. A `✓` next to the tab label shows which sections have content.

4. **Decisions** — when each symbol completes its decision card appears with three buttons: **📄 Full Report** (modal viewer), **⬇ .md** (download as markdown), **📊 Chart** (4-pane Plotly: candlestick + volume + RSI + MACD).

#### History tab
Every past run with timestamp, symbols, status badge, and per-symbol decisions. Per-row buttons:
- **View** — load this run back into the New Run dashboard (replays the per-symbol tabs with their saved reports — no LLM call)
- **📄 SYMBOL** — open the Full Report modal for that symbol directly
- **Cancel** — appears next to running/pending rows; marks them cancelled (background reaper auto-fails any run that's >30 min old in pending/running state)

#### Scanner tab
Standalone Market Scanner (no analysis run). Pick 1–20, click **Scan**. Same 4-row funnel visualization as the New Run page fills in live as the scanner progresses through its layers, then the picks table renders with market regime + themes + per-pick conviction & reasoning. Runs over SSE (`/api/scan/stream`) so you see progress instead of a blank page for 60–90s.

### Full Report viewer & download

Every completed run persists each section's markdown to Postgres (`runs.reports JSONB`). The **📄 Full Report** button opens a modal with:
- The assembled report: metadata header → Final Decision → 7 numbered sections with all sub-headings
- Toolbar: **⬇ Download .md**, **📋 Copy** (clipboard), **🖨 Print** (opens a print-styled tab), **✕ Close**

Or call the endpoint directly:
```bash
curl http://localhost:8000/api/runs/RUN_ID/report.md?symbol=AAPL > AAPL_report.md
```

### REST API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/runs` | Start a run; body has `ticker_source` / `symbols` / `analysis_date` / `analysts` / etc. |
| `GET`  | `/api/runs/{id}` | Run metadata + decisions + reports |
| `GET`  | `/api/runs/{id}/events` | **SSE** live event stream (`agent_status`, `message`, `tool_call`, `report_section`, `scanner_layer`, `symbol_start/done`, …) |
| `GET`  | `/api/runs?limit=N` | List past runs (newest first; auto-reaps stale runs) |
| `POST` | `/api/runs/{id}/cancel` | Mark a run as cancelled |
| `GET`  | `/api/runs/{id}/report?symbol=X` | Assembled markdown JSON for one or all symbols |
| `GET`  | `/api/runs/{id}/report.md?symbol=X` | Same as a downloadable .md file |
| `GET`  | `/api/scan?n=10` | Run the Market Scanner (blocking), return picks |
| `GET`  | `/api/scan/stream?n=10` | **SSE** scanner stream: `scanner_layer` events per layer + final `picks` event |
| `GET`  | `/api/chart/{symbol}?period=6mo` | OHLCV + RSI + MACD JSON for Plotly |
| `GET`  | `/api/movers/stream?pinned=…` | **SSE** marquee feed (anchors + pinned + recent + top movers, live-overlaid by Yahoo WS) |
| `GET`  | `/healthz` | Liveness probe |

### Why a separate WebUI image?

The WebUI needs `fastapi`, `uvicorn`, `sqlalchemy`, `asyncpg`, `websockets` which aren't required for the TUI / batch worker. The `[webui]` extra in `pyproject.toml` keeps these out of the lean worker image; the WebUI uses `Dockerfile.webui` which installs `pip install '.[webui]'`.

---

## ▶ Running on K8s — TUI mode (live Rich dashboard)

A long-lived `ta-shell` pod is deployed for you. Just exec into it:

```bash
kubectl -n tradingagents exec -it ta-shell -- tradingagents
```

You'll be prompted for the things that benefit from interaction (ticker, date, language, analysts, depth, anthropic effort) but provider and models are auto-filled from the ConfigMap so you don't navigate a menu that doesn't even include `github-copilot`.

Convenience aliases (add to your `~/.bashrc`):

```bash
alias ta='kubectl -n tradingagents exec -it ta-shell -- tradingagents'
alias tash='kubectl -n tradingagents exec -it ta-shell -- bash'
ta-run() { kubectl -n tradingagents exec -it ta-shell -- python main_copilot.py "$@"; }
```

Then `ta` launches the TUI from anywhere.

### Why this mode requires a long-lived pod

K8s `Job`s exit when their command finishes — but the TUI is interactive and stays alive until you complete the analysis. The `ta-shell` pod runs `sleep infinity`, so you can `kubectl exec -it` into it as many times as you like, share the same PVC for results, and it survives across analyses. See `k8s/pod-shell.yaml`.

### Skip extra prompts (zero-question TUI)

Any of these env vars (set in the ConfigMap or via `kubectl exec -- env VAR=...`) skip the corresponding prompt:

| Env var | Skips |
|---|---|
| `TA_TICKER` | Step 1 (ticker) |
| `TA_TICKER_SOURCE` | Step 1 source choice (`manual`, `scan-5`, `scan-10`, `scan-20`) |
| `TA_DATE` | Step 2 (analysis date) |
| `TA_LANGUAGE` | Step 3 (output language) |
| `TA_ANALYSTS` | Step 4 (analysts; csv: `market,social,news,fundamentals`) |
| `TA_RESEARCH_DEPTH` | Step 5 (Bull/Bear debate rounds) |
| `LLM_PROVIDER` | Step 6 (provider) |
| `QUICK_THINK_LLM` + `DEEP_THINK_LLM` | Step 7 (models) |
| `TA_ANTHROPIC_EFFORT` / `TA_OPENAI_REASONING_EFFORT` / `TA_GOOGLE_THINKING_LEVEL` | Step 8 (provider-specific effort) |

Set every one and the TUI prompts for nothing — but you still get the full live Rich dashboard.

---

## ▶ Running on K8s — non-interactive mode (scripted)

### One-off Job

```bash
# Edit symbols/date/etc.
kubectl -n tradingagents edit configmap tradingagents-config

# Launch
kubectl -n tradingagents apply -f k8s/job-manual.yaml
kubectl -n tradingagents logs -f job/tradingagents-manual
```

### Scheduled CronJob

Auto-runs daily at **9:30 AM ET, Mon–Fri** (market open):

```bash
kubectl -n tradingagents get cronjob       # status
kubectl -n tradingagents get jobs          # history
```

Edit `k8s/cronjob.yaml` to change the schedule.

### Output

Results are written to the PVC at `/home/appuser/.tradingagents/logs/results_<date>_<time>.json`:

```json
{
  "GOOG": {
    "decision": "Overweight",
    "date": "2026-05-01",
    "timestamp": "2026-05-01T23:39:43"
  }
}
```

Pull them out:

```bash
kubectl -n tradingagents cp ta-shell:/home/appuser/.tradingagents/logs ./results
# On WSL: explorer.exe results
```

---

## Configuration

All config is via environment variables — set them in the ConfigMap (`kubectl -n tradingagents edit configmap tradingagents-config`) or as Pod env vars.

### Core (used by both TUI and `main_copilot.py`)

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `github-copilot` | Provider: `openai`, `anthropic`, `google`, `github-copilot`, `xai`, `deepseek`, `qwen`, `glm`, `openrouter`, `ollama` |
| `DEEP_THINK_LLM` | `claude-opus-4.7` | Model for complex reasoning (debates, decisions) |
| `QUICK_THINK_LLM` | `claude-opus-4.7` | Model for fast tasks (data fetching, parsing) |
| `DATA_VENDOR` | `yfinance` | Market data: `yfinance` or `alpha_vantage` |
| `GITHUB_TOKEN` | — | GitHub PAT with Copilot access (in the Secret, not ConfigMap) |

### `main_copilot.py` only (script mode)

| Variable | Default | Description |
|---|---|---|
| `TRADING_SYMBOLS` | `NVDA` | Comma-separated symbols (`NVDA,AAPL,MSFT`) |
| `ANALYSIS_DATE` | yesterday | Date to analyze (YYYY-MM-DD) |
| `MAX_DEBATE_ROUNDS` | `1` | Bull/Bear debate rounds |
| `MAX_RISK_DISCUSS_ROUNDS` | `1` | Risk debate rounds |
| `OUTPUT_LANGUAGE` | `English` | Output language |

### Market Scanner (`main_scanner.py`)

| Variable | Default | Description |
|---|---|---|
| `SCANNER_MODE` | `full` | `full` (scan + analyze) or `scan-only` |
| `SCANNER_MAX_PICKS` | `10` | Max stocks to run full analysis on |
| `SCANNER_LLM` | `claude-opus-4.7` | Model for scanner LLM synthesis |

### TUI only (skip-vars; see TUI section above)

`TA_TICKER`, `TA_DATE`, `TA_LANGUAGE`, `TA_ANALYSTS`, `TA_RESEARCH_DEPTH`, `TA_ANTHROPIC_EFFORT`, `TA_OPENAI_REASONING_EFFORT`, `TA_GOOGLE_THINKING_LEVEL`, `TA_BACKEND_URL`.

---

## K8s Architecture

```
k8s/
  namespace.yaml              # tradingagents namespace
  secret.yaml                 # Worker API tokens (GITHUB_TOKEN)
  configmap.yaml              # LLM provider, models, symbols config
  pvc.yaml                    # 5Gi persistent storage for logs/cache/memory
  pod-shell.yaml              # Long-lived pod for interactive TUI (kubectl exec)
  job-manual.yaml             # On-demand non-interactive run
  cronjob.yaml                # Scheduled non-interactive run (Mon–Fri 9:30 AM ET)
  postgres.yaml               # Postgres 16 StatefulSet + Service (WebUI history)
  webui-deployment.yaml       # WebUI Deployment + Service + DSN secret
  webui-ingress.yaml          # Optional Ingress for browser access
  deploy.sh                   # One-command deploy script (worker side)
```

After applying everything:

```
NAME                                   READY   STATUS    PURPOSE
ta-shell                               1/1     Running   ← interactive TUI host (long-lived)
postgres-0                             1/1     Running   ← run history for WebUI
tradingagents-webui-xxxx               1/1     Running   ← FastAPI WebUI
cronjob/tradingagents-daily                              ← scheduled batch run
pvc/tradingagents-data (5Gi)                             ← shared results storage
pvc/data-postgres-0 (5Gi)                                ← run history storage
```

---

## GitHub Copilot Provider

This fork adds `github-copilot` as an LLM provider, using your existing Copilot subscription:

- Endpoint: `https://api.githubcopilot.com/chat/completions`
- Auth: GitHub PAT with Copilot access (or `gh auth token`)
- Sends `Copilot-Integration-Id: vscode-chat` header
- Available models: `claude-opus-4.7`, `claude-sonnet-4`, `gpt-4o`, `gpt-4o-mini`, `gpt-5.4`, and more
- No extra API costs beyond your Copilot subscription

## Supported Providers

| Provider | Models | Auth env var |
|---|---|---|
| `github-copilot` | Claude Opus 4.7, GPT-5.4, Sonnet 4, GPT-4o | `GITHUB_TOKEN` |
| `openai` | GPT-5.4, GPT-4.1 | `OPENAI_API_KEY` |
| `anthropic` | Claude Opus 4.6, Sonnet 4.6 | `ANTHROPIC_API_KEY` |
| `google` | Gemini 3.1 Pro, 2.5 Flash | `GOOGLE_API_KEY` |
| `xai` | Grok 4 | `XAI_API_KEY` |
| `deepseek` | DeepSeek R1 | `DEEPSEEK_API_KEY` |
| `ollama` | Local models | None (localhost) |

---

## Cleanup

```bash
kubectl delete namespace tradingagents
```

## Credits

Based on [TradingAgents](https://github.com/tauricresearch/tradingagents) by [Tauric Research](https://tauric.ai). See the original repo for the research paper and framework details.

## Changes from Upstream

This fork adds the following on top of the original TradingAgents framework:

### GitHub Copilot Provider
- Added `github-copilot` as a new OpenAI-compatible LLM provider in `tradingagents/llm_clients/`
- Routes through `https://api.githubcopilot.com/chat/completions` with the `Copilot-Integration-Id` header
- Uses standard Chat Completions API (not OpenAI Responses API)
- Authenticates with a GitHub PAT that has Copilot access — no extra API costs
- Added model catalog entries for Copilot-available models (Claude Opus 4.7, Sonnet 4, GPT-5.4, GPT-4o, etc.)

### Env-Driven TUI (interactive `tradingagents` CLI)
- The TUI now consults env vars before each prompt (`LLM_PROVIDER`, `DEEP_THINK_LLM`, `QUICK_THINK_LLM`, plus `TA_*` overrides) and skips any step that's already set — letting you pre-fill provider+models from a ConfigMap while still answering ticker/date/analyst/depth interactively.
- Adds `github-copilot` to the provider URL map (the upstream provider menu doesn't list it, but env-var override bypasses that).
- **Step 1 now offers a Market Scanner branch**: instead of typing a ticker you can ask the scanner to recommend top 5 / 10 / 20 stocks, then pick one to deep-analyze. Set `TA_TICKER_SOURCE=manual|scan-5|scan-10|scan-20` to skip the prompt.
- Switched the live `Rich.Live` renderer to **alt-screen mode** (`screen=True`) to eliminate flicker when run inside `kubectl exec -it`.

### Env-Driven `main_copilot.py` (non-interactive)
- Reads all config from environment variables for K8s deployment
- Multi-symbol analysis (`TRADING_SYMBOLS=NVDA,AAPL,MSFT`)
- CLI override: `python main_copilot.py AAPL,MSFT 2025-05-01`
- Saves results as timestamped JSON

### Kubernetes Deployment
- Full K8s manifest set in `k8s/`:
  - `namespace.yaml` — dedicated namespace
  - `secret.yaml` — `GITHUB_TOKEN` storage
  - `configmap.yaml` — all trading config (provider, models, symbols, language)
  - `pvc.yaml` — 5 Gi persistent storage for logs, cache, and trading memory
  - **`pod-shell.yaml` — long-lived pod for the interactive TUI (`kubectl exec -it ta-shell -- tradingagents`), with `TERM=xterm-256color`, `FORCE_COLOR=1`, and the PVC mounted**
  - `cronjob.yaml` — automated daily runs at market open (9:30 AM ET, Mon–Fri)
  - `job-manual.yaml` — on-demand non-interactive analysis
  - **`postgres.yaml` — Postgres 16 StatefulSet + Service for WebUI run history**
  - **`webui-deployment.yaml` — FastAPI WebUI Deployment + Service**
  - **`webui-ingress.yaml` — optional Ingress for browser access**
  - `deploy.sh` — one-command deploy with `--build` and `--token` flags
- Works on any K8s cluster (Kind, Minikube, k3s/k3d, Docker Desktop, EKS, GKE, AKS)
- Tested end-to-end on local k3s with Claude Opus 4.7

### WebUI (Phase A)
- New `webui/` package + `Dockerfile.webui`:
  - FastAPI app with form-driven UI, multi-ticker queue, Market Scanner integration
  - Live agent output via Server-Sent Events (SSE)
  - 4-pane Plotly chart per result (candlestick + volume + RSI + MACD)
  - Postgres-backed run history (`/api/runs`)
  - REST endpoints: `POST /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/events` (SSE), `GET /api/scan`, `GET /api/chart/{symbol}`
  - Single-page UI uses Alpine.js + Plotly.js via CDN — no JS build step
- `[webui]` optional dependency group in `pyproject.toml` keeps the worker image lean
