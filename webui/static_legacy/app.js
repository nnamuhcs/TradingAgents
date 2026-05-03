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

    // Per-symbol state — keyed by symbol
    // perSymbol[sym] = { agentStates, messages, reports, decision, done }
    perSymbol: {},
    symbolList: [],            // ordered list of symbols for the tab bar
    currentSymbol: '',         // active tab
    activeStreamSymbol: '',    // symbol the runner is currently processing
    symbolDone: {},            // {sym: true} when symbol_done arrived
    decisions: {},

    // Scanner funnel (live during scan-N runs)
    funnel: [],                // [{layer, name, status, input, output, info}]

    // Live dashboard tabs
    activeTab: 'market_report',
    activeTab: 'market_report',
    agentGroups: [
      { name: 'I. Analysts',     agents: ANALYSTS_GROUP },
      { name: 'II. Research',    agents: RESEARCH_GROUP },
      { name: 'III. Trader',     agents: TRADING_GROUP },
      { name: 'IV. Risk Mgmt',   agents: RISK_GROUP },
    ],
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

    // Final Report modal
    reportModal: { open: false, symbol: '', runId: null, markdown: '', html: '' },

    // Scanner
    scanResult: {},
    scanN: 10,
    scanning: false,
    scanFunnel: [],

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
      const msgs = (this.perSymbol[this.currentSymbol]?.messages) || [];
      return msgs.slice(-80).map(m => ({
        type: m.type,
        short: (m.content || '').length > 600 ? (m.content || '').slice(0, 600) + '…' : (m.content || ''),
      }));
    },

    get renderedReport() {
      const md = this.perSymbol[this.currentSymbol]?.reports?.[this.activeTab];
      if (!md) {
        return `<div style="color:var(--fg-muted)">No content yet for this section.</div>`;
      }
      try {
        return marked.parse(md);
      } catch {
        return `<pre>${this.escape(md)}</pre>`;
      }
    },

    get symbolCount() {
      return (this.form.symbols_str || '').split(',').map(s => s.trim()).filter(Boolean).length;
    },

    barWidth(layer, list) {
      const universe = list || this.funnel;
      const maxIn = Math.max(...universe.map(l => l.input || 0), 1);
      return Math.max(6, ((layer.input || 0) / maxIn) * 100);
    },

    chipClass(layer, sym) {
      if (layer.layer === 4) return sym.picked ? 'picked' : 'dropped';
      if (layer.layer === 3 && sym.has_smart_money) return 'smart';
      if (layer.layer === 2 && sym.has_events) return 'event';
      return '';
    },
    chipTitle(layer, sym) {
      const parts = [`${sym.s} (L${layer.layer})`];
      if (sym.score != null) parts.push(`score=${sym.score}`);
      if (sym.rs_1m != null) parts.push(`rs1m=${sym.rs_1m.toFixed(1)}%`);
      if (sym.rsi != null) parts.push(`rsi=${Math.round(sym.rsi)}`);
      if (sym.vol_ratio != null) parts.push(`vol=${sym.vol_ratio.toFixed(1)}x`);
      if (sym.events && sym.events.length) parts.push(`events: ${sym.events.join(', ')}`);
      if (sym.smart_money && sym.smart_money.length) parts.push(`smart$: ${sym.smart_money.join(', ')}`);
      if (layer.layer === 4) parts.push(sym.picked ? `PICKED [${(sym.conviction||'').toUpperCase()}]` : 'DROPPED');
      return parts.join('  •  ');
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
      this.perSymbol = {};
      this.symbolList = [];
      this.currentSymbol = '';
      this.activeStreamSymbol = '';
      this.symbolDone = {};
      this.decisions = {};
      this.funnel = [];
      this.activeTab = 'market_report';
      this.chartActive = false;
      this.running = true;
      this.status = 'pending';

      // Cap to 5 symbols client-side (server also enforces)
      const symbols = this.form.ticker_source === 'manual'
        ? this.form.symbols_str.split(',').map(s => s.trim()).filter(Boolean).slice(0, 5)
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
        // Pre-create per-symbol state for manual symbols
        for (const s of symbols) this.ensureSymbol(s);
        if (symbols.length) this.currentSymbol = symbols[0];
        this.subscribe(run_id);
      } catch (e) {
        this.error = e.message;
        this.running = false;
        this.status = 'failed';
      }
    },

    ensureSymbol(symbol) {
      if (!symbol) return;
      if (!this.perSymbol[symbol]) {
        this.perSymbol = { ...this.perSymbol, [symbol]: {
          agentStates: {},
          messages: [],
          reports: {},
        }};
        if (!this.symbolList.includes(symbol)) {
          this.symbolList = [...this.symbolList, symbol];
        }
        if (!this.currentSymbol) this.currentSymbol = symbol;
      }
    },

    pushMessage(symbol, type, content) {
      this.ensureSymbol(symbol);
      const sym = symbol || this.currentSymbol || '_global';
      const list = (this.perSymbol[sym]?.messages || []).concat({ type, content });
      this.perSymbol = { ...this.perSymbol, [sym]: {
        ...this.perSymbol[sym],
        messages: list,
      }};
      this.$nextTick(() => {
        const el = this.$refs.messagesEl;
        if (el && sym === this.currentSymbol) el.scrollTop = el.scrollHeight;
      });
    },

    subscribe(runId) {
      this.status = 'running';
      const es = new EventSource(`/api/runs/${runId}/events`);

      const symOf = (data) => data.symbol || this.activeStreamSymbol || this.currentSymbol;

      es.addEventListener('log', (e) => {
        const d = JSON.parse(e.data);
        this.pushMessage(symOf(d) || '_global', 'System', d.line);
      });

      es.addEventListener('agent_states', (e) => {
        const d = JSON.parse(e.data);
        const sym = symOf(d);
        this.ensureSymbol(sym);
        this.perSymbol = { ...this.perSymbol, [sym]: {
          ...this.perSymbol[sym], agentStates: { ...d.states },
        }};
      });
      es.addEventListener('agent_status', (e) => {
        const d = JSON.parse(e.data);
        const sym = symOf(d);
        this.ensureSymbol(sym);
        const cur = this.perSymbol[sym]?.agentStates || {};
        this.perSymbol = { ...this.perSymbol, [sym]: {
          ...this.perSymbol[sym], agentStates: { ...cur, [d.agent]: d.status },
        }};
      });
      es.addEventListener('message', (e) => {
        const d = JSON.parse(e.data);
        this.pushMessage(symOf(d), d.type, d.content);
      });
      es.addEventListener('tool_call', (e) => {
        const d = JSON.parse(e.data);
        const argsStr = (() => { try { return JSON.stringify(d.args); } catch { return String(d.args); } })();
        this.pushMessage(symOf(d), 'Tool', `${d.tool}(${argsStr.slice(0, 240)}${argsStr.length > 240 ? '…' : ''})`);
      });
      es.addEventListener('report_section', (e) => {
        const d = JSON.parse(e.data);
        const sym = symOf(d);
        this.ensureSymbol(sym);
        const cur = this.perSymbol[sym]?.reports || {};
        this.perSymbol = { ...this.perSymbol, [sym]: {
          ...this.perSymbol[sym], reports: { ...cur, [d.section]: d.content },
        }};
        if (sym === this.currentSymbol) this.activeTab = d.section;
      });

      // Scanner funnel
      es.addEventListener('scanner_layer', (e) => {
        const d = JSON.parse(e.data);
        const idx = this.funnel.findIndex(l => l.layer === d.layer);
        if (idx >= 0) {
          const updated = { ...this.funnel[idx], ...d };
          this.funnel = [...this.funnel.slice(0, idx), updated, ...this.funnel.slice(idx + 1)];
        } else {
          this.funnel = [...this.funnel, d];
        }
      });
      es.addEventListener('scanner_picks', (e) => {
        const d = JSON.parse(e.data);
        for (const p of (d.picks || [])) this.ensureSymbol(p.symbol);
        if (this.symbolList.length && !this.currentSymbol) this.currentSymbol = this.symbolList[0];
        this.pushMessage('_global', 'System', `Scanner picks: ${(d.picks||[]).map(p=>p.symbol).join(', ')}`);
        if (d.market_regime) this.pushMessage('_global', 'System', `Market regime: ${d.market_regime}`);
        if (d.themes && d.themes.length) this.pushMessage('_global', 'System', `Themes: ${d.themes.join(', ')}`);
      });

      es.addEventListener('symbol_start', (e) => {
        const { symbol } = JSON.parse(e.data);
        this.ensureSymbol(symbol);
        this.activeStreamSymbol = symbol;
        this.currentSymbol = symbol;          // auto-flip tab to active symbol
        this.activeTab = 'market_report';
        this.pushMessage(symbol, 'System', `── ${symbol} starting ──`);
      });
      es.addEventListener('symbol_done', (e) => {
        const { symbol, decision } = JSON.parse(e.data);
        this.decisions = { ...this.decisions, [symbol]: decision };
        this.symbolDone = { ...this.symbolDone, [symbol]: true };
        this.pushMessage(symbol, 'System', `── ${symbol} done ──`);
      });
      es.addEventListener('symbol_error', (e) => {
        const { symbol, error } = JSON.parse(e.data);
        this.pushMessage(symbol, 'System', `!! ${symbol} ERROR: ${error}`);
      });
      es.addEventListener('final_decision', (e) => {
        this.pushMessage('_global', 'System', '── all decisions in ──');
        this.status = 'completed';
        this.running = false;
      });
      es.addEventListener('error', (e) => {
        try {
          const { message } = JSON.parse(e.data);
          this.pushMessage(this.currentSymbol || '_global', 'System', `ERROR: ${message}`);
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

    async cancelRun(id) {
      if (!confirm('Cancel this run?')) return;
      await fetch(`/api/runs/${id}/cancel`, { method: 'POST' });
      await this.loadHistory();
    },

    // ─────────────────── full report ───────────────────
    async openReport(symbol, runId = null) {
      // For the live page we don't yet know the run_id explicitly; pull
      // the most recent matching run from history if not supplied.
      if (!runId) {
        const res = await fetch('/api/runs?limit=20');
        const runs = await res.json();
        const found = runs.find(r => (r.symbols || []).includes(symbol));
        if (!found) {
          alert('No run found for ' + symbol);
          return;
        }
        runId = found.id;
      }

      const res = await fetch(`/api/runs/${runId}/report?symbol=${encodeURIComponent(symbol)}`);
      if (!res.ok) {
        alert(`Failed to load report (HTTP ${res.status})`);
        return;
      }
      const data = await res.json();
      const r = (data.reports || [])[0];
      if (!r) {
        alert('No report data for ' + symbol);
        return;
      }
      this.reportModal = {
        open: true,
        symbol,
        runId,
        markdown: r.markdown || '',
        html: marked.parse(r.markdown || ''),
      };
    },

    async downloadReport(symbol, runId = null) {
      if (!runId) {
        const res = await fetch('/api/runs?limit=20');
        const runs = await res.json();
        const found = runs.find(r => (r.symbols || []).includes(symbol));
        if (!found) { alert('No run found for ' + symbol); return; }
        runId = found.id;
      }
      const url = `/api/runs/${runId}/report.md?symbol=${encodeURIComponent(symbol)}`;
      const a = document.createElement('a');
      a.href = url;
      a.download = '';
      document.body.appendChild(a);
      a.click();
      a.remove();
    },

    async copyReport() {
      try {
        await navigator.clipboard.writeText(this.reportModal.markdown || '');
        alert('Markdown copied to clipboard');
      } catch (e) {
        alert('Copy failed: ' + e.message);
      }
    },

    printReport() {
      const w = window.open('', '_blank');
      if (!w) return;
      w.document.write(`
        <html><head><title>${this.reportModal.symbol} — TradingAgents Report</title>
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                 max-width: 900px; margin: 24px auto; padding: 0 24px; color: #111; }
          h1, h2 { border-bottom: 1px solid #ccc; padding-bottom: 6px; }
          h1 { font-size: 24px; } h2 { font-size: 18px; margin-top: 24px; }
          h3, h4 { margin-top: 16px; }
          code { background: #f4f4f4; padding: 1px 5px; border-radius: 3px; }
          pre  { background: #f4f4f4; padding: 10px; border-radius: 4px; overflow: auto; }
          blockquote { border-left: 3px solid #999; padding-left: 12px; color: #555; }
          table { border-collapse: collapse; } th, td { border: 1px solid #999; padding: 4px 8px; }
        </style></head><body>${this.reportModal.html}</body></html>`);
      w.document.close();
      setTimeout(() => w.print(), 300);
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
      this.symbolList = (r.symbols || []).slice();
      this.perSymbol = {};
      for (const s of this.symbolList) {
        this.perSymbol[s] = {
          agentStates: {},
          messages: [
            { type: 'System', content: `Loaded run ${id}` },
            { type: 'System', content: `Symbol: ${s}` },
            { type: 'System', content: `Date: ${r.analysis_date}` },
            { type: 'System', content: `Status: ${r.status}` },
          ],
          reports: ((r.reports || {})[s]) || {},
        };
        this.symbolDone[s] = r.status === 'completed';
      }
      if (r.error) {
        for (const s of this.symbolList) {
          this.perSymbol[s].messages.push({ type: 'System', content: `Error: ${r.error}` });
        }
      }
      if (this.symbolList.length) this.currentSymbol = this.symbolList[0];
      this.activeStreamSymbol = '';
      this.funnel = [];
      this.activeTab = 'market_report';
    },

    // ─────────────────── scanner ───────────────────
    runScanner() {
      this.scanning = true;
      this.scanResult = {};
      this.scanFunnel = [];

      const es = new EventSource(`/api/scan/stream?n=${this.scanN}`);

      es.addEventListener('scanner_layer', (e) => {
        const d = JSON.parse(e.data);
        const idx = this.scanFunnel.findIndex(l => l.layer === d.layer);
        if (idx >= 0) {
          const updated = { ...this.scanFunnel[idx], ...d };
          this.scanFunnel = [
            ...this.scanFunnel.slice(0, idx),
            updated,
            ...this.scanFunnel.slice(idx + 1),
          ];
        } else {
          this.scanFunnel = [...this.scanFunnel, d];
        }
      });
      es.addEventListener('picks', (e) => {
        this.scanResult = JSON.parse(e.data);
      });
      es.addEventListener('error', (e) => {
        try {
          const d = JSON.parse(e.data);
          this.scanResult = { error: d.message || 'Scanner error' };
        } catch (_) {}
        this.scanning = false;
      });
      es.addEventListener('done', () => {
        es.close();
        this.scanning = false;
      });
      es.onerror = () => { /* SSE auto-reconnect */ };
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
