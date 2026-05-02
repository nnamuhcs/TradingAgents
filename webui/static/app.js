function app() {
  const ANALYSTS_GROUP = ['Market Analyst', 'Social Analyst', 'News Analyst', 'Fundamentals Analyst'];
  const RESEARCH_GROUP = ['Bull Researcher', 'Bear Researcher', 'Research Manager'];
  const TRADING_GROUP = ['Trader'];
  const RISK_GROUP = ['Aggressive Analyst', 'Neutral Analyst', 'Conservative Analyst', 'Portfolio Manager'];

  return {
    page: 'new',
    running: false,
    error: '',
    status: 'idle',
    currentSymbol: '',

    // Live dashboard state
    agentGroups: [
      { name: 'I. Analysts',     agents: ANALYSTS_GROUP },
      { name: 'II. Research',    agents: RESEARCH_GROUP },
      { name: 'III. Trader',     agents: TRADING_GROUP },
      { name: 'IV. Risk Mgmt',   agents: RISK_GROUP },
    ],
    agentStates: {},

    messages: [],
    decisions: {},

    reports: {},
    activeTab: 'market_report',
    reportTabs: [
      { key: 'market_report',           label: 'Market' },
      { key: 'sentiment_report',        label: 'Social' },
      { key: 'news_report',             label: 'News' },
      { key: 'fundamentals_report',     label: 'Fundamentals' },
      { key: 'investment_plan',         label: 'Research' },
      { key: 'trader_investment_plan',  label: 'Trader' },
      { key: 'final_trade_decision',    label: 'Risk / Decision' },
    ],
    chartActive: false,

    // History
    history: [],

    // Scanner
    scanResult: {},
    scanN: 10,
    scanning: false,

    // Marquee
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
      language: 'English',
      llm_provider: 'github-copilot',
      deep_model: 'claude-opus-4.7',
      quick_model: 'claude-opus-4.7',
      anthropic_effort: 'high',
    },

    // ─────────────────── lifecycle ───────────────────
    init() {
      this.tickClock();
      setInterval(() => this.tickClock(), 1000);
      this.subscribeMarketStream();
    },

    get visibleMessages() {
      // Render last 80 messages with shortened body
      return this.messages.slice(-80).map(m => ({
        type: m.type,
        short: (m.content || '').length > 600 ? (m.content || '').slice(0, 600) + '…' : (m.content || ''),
      }));
    },

    get renderedReport() {
      const md = this.reports[this.activeTab];
      if (!md) {
        return `<div style="color:var(--fg-muted)">No content yet for this section.</div>`;
      }
      try {
        return marked.parse(md);
      } catch {
        return `<pre>${this.escape(md)}</pre>`;
      }
    },

    escape(s) { return (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); },

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

    // ─────────────────── runs ───────────────────
    async startRun() {
      this.error = '';
      this.messages = [];
      this.reports = {};
      this.agentStates = {};
      this.decisions = {};
      this.currentSymbol = '';
      this.activeTab = 'market_report';
      this.chartActive = false;
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
        risk_rounds: this.form.research_depth,
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
        this.pushMessage('System', line);
      });

      es.addEventListener('agent_states', (e) => {
        const { states } = JSON.parse(e.data);
        this.agentStates = { ...states };
      });
      es.addEventListener('agent_status', (e) => {
        const { agent, status } = JSON.parse(e.data);
        this.agentStates = { ...this.agentStates, [agent]: status };
      });
      es.addEventListener('message', (e) => {
        const { type, content, symbol } = JSON.parse(e.data);
        if (symbol) this.currentSymbol = symbol;
        this.pushMessage(type, content);
      });
      es.addEventListener('tool_call', (e) => {
        const { tool, args } = JSON.parse(e.data);
        const argsStr = (() => { try { return JSON.stringify(args); } catch { return String(args); } })();
        this.pushMessage('Tool', `${tool}(${argsStr.slice(0, 240)}${argsStr.length > 240 ? '…' : ''})`);
      });
      es.addEventListener('report_section', (e) => {
        const { section, content } = JSON.parse(e.data);
        this.reports = { ...this.reports, [section]: content };
        this.activeTab = section;
      });
      es.addEventListener('scanner_picks', (e) => {
        const { picks, market_regime, themes } = JSON.parse(e.data);
        this.pushMessage('System', `Scanner picks: ${picks.map(p => p.symbol).join(', ')}`);
        if (market_regime) this.pushMessage('System', `Market regime: ${market_regime}`);
        if (themes && themes.length) this.pushMessage('System', `Themes: ${themes.join(', ')}`);
      });

      es.addEventListener('symbol_start', (e) => {
        const { symbol } = JSON.parse(e.data);
        this.currentSymbol = symbol;
        this.pushMessage('System', `── ${symbol} starting ──`);
        // reset per-symbol state but keep cumulative messages
        this.agentStates = {};
        this.reports = {};
        this.activeTab = 'market_report';
      });
      es.addEventListener('symbol_done', (e) => {
        const { symbol, decision } = JSON.parse(e.data);
        this.decisions = { ...this.decisions, [symbol]: decision };
        this.pushMessage('System', `── ${symbol} done ──`);
      });
      es.addEventListener('symbol_error', (e) => {
        const { symbol, error } = JSON.parse(e.data);
        this.pushMessage('System', `!! ${symbol} ERROR: ${error}`);
      });
      es.addEventListener('final_decision', (e) => {
        this.pushMessage('System', '── all decisions in ──');
        this.status = 'completed';
        this.running = false;
      });
      es.addEventListener('error', (e) => {
        try {
          const { message } = JSON.parse(e.data);
          this.pushMessage('System', `ERROR: ${message}`);
          this.error = message;
        } catch (_) {}
        this.status = 'failed';
        this.running = false;
      });
      es.addEventListener('done', () => {
        es.close();
        this.running = false;
      });
      es.onerror = () => { /* auto-reconnect handles transient */ };
    },

    pushMessage(type, content) {
      this.messages = [...this.messages, { type, content }];
      this.$nextTick(() => {
        const el = this.$refs.messagesEl;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    async cancelRun(id) {
      if (!confirm('Cancel this run?')) return;
      await fetch(`/api/runs/${id}/cancel`, { method: 'POST' });
      await this.loadHistory();
    },

    // ─────────────────── chart ───────────────────
    async loadChart(symbol) {
      this.chartActive = true;
      const res = await fetch(`/api/chart/${encodeURIComponent(symbol)}?period=6mo`);
      const d = await res.json();
      const traces = [
        {
          x: d.dates, open: d.open, high: d.high, low: d.low, close: d.close,
          type: 'candlestick', name: symbol,
          increasing: { line: { color: '#3fb950' } },
          decreasing: { line: { color: '#f85149' } },
        },
        {
          x: d.dates, y: d.volume, type: 'bar', name: 'Volume',
          marker: { color: '#4ec9b0' }, yaxis: 'y2', opacity: 0.5,
        },
        { x: d.dates, y: d.rsi, name: 'RSI(14)', yaxis: 'y3', line: { color: '#d29922', width: 1.5 } },
        { x: d.dates, y: d.macd, name: 'MACD', yaxis: 'y4', line: { color: '#58a6ff', width: 1.5 } },
        { x: d.dates, y: d.macd_signal, name: 'Signal', yaxis: 'y4', line: { color: '#c678dd', width: 1.5 } },
      ];
      const layout = {
        title: { text: `${symbol} — 6-month OHLCV / RSI / MACD`,
                 font: { color: '#c9d1d9', size: 13, family: 'Inter, sans-serif' } },
        height: 720,
        showlegend: true,
        legend: { orientation: 'h', y: -0.15,
                  font: { color: '#c9d1d9', size: 10, family: 'Inter, sans-serif' } },
        paper_bgcolor: '#0a0e14',
        plot_bgcolor:  '#0a0e14',
        font: { color: '#c9d1d9', family: 'Inter, sans-serif', size: 10 },
        margin: { l: 50, r: 30, t: 40, b: 40 },
        grid: { rows: 4, columns: 1, pattern: 'independent' },
        yaxis:  { domain: [0.55, 1.0], title: 'Price', gridcolor: '#1f2733', zerolinecolor: '#2a3544' },
        yaxis2: { domain: [0.40, 0.55], title: 'Volume', gridcolor: '#1f2733', zerolinecolor: '#2a3544' },
        yaxis3: { domain: [0.20, 0.38], title: 'RSI', gridcolor: '#1f2733', zerolinecolor: '#2a3544', range: [0, 100] },
        yaxis4: { domain: [0.00, 0.18], title: 'MACD', gridcolor: '#1f2733', zerolinecolor: '#2a3544' },
        xaxis:  { rangeslider: { visible: false }, gridcolor: '#1f2733' },
      };
      const cfg = { responsive: true, displaylogo: false,
                    modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
      Plotly.newPlot(this.$refs.chartEl, traces, layout, cfg);
    },

    // ─────────────────── history ───────────────────
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
      this.messages = [
        { type: 'System', content: `Loaded run ${id}` },
        { type: 'System', content: `Symbols: ${(r.symbols||[]).join(', ')}` },
        { type: 'System', content: `Date: ${r.analysis_date}` },
        { type: 'System', content: `Status: ${r.status}` },
      ];
      if (r.error) this.messages.push({ type: 'System', content: `Error: ${r.error}` });
    },

    // ─────────────────── scanner ───────────────────
    async runScanner() {
      this.scanning = true;
      this.scanResult = {};
      try {
        const res = await fetch(`/api/scan?n=${this.scanN}`);
        this.scanResult = await res.json();
      } finally { this.scanning = false; }
    },

    // ─────────────────── marquee ───────────────────
    subscribeMarketStream() {
      const params = new URLSearchParams({ pinned: this.pinned.join(',') });
      const es = new EventSource(`/api/movers/stream?${params}`);
      es.addEventListener('snapshot', (e) => {
        try {
          const { feed, live } = JSON.parse(e.data);
          this.liveStreamConnected = !!live;
          if (Array.isArray(feed) && feed.length) {
            this.marketTicker = feed.map(x => ({
              s: x.s, p: x.p, c: x.c, kind: x.kind || 'item', live: !!x.live,
            }));
          }
        } catch (err) { console.warn('marquee snapshot parse error', err); }
      });
      es.onerror = () => { /* auto-reconnect */ };
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
  };
}
