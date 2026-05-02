function app() {
  return {
    page: 'new',
    running: false,
    error: '',
    status: 'idle',
    log: [],
    picks: [],
    decisions: {},

    history: [],
    scanResult: {},
    scanN: 10,
    scanning: false,

    clockNow: '',
    marketTicker: [],
  pinned: JSON.parse(localStorage.getItem('ta_pinned') || '[]'),
  showSettings: false,
  newPin: '',
  liveStreamConnected: false,

    form: {
      ticker_source: 'manual',
      symbols_str: 'NVDA',
      analysis_date: new Date().toISOString().slice(0, 10),
      analysts: ['market', 'social', 'news', 'fundamentals'],
      research_depth: 1,
      risk_rounds: 1,
      language: 'English',
      llm_provider: 'github-copilot',
      deep_model: 'claude-opus-4.7',
      quick_model: 'claude-opus-4.7',
      anthropic_effort: 'high',
    },

    init() {
      this.tickClock();
      setInterval(() => this.tickClock(), 1000);
      this.subscribeMarketStream();
    },

    subscribeMarketStream() {
      const params = new URLSearchParams({ pinned: this.pinned.join(',') });
      const es = new EventSource(`/api/movers/stream?${params}`);
      es.addEventListener('snapshot', (e) => {
        try {
          const { feed, live } = JSON.parse(e.data);
          this.liveStreamConnected = !!live;
          if (Array.isArray(feed) && feed.length) {
            this.marketTicker = feed.map(x => ({
              s: x.s,
              p: x.p,
              c: x.c,
              kind: x.kind || 'item',
              live: !!x.live,
            }));
          }
        } catch (err) {
          console.warn('marquee snapshot parse error', err);
        }
      });
      es.onerror = () => {
        // EventSource auto-reconnects; just log
      };
      this._marqueeStream = es;
    },

    addPin() {
      const sym = (this.newPin || '').trim().toUpperCase();
      if (!sym) return;
      if (!this.pinned.includes(sym)) {
        this.pinned = [...this.pinned, sym];
        localStorage.setItem('ta_pinned', JSON.stringify(this.pinned));
        this.refreshMarqueeStream();
      }
      this.newPin = '';
    },

    removePin(sym) {
      this.pinned = this.pinned.filter(s => s !== sym);
      localStorage.setItem('ta_pinned', JSON.stringify(this.pinned));
      this.refreshMarqueeStream();
    },

    refreshMarqueeStream() {
      if (this._marqueeStream) this._marqueeStream.close();
      this.subscribeMarketStream();
    },

    tickClock() {
      const d = new Date();
      const yyyy = d.getFullYear();
      const mm = String(d.getMonth() + 1).padStart(2, '0');
      const dd = String(d.getDate()).padStart(2, '0');
      const hh = String(d.getHours()).padStart(2, '0');
      const mi = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
      this.clockNow = `${yyyy}-${mm}-${dd} ${hh}:${mi}:${ss} ${tz}`;
    },

    async refreshTicker() {
      // Kept for backwards-compat; not used in live-stream mode.
      try {
        const res = await fetch('/api/movers?n_gainers=10&n_losers=10&pinned=' + encodeURIComponent(this.pinned.join(',')));
        if (!res.ok) return;
        const { feed } = await res.json();
        if (Array.isArray(feed) && feed.length) {
          this.marketTicker = feed.map(x => ({ s: x.s, p: x.p, c: x.c, kind: x.kind || 'item' }));
        }
      } catch (e) {
        console.warn('ticker refresh failed', e);
      }
    },

    async startRun() {
      this.error = '';
      this.log = [];
      this.picks = [];
      this.decisions = {};
      this.running = true;
      this.status = 'pending';

      const symbols = this.form.ticker_source === 'manual'
        ? this.form.symbols_str.split(',').map(s => s.trim()).filter(Boolean)
        : [];

      const body = {
        ticker_source: this.form.ticker_source,
        symbols,
        analysis_date: this.form.analysis_date,
        analysts: this.form.analysts,
        research_depth: this.form.research_depth,
        risk_rounds: this.form.risk_rounds,
        language: this.form.language,
        llm_provider: this.form.llm_provider,
        deep_model: this.form.deep_model,
        quick_model: this.form.quick_model,
        anthropic_effort: this.form.anthropic_effort || null,
      };

      try {
        const res = await fetch('/api/runs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          const txt = await res.text();
          throw new Error(`HTTP ${res.status}: ${txt}`);
        }
        const { run_id } = await res.json();
        this.subscribe(run_id);
      } catch (e) {
        this.error = e.message;
        this.running = false;
        this.status = 'failed';
      }
    },

    subscribe(runId) {
      this.status = 'running';
      const es = new EventSource(`/api/runs/${runId}/events`);

      es.addEventListener('log', (e) => {
        const { line } = JSON.parse(e.data);
        this.appendLog(line);
      });
      es.addEventListener('scanner_picks', (e) => {
        const { picks, market_regime, themes } = JSON.parse(e.data);
        this.picks = picks;
        this.appendLog(`Scanner picks: ${picks.map(p => p.symbol).join(', ')}`);
        if (market_regime) this.appendLog(`Market regime: ${market_regime}`);
        if (themes && themes.length) this.appendLog(`Themes: ${themes.join(', ')}`);
      });
      es.addEventListener('symbol_start', (e) => {
        const { symbol } = JSON.parse(e.data);
        this.appendLog(`\n=== ${symbol} starting ===`);
      });
      es.addEventListener('symbol_done', (e) => {
        const { symbol, decision } = JSON.parse(e.data);
        this.decisions = { ...this.decisions, [symbol]: decision };
        this.appendLog(`=== ${symbol} done ===`);
      });
      es.addEventListener('symbol_error', (e) => {
        const { symbol, error } = JSON.parse(e.data);
        this.appendLog(`!! ${symbol} ERROR: ${error}`);
      });
      es.addEventListener('final_decision', (e) => {
        this.appendLog('\n--- All decisions in ---');
        this.status = 'completed';
        this.running = false;
      });
      es.addEventListener('error', (e) => {
        try {
          const { message } = JSON.parse(e.data);
          this.appendLog(`!! ${message}`);
          this.error = message;
        } catch (_) {}
        this.status = 'failed';
      });
      es.addEventListener('done', () => {
        es.close();
        this.running = false;
      });
      es.onerror = () => {
        // SSE auto-reconnect can fire when stream legit ended; fine to ignore
      };
    },

    appendLog(line) {
      this.log.push(line);
      this.$nextTick(() => {
        const el = this.$refs.logEl;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    async loadChart(symbol) {
      const res = await fetch(`/api/chart/${symbol}?period=6mo`);
      const d = await res.json();
      const traces = [
        {
          x: d.dates, open: d.open, high: d.high, low: d.low, close: d.close,
          type: 'candlestick', name: symbol, yaxis: 'y',
          increasing: { line: { color: '#00ff66' } },
          decreasing: { line: { color: '#ff3b3b' } },
        },
        {
          x: d.dates, y: d.volume, type: 'bar', name: 'Volume',
          marker: { color: '#ffa500' }, yaxis: 'y2', opacity: 0.5,
        },
        { x: d.dates, y: d.rsi, name: 'RSI(14)', yaxis: 'y3', line: { color: '#ffd400', width: 1.5 } },
        { x: d.dates, y: d.macd, name: 'MACD', yaxis: 'y4', line: { color: '#00d4ff', width: 1.5 } },
        { x: d.dates, y: d.macd_signal, name: 'Signal', yaxis: 'y4', line: { color: '#ff5fff', width: 1.5 } },
      ];
      const layout = {
        title: { text: `${symbol}  •  6-MONTH OHLCV / RSI / MACD`, font: { color: '#ffa500', size: 13, family: 'JetBrains Mono, monospace' } },
        height: 720,
        showlegend: true,
        legend: { orientation: 'h', y: -0.15, font: { color: '#d8d8d8', size: 10, family: 'JetBrains Mono, monospace' } },
        paper_bgcolor: '#000',
        plot_bgcolor:  '#000',
        font: { color: '#d8d8d8', family: 'JetBrains Mono, monospace', size: 10 },
        margin: { l: 50, r: 30, t: 40, b: 40 },
        grid: { rows: 4, columns: 1, pattern: 'independent' },
        yaxis:  { domain: [0.55, 1.0], title: 'PRICE', gridcolor: '#222', zerolinecolor: '#333', tickfont: { color: '#ffa500' } },
        yaxis2: { domain: [0.40, 0.55], title: 'VOLUME', gridcolor: '#222', zerolinecolor: '#333', tickfont: { color: '#ffa500' } },
        yaxis3: { domain: [0.20, 0.38], title: 'RSI', gridcolor: '#222', zerolinecolor: '#333', tickfont: { color: '#ffa500' },
                  range: [0, 100] },
        yaxis4: { domain: [0.00, 0.18], title: 'MACD', gridcolor: '#222', zerolinecolor: '#333', tickfont: { color: '#ffa500' } },
        xaxis:  { rangeslider: { visible: false }, gridcolor: '#222', tickfont: { color: '#ffa500' } },
      };
      const cfg = { responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
      Plotly.newPlot(this.$refs.chartEl, traces, layout, cfg);
    },

    async loadHistory() {
      const res = await fetch('/api/runs?limit=100');
      this.history = await res.json();
    },

    async viewRun(id) {
      const res = await fetch(`/api/runs/${id}`);
      const r = await res.json();
      this.page = 'new';
      this.running = false;
      this.status = r.status;
      this.decisions = r.decisions || {};
      this.log = [`Loaded run ${id}`, `Symbols: ${(r.symbols||[]).join(', ')}`,
                  `Date: ${r.analysis_date}`, `Status: ${r.status}`];
      if (r.error) this.log.push(`Error: ${r.error}`);
    },

    async runScanner() {
      this.scanning = true;
      this.scanResult = {};
      try {
        const res = await fetch(`/api/scan?n=${this.scanN}`);
        this.scanResult = await res.json();
      } finally {
        this.scanning = false;
      }
    },
  };
}
