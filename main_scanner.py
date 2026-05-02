"""Market Scanner entry point.

Runs the MarketScanner to identify promising stocks, then feeds each pick
through the full TradingAgentsGraph pipeline for deep analysis.

Usage:
    python main_scanner.py
    python main_scanner.py --max-picks 5
    python main_scanner.py --date 2025-05-01
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta

from tradingagents.scanner import MarketScanner
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scanner")

# ── Config from env vars (same pattern as main_copilot.py) ──
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = os.getenv("LLM_PROVIDER", "github-copilot")
config["deep_think_llm"] = os.getenv("DEEP_THINK_LLM", "claude-opus-4.7")
config["quick_think_llm"] = os.getenv("QUICK_THINK_LLM", "claude-opus-4.7")
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

scanner_mode = os.getenv("SCANNER_MODE", "full")  # "scan-only" or "full"
max_picks = int(os.getenv("SCANNER_MAX_PICKS", "10"))

analysis_date = os.getenv("ANALYSIS_DATE")
if not analysis_date:
    yesterday = datetime.now() - timedelta(days=1)
    analysis_date = yesterday.strftime("%Y-%m-%d")

# CLI overrides
import argparse
parser = argparse.ArgumentParser(description="Market Scanner + Trading Pipeline")
parser.add_argument("--date", default=analysis_date, help="Analysis date (YYYY-MM-DD)")
parser.add_argument("--max-picks", type=int, default=max_picks, help="Max stocks to analyze")
parser.add_argument("--scan-only", action="store_true", help="Only scan, skip full pipeline")
args = parser.parse_args()

analysis_date = args.date
max_picks = args.max_picks
scan_only = args.scan_only or scanner_mode == "scan-only"

results_dir = config.get("results_dir", os.path.expanduser("~/.tradingagents/logs"))
os.makedirs(results_dir, exist_ok=True)

# ── Run Scanner ──
print(f"{'='*60}")
print(f"Market Scanner")
print(f"Provider: {config['llm_provider']}")
print(f"Model: {config['deep_think_llm']}")
print(f"Date: {analysis_date}")
print(f"Mode: {'scan-only' if scan_only else 'full pipeline'}")
print(f"Max picks: {max_picks}")
print(f"{'='*60}\n")

scanner = MarketScanner(
    provider=config["llm_provider"],
    model=config["deep_think_llm"],
)

scan_result = scanner.scan()
symbols = scan_result["symbols"][:max_picks]

print(f"\nScanner identified {len(symbols)} stocks: {', '.join(symbols)}")
print(f"Reasoning: {scan_result['reasoning']}\n")

output = {
    "scan": {
        "symbols": symbols,
        "reasoning": scan_result["reasoning"],
        "market_data": scan_result["market_data"],
        "timestamp": scan_result["timestamp"],
    },
    "analysis": {},
}

# ── Run Full Pipeline (unless scan-only) ──
if not scan_only and symbols:
    print(f"\nRunning full trading pipeline for {len(symbols)} stocks...\n")
    ta = TradingAgentsGraph(debug=True, config=config)

    for symbol in symbols:
        print(f"\n{'='*50}")
        print(f"Analyzing {symbol} as of {analysis_date}")
        print(f"{'='*50}\n")
        try:
            _, decision = ta.propagate(symbol, analysis_date)
            output["analysis"][symbol] = {
                "decision": decision,
                "date": analysis_date,
                "timestamp": datetime.now().isoformat(),
            }
            print(f"\n[{symbol}] Decision: {decision}")
        except Exception as e:
            logger.error(f"[{symbol}] Pipeline error: {e}")
            output["analysis"][symbol] = {
                "error": str(e),
                "date": analysis_date,
                "timestamp": datetime.now().isoformat(),
            }

# ── Save Results ──
output_file = os.path.join(
    results_dir,
    f"scanner_{analysis_date}_{datetime.now().strftime('%H%M%S')}.json",
)
with open(output_file, "w") as f:
    json.dump(output, f, indent=2, default=str)

print(f"\nResults saved to {output_file}")
