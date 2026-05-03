# Verve · drop-in pack

Three files. Replaces your existing `webui/templates/index.html` with the new
brand and the three-view shell (Home → Floor → Stage).

```
webui/
├─ templates/
│   └─ index.html        ← replace
└─ static/
    ├─ css/
    │   └─ floor.css     ← new
    └─ js/
        └─ floor.js      ← new
```

If your FastAPI app already mounts `/static` and serves `index.html` from
`webui/templates`, no Python changes needed. If you don't yet have a static
mount, add this to your FastAPI app:

```python
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="webui/static"), name="static")
```

## What hits which endpoint

The JS hits these endpoints (already in your README):

| Action            | Endpoint                                        |
|-------------------|-------------------------------------------------|
| Marquee feed      | `GET /api/movers/stream`        (SSE)           |
| Start a run       | `POST /api/runs`                                |
| Live run events   | `GET /api/runs/{id}/events`     (SSE)           |
| Full report       | `GET /api/runs/{id}/report.md?symbol=X`         |
| 4-pane chart      | `GET /api/chart/{symbol}?period=6mo`            |

## SSE event shapes the JS expects

The JS dispatches on `event:` names. Most map directly to events you already
emit; a few are net-new and worth wiring up:

| Event              | Payload                                          | Status     |
|--------------------|--------------------------------------------------|------------|
| `symbol_start`     | `{symbol}`                                       | existing   |
| `agent_status`     | `{symbol, agent, status: live|done|wait}`        | existing   |
| `message`          | `{symbol, agent, text, round?}`                  | existing   |
| `tool_call`        | `{symbol, name, args}`                           | existing   |
| `report_section`   | `{symbol, section, content}`                     | existing   |
| `decision`         | `{symbol, rating}`                               | existing   |
| `symbol_done`      | `{symbol}`                                       | existing   |
| `run_done`         | `{}`                                             | existing   |
| **`conviction`**   | **`{symbol, score: 0-10}`**                      | **new**    |
| **`debate_round`** | **`{symbol, round, total}`**                     | **new**    |
| **`price`**        | **`{symbol, price, changePct}`**                 | **new**    |

The new three are what power the conviction bars, the debate progress card on
the stage, and the price line in each row. If you don't emit them yet, the UI
degrades gracefully — rows just won't show conviction or live price.

A reasonable place to emit `conviction` is right after each Bull/Bear round
inside the research debate (score the latest argument 0-10). `debate_round`
fires at the start of each round. `price` can pull from your existing yfinance
poller — emit once per second only for symbols in the active run.

## Drama details to watch for

- **Verdict drop**: row pulses (bull/bear/neutral color) for ~1.2s when
  `decision` arrives. One-shot, controlled by a transient `_justLanded` flag.
- **Conviction flip**: when `conviction` crosses 5.0 with movement >0.5, the
  row gets an amber left accent and a `↻ Flipped` tag for 30s.
- **Marquee flare**: the live-pulse dot does a one-shot expand whenever any
  symbol's `decision` event arrives. Subtle but you'll feel it.
- **Vignette + grain**: edge darkening and ~2.5% film grain are page-level,
  set on `body::before` and `body::after`. They don't move; they just frame.

## Keyboard

- `Esc` from stage → floor
- `1..9` on stage → jump to that symbol's view
- `←` / `→` on stage → cycle report tabs

## Brand

The `<header class="v-brand-bar">` is the brand. Edit the markup if you want a
different sub-label, but keep the amber bar — it's the only place that color
appears statically and it ties to the live-pulse used for state.

## Things I deliberately did not do

- No scanner funnel view (separate build, deserves its own focused widget)
- No final-batch-verdict screen (also separate; magazine-cover treatment)
- No history timeline (the existing list works; a timeline upgrade is its own
  exercise)
- No localStorage watchlist (the existing implementation in your codebase is
  fine; this drop-in doesn't touch it but you may want to wire the
  pinned-symbols input back in next to the marquee)
