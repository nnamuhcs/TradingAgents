# TradingAgents on K8s

Multi-agent LLM trading framework with a live terminal dashboard, deployable on Kubernetes. Forked from [TauricResearch/TradingAgents](https://github.com/tauricresearch/tradingagents) with added **GitHub Copilot provider support**, **K8s manifests** for both interactive and batch use, and **env-driven config** that lets the interactive TUI skip prompts you've already answered in the ConfigMap.

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

## Two ways to run

There are two complementary ways to use this framework. Pick whichever fits your workflow — the K8s manifests support both side-by-side.

### 🎛️ Interactive TUI (live Rich dashboard)

Best for: exploring, demos, picking models on the fly, watching agents work in real time.

- Walks you through ticker, date, language, analysts, depth, and effort with arrow-key prompts
- Renders a **live Rich dashboard** — analyst statuses, message stream, tool calls, current report — that updates as the graph executes
- Output is human-readable; you stay in the loop until the final decision is printed

### ⚙️ Non-interactive (script / batch)

Best for: scheduled runs, multi-symbol portfolios, CI/CD, headless servers.

- Reads everything from environment variables (ConfigMap + Secret in K8s)
- Writes a JSON file per run with the decision and timestamp
- Runs as a one-shot K8s `Job` or a daily `CronJob` (`9:30 AM ET, Mon–Fri`)
- Includes a **Market Scanner** entry point (`main_scanner.py`) that automatically picks stocks to analyze (see below)

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
- Docker (for building the image)

### One-Command Deploy

```bash
./k8s/deploy.sh --build --token "$(gh auth token)"
```

This builds the image, creates the namespace, secret, configmap, PVC, CronJob, **and a long-lived `ta-shell` pod** for running the interactive TUI inside the cluster.

### Image distribution by cluster type

| Cluster | After `docker build -t tradingagents:latest .` |
|---|---|
| **Docker Desktop K8s** | Nothing — image is auto-visible to the cluster |
| **Kind** | `kind load docker-image tradingagents:latest --name <cluster>` |
| **Minikube** | `minikube image load tradingagents:latest` |
| **k3s / k3d** | `docker save tradingagents:latest -o /tmp/ta.tar && sudo k3s ctr images import /tmp/ta.tar` (or push to a local registry, see [k3s docs](https://docs.k3s.io/installation/private-registry)) |
| **Cloud (EKS/GKE/AKS)** | `docker tag` + `docker push` to your registry, then update `image:` in the manifests |

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
  namespace.yaml      # tradingagents namespace
  secret.yaml         # API tokens (GITHUB_TOKEN)
  configmap.yaml      # LLM provider, models, symbols config
  pvc.yaml            # 5Gi persistent storage for logs/cache/memory
  pod-shell.yaml      # Long-lived pod for interactive TUI (kubectl exec)
  job-manual.yaml     # On-demand non-interactive run
  cronjob.yaml        # Scheduled non-interactive run (Mon–Fri 9:30 AM ET)
  deploy.sh           # One-command deploy script
```

After `deploy.sh`:

```
NAME                       READY   STATUS    PURPOSE
ta-shell                   1/1     Running   ← interactive TUI host (long-lived)
cronjob/tradingagents-daily            ← scheduled batch run
pvc/tradingagents-data (5Gi)           ← shared results storage
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
  - `deploy.sh` — one-command deploy with `--build` and `--token` flags
- Works on any K8s cluster (Kind, Minikube, k3s/k3d, Docker Desktop, EKS, GKE, AKS)
- Tested end-to-end on local k3s with Claude Opus 4.7
