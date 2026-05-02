import questionary
from typing import List, Optional, Tuple, Dict

from rich.console import Console

from cli.models import AnalystType
from tradingagents.llm_clients.model_catalog import get_model_options

console = Console()

TICKER_INPUT_EXAMPLES = "Examples: SPY, CNC.TO, 7203.T, 0700.HK"

ANALYST_ORDER = [
    ("Market Analyst", AnalystType.MARKET),
    ("Social Media Analyst", AnalystType.SOCIAL),
    ("News Analyst", AnalystType.NEWS),
    ("Fundamentals Analyst", AnalystType.FUNDAMENTALS),
]


def get_ticker() -> str:
    """Prompt the user to enter a ticker symbol."""
    ticker = questionary.text(
        f"Enter the exact ticker symbol to analyze ({TICKER_INPUT_EXAMPLES}):",
        validate=lambda x: len(x.strip()) > 0 or "Please enter a valid ticker symbol.",
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if not ticker:
        console.print("\n[red]No ticker symbol provided. Exiting...[/red]")
        exit(1)

    return normalize_ticker_symbol(ticker)


def select_ticker_source() -> str:
    """Ask whether to enter a ticker manually or use the Market Scanner.

    Returns one of: 'manual', 'scan-5', 'scan-10', 'scan-20'.
    """
    choice = questionary.select(
        "How do you want to choose the ticker?",
        choices=[
            questionary.Choice("Type a ticker symbol myself", value="manual"),
            questionary.Choice("Let the Market Scanner pick top 5 (fastest)", value="scan-5"),
            questionary.Choice("Let the Market Scanner pick top 10", value="scan-10"),
            questionary.Choice("Let the Market Scanner pick top 20 (slowest)", value="scan-20"),
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()
    if choice is None:
        console.print("\n[red]No choice made. Exiting...[/red]")
        exit(1)
    return choice


def run_scanner_and_pick(max_picks: int, llm_provider: str | None = None,
                         scanner_model: str | None = None) -> list[str]:
    """Run the MarketScanner with a live status spinner, then let the user pick
    one or more of the recommended tickers to analyze.

    Returns a list of chosen ticker symbols (uppercase). Length >= 1.
    """
    import os
    from tradingagents.scanner import MarketScanner
    from rich.table import Table

    provider = llm_provider or os.getenv("LLM_PROVIDER", "github-copilot")
    model = scanner_model or os.getenv("SCANNER_LLM") or os.getenv("DEEP_THINK_LLM", "claude-opus-4.7")

    console.print(
        f"\n[bold]Market Scanner running[/bold] — provider=[cyan]{provider}[/cyan], "
        f"model=[cyan]{model}[/cyan], max_picks=[cyan]{max_picks}[/cyan]"
    )
    console.print(
        "[dim]This screens the S&P 500 across quant signals, event catalysts, smart-money "
        "activity, then runs LLM synthesis. Typically 1–3 minutes.[/dim]\n"
    )

    with console.status("[bold cyan]Scanning market...[/bold cyan]", spinner="dots"):
        scanner = MarketScanner(provider=provider, model=model)
        result = scanner.scan()

    detailed = result.get("detailed", {})
    picks = (detailed.get("picks") or [])[:max_picks]
    candidates = detailed.get("candidates", [])

    if not picks:
        console.print("[red]Scanner returned no picks. Falling back to manual entry.[/red]")
        return [get_ticker()]

    table = Table(title=f"Scanner Picks (top {len(picks)})", show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Symbol", style="bold cyan")
    table.add_column("Conviction", style="bold")
    table.add_column("Signals", style="dim")
    table.add_column("Reasoning")

    conviction_color = {"high": "green", "medium": "yellow", "low": "white"}

    for i, pick in enumerate(picks, 1):
        conv = (pick.get("conviction") or "?").lower()
        cand = next((c for c in candidates if c["symbol"] == pick["symbol"]), None)
        signals = []
        if cand:
            if cand.get("rs_1m") is not None:
                signals.append(f"RS1m={cand['rs_1m']:+.1f}%")
            if cand.get("vol_ratio") is not None:
                signals.append(f"Vol={cand['vol_ratio']:.1f}x")
            if cand.get("rsi") is not None:
                signals.append(f"RSI={cand['rsi']:.0f}")
            if cand.get("at_20d_high"):
                signals.append("20dHigh")
            if cand.get("events"):
                signals.append("Events")
            if cand.get("smart_money_signals"):
                signals.append("Smart$")
        table.add_row(
            str(i),
            pick["symbol"],
            f"[{conviction_color.get(conv, 'white')}]{conv.upper()}[/]",
            " | ".join(signals) or "-",
            (pick.get("reasoning") or "")[:90],
        )

    if detailed.get("market_regime"):
        console.print(f"[bold]Market Regime:[/bold] {detailed['market_regime']}")
    if detailed.get("themes"):
        console.print(f"[bold]Themes:[/bold] {', '.join(detailed['themes'])}")
    console.print(table)

    # Step 2: how many to deep-analyze?
    mode = questionary.select(
        "How do you want to deep-analyze the picks?",
        choices=[
            questionary.Choice("Pick ONE to deep-analyze", value="one"),
            questionary.Choice(f"Run ALL {len(picks)} picks (sequentially)", value="all"),
            questionary.Choice("Pick MULTIPLE (space to toggle)", value="multi"),
            questionary.Choice("(none — let me type a ticker manually)", value="manual"),
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if mode is None or mode == "manual":
        return [get_ticker()]
    if mode == "all":
        return [normalize_ticker_symbol(p["symbol"]) for p in picks]
    if mode == "multi":
        chosen = questionary.checkbox(
            "Select symbols to deep-analyze (space to toggle, enter to confirm):",
            choices=[
                questionary.Choice(
                    f"{p['symbol']:<6}  [{(p.get('conviction') or '?').upper()}]  "
                    f"{(p.get('reasoning') or '')[:70]}",
                    value=p["symbol"],
                    checked=True,
                )
                for p in picks
            ],
            style=questionary.Style(
                [
                    ("selected", "fg:magenta noinherit"),
                    ("highlighted", "fg:magenta noinherit"),
                    ("pointer", "fg:magenta noinherit"),
                ]
            ),
        ).ask()
        if not chosen:
            console.print("[yellow]No symbols selected; falling back to manual entry.[/yellow]")
            return [get_ticker()]
        return [normalize_ticker_symbol(s) for s in chosen]

    # mode == "one"
    chosen = questionary.select(
        "Which one do you want to deep-analyze?",
        choices=[
            questionary.Choice(
                f"{p['symbol']:<6}  [{(p.get('conviction') or '?').upper()}]  "
                f"{(p.get('reasoning') or '')[:70]}",
                value=p["symbol"],
            )
            for p in picks
        ] + [questionary.Choice("(none — let me type a ticker manually)", value="__manual__")],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if chosen is None or chosen == "__manual__":
        return [get_ticker()]
    return [normalize_ticker_symbol(chosen)]


def normalize_ticker_symbol(ticker: str) -> str:
    """Normalize ticker input while preserving exchange suffixes."""
    return ticker.strip().upper()


def get_analysis_date() -> str:
    """Prompt the user to enter a date in YYYY-MM-DD format."""
    import re
    from datetime import datetime

    def validate_date(date_str: str) -> bool:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return False
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    date = questionary.text(
        "Enter the analysis date (YYYY-MM-DD):",
        validate=lambda x: validate_date(x.strip())
        or "Please enter a valid date in YYYY-MM-DD format.",
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if not date:
        console.print("\n[red]No date provided. Exiting...[/red]")
        exit(1)

    return date.strip()


def select_analysts() -> List[AnalystType]:
    """Select analysts using an interactive checkbox."""
    choices = questionary.checkbox(
        "Select Your [Analysts Team]:",
        choices=[
            questionary.Choice(display, value=value) for display, value in ANALYST_ORDER
        ],
        instruction="\n- Press Space to select/unselect analysts\n- Press 'a' to select/unselect all\n- Press Enter when done",
        validate=lambda x: len(x) > 0 or "You must select at least one analyst.",
        style=questionary.Style(
            [
                ("checkbox-selected", "fg:green"),
                ("selected", "fg:green noinherit"),
                ("highlighted", "noinherit"),
                ("pointer", "noinherit"),
            ]
        ),
    ).ask()

    if not choices:
        console.print("\n[red]No analysts selected. Exiting...[/red]")
        exit(1)

    return choices


def select_research_depth() -> int:
    """Select research depth using an interactive selection."""

    # Define research depth options with their corresponding values
    DEPTH_OPTIONS = [
        ("Shallow - Quick research, few debate and strategy discussion rounds", 1),
        ("Medium - Middle ground, moderate debate rounds and strategy discussion", 3),
        ("Deep - Comprehensive research, in depth debate and strategy discussion", 5),
    ]

    choice = questionary.select(
        "Select Your [Research Depth]:",
        choices=[
            questionary.Choice(display, value=value) for display, value in DEPTH_OPTIONS
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:yellow noinherit"),
                ("highlighted", "fg:yellow noinherit"),
                ("pointer", "fg:yellow noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print("\n[red]No research depth selected. Exiting...[/red]")
        exit(1)

    return choice


def _fetch_openrouter_models() -> List[Tuple[str, str]]:
    """Fetch available models from the OpenRouter API."""
    import requests
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        return [(m.get("name") or m["id"], m["id"]) for m in models]
    except Exception as e:
        console.print(f"\n[yellow]Could not fetch OpenRouter models: {e}[/yellow]")
        return []


def select_openrouter_model() -> str:
    """Select an OpenRouter model from the newest available, or enter a custom ID."""
    models = _fetch_openrouter_models()

    choices = [questionary.Choice(name, value=mid) for name, mid in models[:5]]
    choices.append(questionary.Choice("Custom model ID", value="custom"))

    choice = questionary.select(
        "Select OpenRouter Model (latest available):",
        choices=choices,
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style([
            ("selected", "fg:magenta noinherit"),
            ("highlighted", "fg:magenta noinherit"),
            ("pointer", "fg:magenta noinherit"),
        ]),
    ).ask()

    if choice is None or choice == "custom":
        return questionary.text(
            "Enter OpenRouter model ID (e.g. google/gemma-4-26b-a4b-it):",
            validate=lambda x: len(x.strip()) > 0 or "Please enter a model ID.",
        ).ask().strip()

    return choice


def _prompt_custom_model_id() -> str:
    """Prompt user to type a custom model ID."""
    return questionary.text(
        "Enter model ID:",
        validate=lambda x: len(x.strip()) > 0 or "Please enter a model ID.",
    ).ask().strip()


def _select_model(provider: str, mode: str) -> str:
    """Select a model for the given provider and mode (quick/deep)."""
    if provider.lower() == "openrouter":
        return select_openrouter_model()

    if provider.lower() == "azure":
        return questionary.text(
            f"Enter Azure deployment name ({mode}-thinking):",
            validate=lambda x: len(x.strip()) > 0 or "Please enter a deployment name.",
        ).ask().strip()

    choice = questionary.select(
        f"Select Your [{mode.title()}-Thinking LLM Engine]:",
        choices=[
            questionary.Choice(display, value=value)
            for display, value in get_model_options(provider, mode)
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print(f"\n[red]No {mode} thinking llm engine selected. Exiting...[/red]")
        exit(1)

    if choice == "custom":
        return _prompt_custom_model_id()

    return choice


def select_shallow_thinking_agent(provider) -> str:
    """Select shallow thinking llm engine using an interactive selection."""
    return _select_model(provider, "quick")


def select_deep_thinking_agent(provider) -> str:
    """Select deep thinking llm engine using an interactive selection."""
    return _select_model(provider, "deep")

def select_llm_provider() -> tuple[str, str | None]:
    """Select the LLM provider and its API endpoint."""
    # (display_name, provider_key, base_url)
    PROVIDERS = [
        ("OpenAI", "openai", "https://api.openai.com/v1"),
        ("Google", "google", None),
        ("Anthropic", "anthropic", "https://api.anthropic.com/"),
        ("xAI", "xai", "https://api.x.ai/v1"),
        ("DeepSeek", "deepseek", "https://api.deepseek.com"),
        ("Qwen", "qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("GLM", "glm", "https://open.bigmodel.cn/api/paas/v4/"),
        ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1"),
        ("Azure OpenAI", "azure", None),
        ("Ollama", "ollama", "http://localhost:11434/v1"),
    ]

    choice = questionary.select(
        "Select your LLM Provider:",
        choices=[
            questionary.Choice(display, value=(provider_key, url))
            for display, provider_key, url in PROVIDERS
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()
    
    if choice is None:
        console.print("\n[red]No LLM provider selected. Exiting...[/red]")
        exit(1)

    provider, url = choice
    return provider, url


def ask_openai_reasoning_effort() -> str:
    """Ask for OpenAI reasoning effort level."""
    choices = [
        questionary.Choice("Medium (Default)", "medium"),
        questionary.Choice("High (More thorough)", "high"),
        questionary.Choice("Low (Faster)", "low"),
    ]
    return questionary.select(
        "Select Reasoning Effort:",
        choices=choices,
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_anthropic_effort() -> str | None:
    """Ask for Anthropic effort level.

    Controls token usage and response thoroughness on Claude 4.5+ and 4.6 models.
    """
    return questionary.select(
        "Select Effort Level:",
        choices=[
            questionary.Choice("High (recommended)", "high"),
            questionary.Choice("Medium (balanced)", "medium"),
            questionary.Choice("Low (faster, cheaper)", "low"),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_gemini_thinking_config() -> str | None:
    """Ask for Gemini thinking configuration.

    Returns thinking_level: "high" or "minimal".
    Client maps to appropriate API param based on model series.
    """
    return questionary.select(
        "Select Thinking Mode:",
        choices=[
            questionary.Choice("Enable Thinking (recommended)", "high"),
            questionary.Choice("Minimal/Disable Thinking", "minimal"),
        ],
        style=questionary.Style([
            ("selected", "fg:green noinherit"),
            ("highlighted", "fg:green noinherit"),
            ("pointer", "fg:green noinherit"),
        ]),
    ).ask()


def ask_output_language() -> str:
    """Ask for report output language."""
    choice = questionary.select(
        "Select Output Language:",
        choices=[
            questionary.Choice("English (default)", "English"),
            questionary.Choice("Chinese (中文)", "Chinese"),
            questionary.Choice("Japanese (日本語)", "Japanese"),
            questionary.Choice("Korean (한국어)", "Korean"),
            questionary.Choice("Hindi (हिन्दी)", "Hindi"),
            questionary.Choice("Spanish (Español)", "Spanish"),
            questionary.Choice("Portuguese (Português)", "Portuguese"),
            questionary.Choice("French (Français)", "French"),
            questionary.Choice("German (Deutsch)", "German"),
            questionary.Choice("Arabic (العربية)", "Arabic"),
            questionary.Choice("Russian (Русский)", "Russian"),
            questionary.Choice("Custom language", "custom"),
        ],
        style=questionary.Style([
            ("selected", "fg:yellow noinherit"),
            ("highlighted", "fg:yellow noinherit"),
            ("pointer", "fg:yellow noinherit"),
        ]),
    ).ask()

    if choice == "custom":
        return questionary.text(
            "Enter language name (e.g. Turkish, Vietnamese, Thai, Indonesian):",
            validate=lambda x: len(x.strip()) > 0 or "Please enter a language name.",
        ).ask().strip()

    return choice
