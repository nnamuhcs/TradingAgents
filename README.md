# TradingAgents on K8s

Multi-agent LLM trading framework deployed on Kubernetes. Forked from [TauricResearch/TradingAgents](https://github.com/tauricresearch/tradingagents) with added GitHub Copilot provider support and K8s deployment manifests.

## What It Does

Deploys a multi-agent AI trading system that mirrors a real trading firm:

- **Fundamentals Analyst** -- company financials, balance sheet, cash flow
- **Technical Analyst** -- MACD, RSI, SMA, EMA, ATR
- **Sentiment Analyst** -- social media and market mood
- **News Analyst** -- global news and macro events
- **Bull/Bear Researchers** -- structured debate on the analysis
- **Trader** -- makes the trading decision
- **Risk Management** -- evaluates portfolio risk
- **Portfolio Manager** -- final call with position sizing

All agents powered by LLMs (Claude Opus 4.7, GPT-5.4, Gemini, etc.) via your existing GitHub Copilot subscription -- no extra API costs.

## Quick Start (Local)

```bash
# Clone
git clone https://github.com/nnamuhcs/TradingAgents.git
cd TradingAgents

# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure
echo "GITHUB_TOKEN=your_github_pat_here" > .env

# Run
python main_copilot.py NVDA 2025-05-01
```

## Deploy to K8s

### Prerequisites
- A Kubernetes cluster (any: Kind, Minikube, EKS, GKE, AKS)
- `kubectl` configured
- Docker (for building the image)

### One-Command Deploy

```bash
# Build image + deploy with your GitHub token
./k8s/deploy.sh --build --token YOUR_GITHUB_TOKEN
```

### Manual Deploy

```bash
# Build the Docker image
docker build -t tradingagents:latest .

# For Kind clusters, load the image
kind load docker-image tradingagents:latest --name YOUR_CLUSTER

# For cloud registries
docker tag tradingagents:latest YOUR_REGISTRY/tradingagents:latest
docker push YOUR_REGISTRY/tradingagents:latest
# Then update image in k8s/cronjob.yaml and k8s/job-manual.yaml

# Deploy
kubectl apply -f k8s/namespace.yaml
kubectl -n tradingagents create secret generic tradingagents-secrets \
  --from-literal=GITHUB_TOKEN="YOUR_TOKEN"
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/cronjob.yaml
```

### Run a Manual Analysis

```bash
kubectl -n tradingagents apply -f k8s/job-manual.yaml

# Watch logs
kubectl -n tradingagents logs -f job/tradingagents-manual
```

### Check Scheduled Runs

```bash
# CronJob runs daily at 9:30 AM ET (market open), Mon-Fri
kubectl -n tradingagents get cronjob
kubectl -n tradingagents get jobs
```

## Configuration

All config is via environment variables (K8s ConfigMap):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `github-copilot` | LLM provider (openai, anthropic, google, github-copilot, xai, deepseek, ollama) |
| `DEEP_THINK_LLM` | `claude-opus-4.7-1m-internal` | Model for complex reasoning (debates, decisions) |
| `QUICK_THINK_LLM` | `claude-opus-4.7-1m-internal` | Model for fast tasks (data pulling, parsing) |
| `DATA_VENDOR` | `yfinance` | Market data source (yfinance or alpha_vantage) |
| `TRADING_SYMBOLS` | `NVDA` | Comma-separated stock symbols to analyze |
| `ANALYSIS_DATE` | Yesterday | Date to analyze (YYYY-MM-DD) |
| `MAX_DEBATE_ROUNDS` | `1` | Bull/Bear debate rounds |
| `OUTPUT_LANGUAGE` | `English` | Output language |

Edit the ConfigMap:
```bash
kubectl -n tradingagents edit configmap tradingagents-config
```

## K8s Architecture

```
k8s/
  namespace.yaml      # tradingagents namespace
  secret.yaml         # API tokens (GITHUB_TOKEN)
  configmap.yaml      # LLM provider, models, symbols config
  pvc.yaml            # 5Gi persistent storage for logs/cache/memory
  cronjob.yaml        # Daily 9:30 AM ET schedule (Mon-Fri)
  job-manual.yaml     # On-demand manual run
  deploy.sh           # One-command deploy script
```

## GitHub Copilot Provider

This fork adds `github-copilot` as an LLM provider, using your existing Copilot subscription:

- Endpoint: `https://api.githubcopilot.com/chat/completions`
- Auth: GitHub PAT with Copilot access
- Available models: `claude-opus-4.7`, `claude-opus-4.7-1m-internal`, `claude-sonnet-4`, `gpt-4o`, `gpt-4o-mini`, `gpt-5.4`, and more
- No extra API costs beyond your Copilot subscription

## Supported Providers

| Provider | Models | Auth |
|----------|--------|------|
| `github-copilot` | Claude Opus 4.7, GPT-5.4, Sonnet 4 | `GITHUB_TOKEN` |
| `openai` | GPT-5.4, GPT-4.1 | `OPENAI_API_KEY` |
| `anthropic` | Claude Opus 4.6, Sonnet 4.6 | `ANTHROPIC_API_KEY` |
| `google` | Gemini 3.1 Pro, 2.5 Flash | `GOOGLE_API_KEY` |
| `xai` | Grok 4 | `XAI_API_KEY` |
| `deepseek` | DeepSeek R1 | `DEEPSEEK_API_KEY` |
| `ollama` | Local models | None (localhost) |

## Results

Results are saved as JSON to the persistent volume:
```json
{
  "NVDA": {
    "decision": "Buy",
    "date": "2025-05-01",
    "timestamp": "2026-05-01T22:09:09"
  }
}
```

## Cleanup

```bash
kubectl delete namespace tradingagents
```

## Credits

Based on [TradingAgents](https://github.com/tauricresearch/tradingagents) by [Tauric Research](https://tauric.ai). See the original repo for the research paper and framework details.
