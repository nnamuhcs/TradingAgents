/* ============================================================
   VERVE TRADING AGENTS — floor.js
   View-swap state machine + SSE choreography.
   Wires against the existing FastAPI endpoints:
     POST /api/runs                          - start a run
     GET  /api/runs/{id}/events  (SSE)       - live event stream
     GET  /api/runs/{id}/report?symbol=X     - assembled report
     GET  /api/movers/stream      (SSE)      - marquee feed
   ============================================================ */

(() => {
  'use strict';

  /* -------- AGENT ROSTER (fixed shape) -------- */
  const AGENTS = {
    analysts: [
      { key: 'market',       name: 'Market' },
      { key: 'fundamentals', name: 'Fundamentals' },
      { key: 'news',         name: 'News' },
      { key: 'social',       name: 'Sentiment' },
    ],
    research: [
      { key: 'bull', name: 'Bull' },
      { key: 'bear', name: 'Bear' },
    ],
    risk: [
      { key: 'trader',       name: 'Trader' },
      { key: 'risk',         name: 'Risk panel' },
      { key: 'portfolio',    name: 'Portfolio mgr' },
    ],
  };
  const ALL_AGENTS = [
    ...AGENTS.analysts,
    ...AGENTS.research,
    ...AGENTS.risk,
  ];
  const REPORT_TABS = [
    { key: 'market',          label: 'Market' },
    { key: 'sentiment',       label: 'Sentiment' },
    { key: 'news',            label: 'News' },
    { key: 'fundamentals',    label: 'Fundamentals' },
    { key: 'investment_plan', label: 'Investment plan' },
    { key: 'trader_plan',     label: 'Trader plan' },
    { key: 'final_decision',  label: 'Final decision' },
  ];
  const VERDICT_CLASS = {
    'Buy':         'buy',
    'Overweight':  'overweight',
    'Hold':        'hold',
    'Underweight': 'underweight',
    'Sell':        'sell',
  };
  const VERDICT_DROP_CLASS = {
    'Buy':         'just-landed-bull',
    'Overweight':  'just-landed-bull',
    'Hold':        'just-landed-neutral',
    'Underweight': 'just-landed-bear',
    'Sell':        'just-landed-bear',
  };

  /* -------- STATE -------- */
  const state = {
    view: 'home',           // 'home' | 'desk' | 'stage'
    runId: null,
    symbols: [],            // ['NVDA','AAPL',...]
    activeSymbol: null,     // for stage view
    activeReportTab: 'market',
    bySymbol: {},           // symbol -> { agents, messages, reports, conviction, prevConviction, verdict, flipped, debateRound, totalRounds, price, priceDelta }
    elapsedStart: null,
    marqueeES: null,
    runES: null,
  };

  function makeSymbolState() {
    return {
      agents: Object.fromEntries(ALL_AGENTS.map(a => [a.key, 'wait'])), // wait | live | done
      messages: [],
      reports: {},
      conviction: 0,
      prevConviction: null,
      verdict: null,
      flipped: false,
      debateRound: 0,
      totalRounds: 1,
      price: null,
      priceDelta: null,
      done: false,
    };
  }

  /* -------- DOM HELPERS -------- */
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') node.className = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') {
        node.addEventListener(k.slice(2), v);
      } else if (v !== null && v !== undefined && v !== false) {
        node.setAttribute(k, v);
      }
    }
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(child));
    }
    return node;
  };

  /* -------- TIME -------- */
  const fmtClock = (ms) => {
    const total = Math.floor(ms / 1000);
    const m = String(Math.floor(total / 60)).padStart(2, '0');
    const s = String(total % 60).padStart(2, '0');
    return `${m}:${s}`;
  };
  const fmtTimeOfDay = (date = new Date()) =>
    date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

  /* -------- TYPE/HIGHLIGHT helpers (text emphasis on numbers) -------- */
  const highlightNumbers = (text) => {
    if (!text) return '';
    // Wrap percentages, dollar amounts, integers >= 100, and decimals like 7.4
    const safe = text.replace(/[<>&]/g, s => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[s]));
    return safe.replace(
      /(\$[\d,]+(?:\.\d+)?|\d+(?:\.\d+)?%|\d+(?:\.\d+)?\s*(?:bps|x|×)|\d+(?:\.\d+)?\s*YoY|\b\d{3,}(?:,\d{3})*\b)/g,
      '<em>$1</em>'
    );
  };
  const bylineClassFor = (agentKey) => {
    if (agentKey === 'bull')          return 'bull';
    if (agentKey === 'bear')          return 'bear';
    if (agentKey === 'market')        return 'market';
    if (agentKey === 'fundamentals')  return 'fund';
    if (agentKey === 'news')          return 'news';
    if (agentKey === 'social')        return 'sent';
    if (agentKey === 'tool')          return 'tool';
    return 'system';
  };
  const bylineLabelFor = (agentKey, msg) => {
    const a = ALL_AGENTS.find(x => x.key === agentKey);
    if (a) {
      if ((agentKey === 'bull' || agentKey === 'bear') && msg.round) {
        return `${a.name} researcher · round ${msg.round}`;
      }
      return a.name + ' analyst';
    }
    if (agentKey === 'tool') return 'Tool call';
    return agentKey || 'System';
  };

  /* ============================================================
     MARQUEE
     ============================================================ */
  function initMarquee() {
    const track = $('#v-marquee-track');
    if (!track) return;
    // Try the SSE feed first, fall back gracefully
    try {
      const es = new EventSource('/api/movers/stream?pinned=');
      state.marqueeES = es;
      es.addEventListener('snapshot', (ev) => {
        try {
          const data = JSON.parse(ev.data);
          renderMarquee(track, data);
        } catch (_) { /* ignore */ }
      });
      es.addEventListener('update', (ev) => {
        try {
          const data = JSON.parse(ev.data);
          updateMarqueeTick(track, data);
        } catch (_) { /* ignore */ }
      });
      es.addEventListener('error', () => {
        // SSE failed — render static placeholder so the bar isn't empty
        renderMarqueePlaceholder(track);
      });
    } catch (_) {
      renderMarqueePlaceholder(track);
    }
  }

  function renderMarquee(track, data) {
    track.innerHTML = '';
    const sections = [
      { label: 'Anchors', items: data.anchors || [] },
      { label: 'Pinned',  items: data.pinned  || [] },
      { label: 'Movers',  items: data.movers  || [] },
    ].filter(s => s.items.length);

    const buildSection = (s) => {
      const frag = document.createDocumentFragment();
      frag.append(el('span', { class: 'v-marquee-section' }, s.label));
      s.items.forEach((tk, i) => {
        if (i > 0) frag.append(el('span', { class: 'v-marquee-divider' }, '·'));
        frag.append(buildTick(tk));
      });
      return frag;
    };
    sections.forEach(s => track.append(buildSection(s)));
    // Duplicate for seamless loop
    sections.forEach(s => track.append(buildSection(s)));
  }

  function buildTick(tk) {
    const dt = tk.changePct ?? 0;
    const sign = dt > 0 ? '+' : '';
    const cls = dt > 0.05 ? 'up' : dt < -0.05 ? 'down' : 'flat';
    const arrow = dt > 0.05 ? '▲' : dt < -0.05 ? '▼' : '–';
    return el('span', { class: 'v-tick', 'data-symbol': tk.symbol },
      el('span', { class: 'v-tick-sym' }, tk.symbol),
      el('span', { class: 'v-tick-px' }, fmtPx(tk.price)),
      el('span', { class: `v-tick-dt ${cls}` }, `${arrow} ${sign}${dt.toFixed(2)}%`),
    );
  }

  function fmtPx(p) {
    if (p === null || p === undefined) return '—';
    if (p > 1000) return p.toLocaleString(undefined, { maximumFractionDigits: 0 });
    return p.toFixed(2);
  }

  function updateMarqueeTick(track, tk) {
    if (!tk || !tk.symbol) return;
    const els = $$(`.v-tick[data-symbol="${tk.symbol}"]`, track);
    els.forEach(node => {
      const px = $('.v-tick-px', node);
      const dt = $('.v-tick-dt', node);
      if (px) px.textContent = fmtPx(tk.price);
      if (dt) {
        const d = tk.changePct ?? 0;
        const sign = d > 0 ? '+' : '';
        const cls = d > 0.05 ? 'up' : d < -0.05 ? 'down' : 'flat';
        const arrow = d > 0.05 ? '▲' : d < -0.05 ? '▼' : '–';
        dt.className = `v-tick-dt ${cls}`;
        dt.textContent = `${arrow} ${sign}${d.toFixed(2)}%`;
      }
    });
  }

  function renderMarqueePlaceholder(track) {
    const placeholder = [
      { symbol: 'SPY',   price: 582.41, changePct:  0.42 },
      { symbol: 'QQQ',   price: 512.08, changePct:  0.71 },
      { symbol: '^VIX',  price:  14.22, changePct: -2.10 },
      { symbol: 'BTC',   price: 98420,  changePct:  1.84 },
    ];
    renderMarquee(track, { anchors: placeholder, pinned: [], movers: [] });
  }

  function flareMarquee() {
    const dot = $('.v-pulse-dot');
    if (!dot) return;
    dot.classList.remove('flare');
    void dot.offsetWidth; // restart animation
    dot.classList.add('flare');
    setTimeout(() => dot.classList.remove('flare'), 700);
  }

  /* ============================================================
     RUN START + SSE STREAM
     ============================================================ */
  async function startRun(payload) {
    const res = await fetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`Run failed to start: ${res.status}`);
    const data = await res.json();
    return data;
  }

  function attachToRun(runId, presetSymbols = []) {
    closeRunStream();
    state.runId = runId;
    state.symbols = presetSymbols.slice();
    state.bySymbol = {};
    presetSymbols.forEach(s => { state.bySymbol[s] = makeSymbolState(); });
    state.activeSymbol = presetSymbols[0] || null;
    state.elapsedStart = Date.now();

    const es = new EventSource(`/api/runs/${runId}/events`);
    state.runES = es;
    const evtTypes = [
      'symbol_start', 'symbol_done', 'agent_status',
      'message', 'tool_call', 'report_section',
      'conviction', 'debate_round', 'decision',
      'price', 'scanner_layer', 'run_done', 'error',
    ];
    evtTypes.forEach(t => es.addEventListener(t, (ev) => handleSseEvent(t, ev)));
    es.onerror = () => { /* let browser retry */ };

    showView('desk');
    startElapsedTicker();
  }

  function closeRunStream() {
    if (state.runES) { state.runES.close(); state.runES = null; }
    stopElapsedTicker();
  }

  function handleSseEvent(type, ev) {
    let data;
    try { data = JSON.parse(ev.data); } catch (_) { return; }
    const sym = data.symbol;
    if (sym && !state.bySymbol[sym]) {
      state.bySymbol[sym] = makeSymbolState();
      if (!state.symbols.includes(sym)) state.symbols.push(sym);
    }
    const s = sym ? state.bySymbol[sym] : null;

    switch (type) {
      case 'symbol_start':
        if (s) { /* nothing more — already have state */ }
        break;
      case 'agent_status':
        if (s && data.agent) {
          s.agents[data.agent] = data.status; // 'live' | 'done' | 'wait'
        }
        break;
      case 'message':
        if (s) {
          s.messages.push({
            agent: data.agent,
            text: data.text,
            round: data.round,
            time: new Date(),
            kind: 'message',
          });
        }
        break;
      case 'tool_call':
        if (s) {
          s.messages.push({
            agent: 'tool',
            text: data.text || `${data.name}(${data.args || ''})`,
            time: new Date(),
            kind: 'tool',
          });
        }
        break;
      case 'report_section':
        if (s && data.section) {
          s.reports[data.section] = data.content || '';
        }
        break;
      case 'conviction':
        if (s) {
          s.prevConviction = s.conviction;
          s.conviction = data.score ?? s.conviction;
          // Flip detection: crossing 5.0 in either direction
          if (s.prevConviction !== null) {
            const wasBear = s.prevConviction < 5.0;
            const isBull  = s.conviction >= 5.0;
            if (wasBear !== isBull && Math.abs(s.conviction - s.prevConviction) > 0.5) {
              s.flipped = true;
              setTimeout(() => { s.flipped = false; renderDeskRows(); }, 30000);
            }
          }
        }
        break;
      case 'debate_round':
        if (s) {
          s.debateRound = data.round ?? s.debateRound;
          s.totalRounds = data.total ?? s.totalRounds;
        }
        break;
      case 'price':
        if (s) {
          s.price = data.price;
          s.priceDelta = data.changePct;
        }
        break;
      case 'decision':
        if (s) {
          s.verdict = data.rating;
          s.done = true;
          // Flag for one-shot animation; cleared after render
          s._justLanded = true;
          flareMarquee();
        }
        break;
      case 'symbol_done':
        if (s) s.done = true;
        break;
      case 'run_done':
        closeRunStream();
        break;
    }

    // Render the active surfaces
    if (state.view === 'desk') renderDeskRows();
    if (state.view === 'stage' && state.activeSymbol === sym) renderStage();
  }

  /* ============================================================
     ELAPSED TIMER
     ============================================================ */
  let _elapsedTimer = null;
  function startElapsedTicker() {
    stopElapsedTicker();
    _elapsedTimer = setInterval(() => {
      const target = $('#v-elapsed');
      if (target && state.elapsedStart) {
        target.textContent = fmtClock(Date.now() - state.elapsedStart);
      }
    }, 1000);
  }
  function stopElapsedTicker() {
    if (_elapsedTimer) { clearInterval(_elapsedTimer); _elapsedTimer = null; }
  }

  /* ============================================================
     VIEW SWITCHING
     ============================================================ */
  function showView(name) {
    state.view = name;
    $$('.v-view').forEach(v => v.classList.add('v-hidden'));
    const target = $(`#v-view-${name}`);
    if (target) target.classList.remove('v-hidden');
    $$('.v-nav-item').forEach(b => {
      b.classList.toggle('active', b.dataset.view === name);
    });
    if (name === 'desk') renderDesk();
    if (name === 'stage') renderStage();
    if (name === 'home') renderHome();
  }

  /* ============================================================
     HOME (form)
     ============================================================ */
  function renderHome() {
    const form = $('#v-home');
    if (!form) return;
    if (form.dataset.built) return;
    form.dataset.built = '1';
    form.append(buildForm());
  }

  function buildForm() {
    const wrap = el('div', { class: 'v-form-card' });
    wrap.append(el('div', { class: 'v-section-label' }, 'New run'));

    const grid = el('div', { class: 'v-form-grid' });

    // Ticker source
    const fieldSrc = el('div', { class: 'v-form-field' });
    fieldSrc.append(el('label', { class: 'v-form-label' }, 'Ticker source'));
    const sel = el('select', { class: 'v-select', id: 'v-ticker-source' });
    [
      ['manual',  'Manual'],
      ['scan-3',  'Scanner: top 3'],
      ['scan-5',  'Scanner: top 5'],
    ].forEach(([v, t]) => sel.append(el('option', { value: v }, t)));
    fieldSrc.append(sel);
    grid.append(fieldSrc);

    // Symbols
    const fieldSym = el('div', { class: 'v-form-field' });
    fieldSym.append(el('label', { class: 'v-form-label' }, 'Symbols'));
    fieldSym.append(el('input', {
      class: 'v-input',
      id: 'v-symbols',
      placeholder: 'NVDA, AAPL, MSFT',
      value: 'NVDA',
    }));
    grid.append(fieldSym);

    // Date
    const fieldDate = el('div', { class: 'v-form-field' });
    fieldDate.append(el('label', { class: 'v-form-label' }, 'Analysis date'));
    fieldDate.append(el('input', {
      class: 'v-input',
      id: 'v-date',
      type: 'date',
      value: new Date().toISOString().slice(0, 10),
    }));
    grid.append(fieldDate);

    // Depth
    const fieldDepth = el('div', { class: 'v-form-field' });
    fieldDepth.append(el('label', { class: 'v-form-label' }, 'Research depth'));
    const selDepth = el('select', { class: 'v-select', id: 'v-depth' });
    [['shallow', 'Shallow · 1 round'], ['medium', 'Medium · 3 rounds'], ['deep', 'Deep · 5 rounds']]
      .forEach(([v, t]) => selDepth.append(el('option', { value: v, selected: v === 'medium' }, t)));
    fieldDepth.append(selDepth);
    grid.append(fieldDepth);

    wrap.append(grid);

    // Analyst checkboxes
    const analystRow = el('div', { class: 'v-form-field', style: 'margin-bottom: 18px;' });
    analystRow.append(el('label', { class: 'v-form-label' }, 'Analysts'));
    const checks = el('div', { class: 'v-checkboxes' });
    [
      ['market', 'Market'],
      ['social', 'Sentiment'],
      ['news', 'News'],
      ['fundamentals', 'Fundamentals'],
    ].forEach(([k, label]) => {
      const lab = el('label', { class: 'v-checkbox' });
      lab.append(el('input', { type: 'checkbox', name: 'analyst', value: k, checked: 'checked' }));
      lab.append(document.createTextNode(label));
      checks.append(lab);
    });
    analystRow.append(checks);
    wrap.append(analystRow);

    // Submit
    const actions = el('div', { class: 'v-form-actions' });
    actions.append(el('div', { style: 'font-size: 11px; color: var(--v-text-4); letter-spacing: 0.18em; text-transform: uppercase;' }, 'The floor convenes when you start'));
    const btn = el('button', { class: 'v-start-btn', id: 'v-start' }, 'Start analysis →');
    btn.addEventListener('click', onStartClick);
    actions.append(btn);
    wrap.append(actions);

    return wrap;
  }

  async function onStartClick() {
    const btn = $('#v-start');
    btn.disabled = true;
    btn.textContent = 'Starting…';
    try {
      const symbolsRaw = $('#v-symbols').value.trim();
      const symbols = symbolsRaw.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
      const tickerSource = $('#v-ticker-source').value;
      const date = $('#v-date').value;
      const depth = $('#v-depth').value;
      const analysts = $$('input[name="analyst"]:checked').map(c => c.value);
      const depthRounds = { shallow: 1, medium: 3, deep: 5 }[depth] || 1;

      const data = await startRun({
        ticker_source: tickerSource,
        symbols,
        analysis_date: date,
        analysts,
        max_debate_rounds: depthRounds,
        max_risk_discuss_rounds: depthRounds,
      });
      attachToRun(data.run_id, tickerSource === 'manual' ? symbols : []);
    } catch (err) {
      console.error(err);
      btn.textContent = 'Failed — try again';
      setTimeout(() => { btn.disabled = false; btn.textContent = 'Start analysis →'; }, 2000);
    }
  }

  /* ============================================================
     DESK (multi-symbol overview)
     ============================================================ */
  function renderDesk() {
    const root = $('#v-desk');
    if (!root) return;
    root.innerHTML = '';

    const nSym = state.symbols.length;
    const liveCount = state.symbols.filter(s => !state.bySymbol[s].done).length;

    const head = el('div', { class: 'v-desk-head' });
    const titleWrap = el('div');
    titleWrap.append(el('div', { class: 'v-desk-title', html: `Floor · <b>${nSym} ${nSym === 1 ? 'symbol' : 'symbols'}</b>` }));
    titleWrap.append(el('div', { class: 'v-desk-sub' }, `Run ${state.runId || '—'}`));
    head.append(titleWrap);

    const status = el('div', { class: 'v-desk-status' });
    if (liveCount > 0) {
      status.append(el('span', { class: 'v-pulse-dot', style: 'flex-shrink:0;' }));
      status.append(document.createTextNode(`${liveCount} of ${nSym} in progress · `));
      status.append(el('span', { id: 'v-elapsed' }, '00:00'));
    } else if (nSym > 0) {
      status.append(document.createTextNode(`Run complete · ${fmtClock(Date.now() - (state.elapsedStart || Date.now()))}`));
    }
    head.append(status);
    root.append(head);

    if (nSym === 0) {
      root.append(el('div', { class: 'v-empty' },
        el('div', { class: 'v-empty-title' }, 'No active run'),
        el('div', { class: 'v-empty-body' },
          'Start an analysis from the home tab. The floor convenes when the first symbol begins, and you\u2019ll see every analyst, researcher, and risk-panel member arrive in real time.'),
      ));
      return;
    }

    // Column header
    const ch = el('div', { class: 'v-desk-colhead' });
    ['Symbol', 'Pipeline', 'Latest argument', 'Conviction'].forEach(t =>
      ch.append(el('span', {}, t)),
    );
    ch.append(el('span', { class: 'v-right' }, 'Verdict'));
    root.append(ch);

    // Rows container
    const list = el('div', { id: 'v-desk-list' });
    root.append(list);

    // Summary
    const summary = el('div', { class: 'v-summary', id: 'v-desk-summary' });
    root.append(summary);

    renderDeskRows();
  }

  function renderDeskRows() {
    const list = $('#v-desk-list');
    if (!list) return;
    list.innerHTML = '';
    state.symbols.forEach(sym => list.append(buildDeskRow(sym)));
    renderDeskSummary();
  }

  function buildDeskRow(sym) {
    const s = state.bySymbol[sym];
    const isLive = !s.done && Object.values(s.agents).some(v => v === 'live');
    const justLanded = s._justLanded ? VERDICT_DROP_CLASS[s.verdict] : '';
    if (s._justLanded) s._justLanded = false; // one-shot

    const cls = ['v-row'];
    if (isLive) cls.push('live');
    if (s.flipped) cls.push('flipped');
    if (justLanded) cls.push(justLanded);

    const row = el('div', { class: cls.join(' '), 'data-symbol': sym });
    row.addEventListener('click', () => selectSymbol(sym));

    // Symbol cell
    const pxClass = s.priceDelta == null ? '' : s.priceDelta >= 0 ? 'up' : 'down';
    const pxArrow = s.priceDelta == null ? '' : s.priceDelta >= 0 ? '▲' : '▼';
    const pxText = s.price == null ? '—' :
      `$${fmtPx(s.price)}${s.priceDelta != null ? ` ${pxArrow} ${Math.abs(s.priceDelta).toFixed(2)}%` : ''}`;
    const symCell = el('div', {});
    symCell.append(el('span', { class: 'v-symbol' }, sym));
    symCell.append(el('span', { class: `v-symbol-px ${pxClass}` }, pxText));
    row.append(symCell);

    // Pipeline
    row.append(buildPipe(s.agents, false));

    // Latest argument
    const lastReal = [...s.messages].reverse().find(m => m.kind === 'message');
    const last = lastReal || s.messages[s.messages.length - 1];
    const msgCell = el('div', { class: last && last.kind === 'tool' ? 'v-msg tool' : 'v-msg' });
    if (last) {
      msgCell.append(el('span', { class: 'who' }, bylineLabelFor(last.agent, last)));
      msgCell.append(el('span', { html: highlightNumbers(last.text || '') }));
    } else {
      msgCell.append(el('span', { style: 'color: var(--v-text-4);' }, 'Convening…'));
    }
    if (s.flipped) {
      msgCell.append(el('span', { class: 'v-flip-tag' }, '↻ Flipped'));
    }
    row.append(msgCell);

    // Conviction
    const convCell = el('div', { class: 'v-conv' });
    const fillCls = s.conviction >= 7 ? 'bull' : s.conviction <= 3 ? 'bear' : '';
    const convPct = Math.max(0, Math.min(100, s.conviction * 10));
    const bar = el('div', { class: 'v-conv-bar' });
    bar.append(el('div', { class: `v-conv-fill ${fillCls}`, style: `width: ${convPct}%;` }));
    convCell.append(bar);
    convCell.append(el('span', { class: 'v-conv-label' },
      s.conviction === 0 ? '— starting' : `${s.conviction.toFixed(1)} / 10`));
    row.append(convCell);

    // Verdict
    let verdictCell;
    if (s.verdict) {
      verdictCell = el('div', { class: `v-verdict ${VERDICT_CLASS[s.verdict] || 'hold'}` }, s.verdict.toUpperCase());
    } else if (s.debateRound > 0) {
      verdictCell = el('div', { class: 'v-verdict live' }, `R${s.debateRound} / ${s.totalRounds}`);
    } else {
      verdictCell = el('div', { class: 'v-verdict pending' }, 'analysts');
    }
    row.append(verdictCell);

    return row;
  }

  function buildPipe(agents, compact = false) {
    const wrap = el('div', { class: compact ? 'v-pill-pipe' : 'v-pipe' });
    [AGENTS.analysts, AGENTS.research, AGENTS.risk].forEach(group => {
      const g = el('div', { class: compact ? '' : 'v-pipe-grp' });
      group.forEach(a => {
        const status = agents[a.key] || 'wait';
        const cls = status === 'done' ? 'done' : status === 'live' ? 'live' : '';
        g.append(el('span', { class: `v-pip ${cls}` }));
      });
      wrap.append(g);
    });
    return wrap;
  }

  function renderDeskSummary() {
    const wrap = $('#v-desk-summary');
    if (!wrap) return;
    wrap.innerHTML = '';
    const n = state.symbols.length;
    const done = state.symbols.filter(s => state.bySymbol[s].done).length;
    const live = n - done;
    const convs = state.symbols.map(s => state.bySymbol[s].conviction).filter(c => c > 0);
    const avg = convs.length ? convs.reduce((a, b) => a + b, 0) / convs.length : 0;
    const tilt = avg >= 6 ? 'Bullish' : avg <= 4 ? 'Bearish' : 'Neutral';
    const tiltClass = avg >= 6 ? 'bull' : avg <= 4 ? 'bear' : 'amber';

    const cell = (label, value, cls = '', right = false) => {
      const c = el('div', { class: 'v-summary-cell' + (right ? ' right' : '') });
      c.append(el('span', { class: 'v-summary-label' }, label));
      c.append(el('span', { class: `v-summary-val ${cls}` }, value));
      return c;
    };
    wrap.append(cell('Done', String(done), 'bull'));
    wrap.append(cell('In progress', String(live), 'amber'));
    wrap.append(cell('Avg conviction', avg ? avg.toFixed(1) : '—'));
    wrap.append(cell('Batch tilt', avg ? tilt : '—', tiltClass));
  }

  /* ============================================================
     STAGE (single-symbol detail)
     ============================================================ */
  function selectSymbol(sym) {
    state.activeSymbol = sym;
    showView('stage');
  }

  function renderStage() {
    const root = $('#v-stage');
    if (!root) return;
    const sym = state.activeSymbol;
    if (!sym || !state.bySymbol[sym]) {
      root.innerHTML = '';
      root.append(el('div', { class: 'v-empty' },
        el('div', { class: 'v-empty-title' }, 'No symbol selected'),
        el('div', { class: 'v-empty-body' }, 'Pick a row from the floor to bring it to the stage.'),
      ));
      return;
    }
    const s = state.bySymbol[sym];

    root.innerHTML = '';

    // Strip with all symbols
    const strip = el('div', { class: 'v-strip' });
    const back = el('button', { class: 'v-back' }, '← Back to floor');
    back.addEventListener('click', () => showView('desk'));
    strip.append(back);
    strip.append(el('span', { class: 'v-strip-label' }, 'Batch'));
    state.symbols.forEach(other => {
      const otherS = state.bySymbol[other];
      const pill = el('div', { class: 'v-pill' + (other === sym ? ' active' : '') });
      pill.addEventListener('click', () => selectSymbol(other));
      pill.append(el('span', { class: 'v-pill-sym' }, other));
      pill.append(buildPipe(otherS.agents, true));
      let vText = '…';
      let vCls = 'pending';
      if (otherS.verdict) {
        vText = otherS.verdict.toUpperCase().slice(0, 4);
        vCls = VERDICT_CLASS[otherS.verdict] || 'hold';
      } else if (otherS.debateRound > 0) {
        vText = `R${otherS.debateRound}/${otherS.totalRounds}`;
        vCls = 'live';
      }
      pill.append(el('span', { class: `v-pill-verdict ${vCls}` }, vText));
      strip.append(pill);
    });
    root.append(strip);

    // Stage header
    const head = el('div', { class: 'v-stage-head' });
    const headLeft = el('div');
    const tickEl = el('div', { class: 'v-stage-tick' }, sym);
    if (s.price != null) {
      const pxArrow = s.priceDelta >= 0 ? '▲' : '▼';
      tickEl.append(el('span', { class: 'v-stage-px' },
        `$${fmtPx(s.price)} ${pxArrow} ${Math.abs(s.priceDelta || 0).toFixed(2)}%`));
    }
    headLeft.append(tickEl);
    headLeft.append(el('div', { class: 'v-stage-meta' },
      `${state.runId ? `Run ${state.runId}` : ''} · Date ${new Date().toISOString().slice(0,10)}`));
    head.append(headLeft);

    const status = el('div', { class: 'v-stage-status' });
    if (s.done && s.verdict) {
      status.append(document.createTextNode(`Verdict · ${s.verdict}`));
    } else if (s.debateRound > 0) {
      status.append(el('span', { class: 'v-pulse-dot' }));
      status.append(document.createTextNode(`Round ${s.debateRound} of ${s.totalRounds}`));
    } else {
      status.append(el('span', { class: 'v-pulse-dot' }));
      status.append(document.createTextNode('In session'));
    }
    head.append(status);
    root.append(head);

    // Report tabs
    const tabs = el('div', { class: 'v-tabs' });
    REPORT_TABS.forEach(t => {
      const has = !!s.reports[t.key];
      const isActive = state.activeReportTab === t.key;
      const isLive = !has && nextLiveReportSection(s) === t.key;
      const btn = el('button', { class: `v-tab ${isActive ? 'active' : ''}` });
      if (has) btn.append(el('span', { class: 'v-check' }, '✓'));
      if (isLive) btn.append(el('span', { class: 'v-live-dot' }));
      btn.append(document.createTextNode(t.label));
      btn.addEventListener('click', () => { state.activeReportTab = t.key; renderStage(); });
      tabs.append(btn);
    });
    root.append(tabs);

    // Three-col grid
    const grid = el('div', { class: 'v-stage-grid' });

    // Left: agent roster
    const roster = el('div', { class: 'v-roster' });
    [
      ['Analysts', AGENTS.analysts],
      ['Research', AGENTS.research],
      ['Risk & trade', AGENTS.risk],
    ].forEach(([label, group]) => {
      roster.append(el('div', { class: 'v-stage-label' }, label));
      group.forEach(a => {
        const status = s.agents[a.key];
        const cls = ['v-agent'];
        if (status === 'live') cls.push('live');
        if (status === 'wait') cls.push('wait');
        const wrap = el('div', { class: cls.join(' ') });
        wrap.append(el('span', { class: `v-agent-dot ${status}` }));
        wrap.append(el('span', { class: 'v-agent-name' }, a.name));
        roster.append(wrap);
      });
    });
    grid.append(roster);

    // Middle: stream OR rendered report
    const stream = el('div', { class: 'v-stream' });
    const tabKey = state.activeReportTab;
    const showReport = !!s.reports[tabKey] && tabKey !== nextLiveReportSection(s);
    if (showReport) {
      const body = el('div', { class: 'v-stream-body', html: simpleMarkdown(s.reports[tabKey]) });
      stream.append(body);
    } else {
      const msgs = s.messages.length ? s.messages : [{ kind: 'system', agent: 'system', text: 'Waiting for the first analyst to file.', time: new Date() }];
      msgs.forEach(m => {
        const wrap = el('div', { class: 'v-stream-msg' });
        const headRow = el('div', { class: 'v-stream-msg-head' });
        headRow.append(el('span', { class: `v-byline ${bylineClassFor(m.agent)}` },
          bylineLabelFor(m.agent, m)));
        headRow.append(el('span', { class: 'v-time' }, fmtTimeOfDay(m.time)));
        wrap.append(headRow);
        const isTool = m.kind === 'tool';
        wrap.append(el('div', { class: `v-stream-body ${isTool ? 'tool' : ''}`,
          html: isTool ? (m.text || '') : highlightNumbers(m.text || '') }));
        stream.append(wrap);
      });
      // Auto-scroll
      setTimeout(() => { stream.scrollTop = stream.scrollHeight; }, 0);
    }
    grid.append(stream);

    // Right: meta column
    const meta = el('div', { class: 'v-meta-col' });

    const convCard = el('div', { class: 'v-card' });
    convCard.append(el('div', { class: 'v-card-label' }, s.done ? 'Final conviction' : 'Conviction'));
    const convNum = el('div', { class: `v-card-val ${s.conviction >= 5 ? 'bull' : s.conviction > 0 && s.conviction < 5 ? 'bear' : ''}` },
      s.conviction ? s.conviction.toFixed(1) : '—');
    convNum.append(el('span', { class: 'v-card-val-sub' }, '/ 10'));
    convCard.append(convNum);
    meta.append(convCard);

    const debateCard = el('div', { class: 'v-card' });
    debateCard.append(el('div', { class: 'v-card-label' }, 'Debate rounds'));
    for (let r = 1; r <= (s.totalRounds || 1); r++) {
      const cls = r < s.debateRound ? 'done' : r === s.debateRound ? 'live' : 'wait';
      const text = r < s.debateRound ? 'complete' : r === s.debateRound ? 'live' : '—';
      const row = el('div', { class: 'v-debate-row' });
      row.append(el('span', {}, `Round ${r}`));
      row.append(el('span', { class: cls }, text));
      debateCard.append(row);
    }
    meta.append(debateCard);

    const actCard = el('div', { class: 'v-card v-actions' });
    const chartBtn = el('button', { class: 'v-btn' }, 'View 4-pane chart');
    chartBtn.addEventListener('click', () => window.open(`/api/chart/${sym}?period=6mo`, '_blank'));
    actCard.append(chartBtn);
    if (s.done) {
      const reportBtn = el('button', { class: 'v-btn primary' }, 'Open full report');
      reportBtn.addEventListener('click', () => window.open(`/api/runs/${state.runId}/report.md?symbol=${sym}`, '_blank'));
      actCard.append(reportBtn);
    }
    meta.append(actCard);

    grid.append(meta);
    root.append(grid);
  }

  function nextLiveReportSection(s) {
    // The live one is the agent currently 'live' mapped to a report section
    const liveAgent = ALL_AGENTS.find(a => s.agents[a.key] === 'live');
    if (!liveAgent) return null;
    const map = {
      market:       'market',
      social:       'sentiment',
      news:         'news',
      fundamentals: 'fundamentals',
      bull:         'investment_plan',
      bear:         'investment_plan',
      trader:       'trader_plan',
      risk:         'final_decision',
      portfolio:    'final_decision',
    };
    return map[liveAgent.key] || null;
  }

  function simpleMarkdown(md) {
    if (!md) return '';
    const safe = md.replace(/[<>&]/g, s => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[s]));
    return safe
      .replace(/^### (.*)$/gm, '<h3 style="font-size:14px;margin:18px 0 6px;color:var(--v-text);text-transform:uppercase;letter-spacing:0.14em;">$1</h3>')
      .replace(/^## (.*)$/gm,  '<h2 style="font-size:16px;margin:22px 0 8px;color:var(--v-text);">$1</h2>')
      .replace(/^# (.*)$/gm,   '<h1 style="font-size:18px;margin:24px 0 10px;color:var(--v-text);">$1</h1>')
      .replace(/\*\*(.+?)\*\*/g, '<strong style="color:var(--v-text);">$1</strong>')
      .replace(/\*(.+?)\*/g, '<em style="color:var(--v-amber);font-style:normal;font-weight:500;">$1</em>')
      .replace(/^- (.*)$/gm, '<div style="padding-left:14px;position:relative;margin:4px 0;">• $1</div>')
      .replace(/\n\n/g, '<br><br>');
  }

  /* ============================================================
     KEYBOARD
     ============================================================ */
  function attachKeys() {
    document.addEventListener('keydown', (e) => {
      if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA')) return;
      // Esc → back to desk from stage
      if (e.key === 'Escape' && state.view === 'stage') { showView('desk'); return; }
      // 1..9 → jump to symbol N (works on desk and stage)
      if (state.view === 'stage' && /^[1-9]$/.test(e.key)) {
        const i = parseInt(e.key, 10) - 1;
        if (state.symbols[i]) selectSymbol(state.symbols[i]);
        return;
      }
      // Arrow keys cycle report tabs while on stage
      if (state.view === 'stage' && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
        const idx = REPORT_TABS.findIndex(t => t.key === state.activeReportTab);
        const nxt = e.key === 'ArrowRight' ? Math.min(REPORT_TABS.length - 1, idx + 1) : Math.max(0, idx - 1);
        state.activeReportTab = REPORT_TABS[nxt].key;
        renderStage();
      }
    });
  }

  /* ============================================================
     BOOT
     ============================================================ */
  document.addEventListener('DOMContentLoaded', () => {
    initMarquee();
    $$('.v-nav-item').forEach(b => {
      b.addEventListener('click', () => showView(b.dataset.view));
    });
    attachKeys();
    showView('home');
  });

  // Expose for debugging / external triggers if needed
  window.Verve = { state, showView, selectSymbol, attachToRun };
})();
