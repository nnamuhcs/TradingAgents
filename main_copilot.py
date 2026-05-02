from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Create a custom config using GitHub Copilot subscription
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "github-copilot"
config["deep_think_llm"] = "claude-opus-4.7-1m-internal"  # 1M context, via Copilot
config["quick_think_llm"] = "claude-opus-4.7-1m-internal"  # All opus, via Copilot
config["max_debate_rounds"] = 1

# Use yfinance for all data (free, no API keys needed)
config["data_vendors"] = {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}

# Initialize with custom config
ta = TradingAgentsGraph(debug=True, config=config)

# Paper trade: analyze NVDA
_, decision = ta.propagate("NVDA", "2025-05-01")
print(decision)
