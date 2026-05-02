"""TradingAgents runner with GitHub Copilot provider support.

Reads configuration from environment variables for K8s deployment.
Falls back to defaults for local development.
"""
import os
import sys
import json
from datetime import datetime, timedelta

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv
load_dotenv()

# Build config from env vars with sensible defaults
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = os.getenv("LLM_PROVIDER", "github-copilot")
config["deep_think_llm"] = os.getenv("DEEP_THINK_LLM", "claude-opus-4.7-1m-internal")
config["quick_think_llm"] = os.getenv("QUICK_THINK_LLM", "claude-opus-4.7-1m-internal")
config["max_debate_rounds"] = int(os.getenv("MAX_DEBATE_ROUNDS", "1"))
config["max_risk_discuss_rounds"] = int(os.getenv("MAX_RISK_DISCUSS_ROUNDS", "1"))
config["output_language"] = os.getenv("OUTPUT_LANGUAGE", "English")

data_vendor = os.getenv("DATA_VENDOR", "yfinance")
config["data_vendors"] = {
    "core_stock_apis": data_vendor,
    "technical_indicators": data_vendor,
    "fundamental_data": data_vendor,
    "news_data": data_vendor,
}

# Symbols to analyze (comma-separated via env, or CLI arg, or default)
symbols_str = os.getenv("TRADING_SYMBOLS", "NVDA")
symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]

# Analysis date (default: yesterday for completed trading day)
analysis_date = os.getenv("ANALYSIS_DATE")
if not analysis_date:
    yesterday = datetime.now() - timedelta(days=1)
    analysis_date = yesterday.strftime("%Y-%m-%d")

# Allow CLI override: python main_copilot.py AAPL,MSFT 2025-05-01
if len(sys.argv) > 1:
    symbols = [s.strip() for s in sys.argv[1].split(",")]
if len(sys.argv) > 2:
    analysis_date = sys.argv[2]

# Results output
results_dir = config.get("results_dir", os.path.expanduser("~/.tradingagents/logs"))
os.makedirs(results_dir, exist_ok=True)

print(f"TradingAgents Paper Trade")
print(f"Provider: {config['llm_provider']}")
print(f"Deep model: {config['deep_think_llm']}")
print(f"Quick model: {config['quick_think_llm']}")
print(f"Symbols: {symbols}")
print(f"Date: {analysis_date}")
print(f"Results: {results_dir}")
print("-" * 50)

ta = TradingAgentsGraph(debug=True, config=config)

all_results = {}
for symbol in symbols:
    print(f"\n{'='*50}")
    print(f"Analyzing {symbol} as of {analysis_date}")
    print(f"{'='*50}\n")
    try:
        _, decision = ta.propagate(symbol, analysis_date)
        all_results[symbol] = {
            "decision": decision,
            "date": analysis_date,
            "timestamp": datetime.now().isoformat(),
        }
        print(f"\n[{symbol}] Decision: {decision}")
    except Exception as e:
        print(f"\n[{symbol}] ERROR: {e}")
        all_results[symbol] = {
            "error": str(e),
            "date": analysis_date,
            "timestamp": datetime.now().isoformat(),
        }

# Save results
output_file = os.path.join(results_dir, f"results_{analysis_date}_{datetime.now().strftime('%H%M%S')}.json")
with open(output_file, "w") as f:
    json.dump(all_results, f, indent=2, default=str)
print(f"\nResults saved to {output_file}")
