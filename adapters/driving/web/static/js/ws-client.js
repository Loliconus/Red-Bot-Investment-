/* Snapshot + delta, HTTP replay BEFORE live dispatch, exponential reconnect. */
(function () {
  'use strict';
  if (document.body.dataset.authenticated !== 'true') return;
  const SECTION = document.body.dataset.section;
  const channels = ['system.mode','system.resources','system.tasks','system.notifications'];
  if (SECTION === 'control') channels.push('control.logs');
  if (SECTION === 'dashboard' || SECTION === 'reasoning') channels.push('dashboard.decisions');
  if (SECTION === 'reasoning') channels.push('reasoning.scan');
  if (SECTION === 'journal') channels.push('journal.hypotheses');
  if (SECTION === 'storage') channels.push('db_admin.jobs');
  if (SECTION === 'security') channels.push('security.audit');
  if (SECTION === 'chart') channels.push(`chart.${document.querySelector('[data-chart-uid]')?.dataset.chartUid}`);
  const key = 'redbot-ws-seq';
  let seq = {};
  try { seq = JSON.parse(sessionStorage.getItem(key) || '{}'); } catch (_) { seq = {}; }
  const handlers = new Map(); let socket, attempts = 0, connectedBefore = false, replaying = false;
  let buffer = [], reconnectTimer = null, lastUpdate = null;
  try { lastUpdate = sessionStorage.getItem('redbot-ws-last-update'); } catch (_) {}
  const disconnected = () => {
    const badge = document.getElementById('ws-indicator');
    if (badge) { badge.classList.remove('online'); badge.classList.add('offline'); badge.querySelector('span').textContent = 'WS OFFLINE'; }
    const banner = document.getElementById('offline-banner');
    if (banner) banner.hidden = false;
    const text = document.getElementById('last-update');
    if (text) text.textContent = lastUpdate ? new Date(lastUpdate).toLocaleString('ru-RU') : 'нет данных';
  };
  const setConnected = () => {
    const badge = document.getElementById('ws-indicator');
    if (badge) { badge.classList.remove('offline'); badge.classList.add('online'); badge.querySelector('span').textContent = 'WS ONLINE'; }
    const banner = document.getElementById('offline-banner'); if (banner) banner.hidden = true;
  };
  function register(channel, handler) {
    if (!handlers.has(channel)) handlers.set(channel, new Set());
    handlers.get(channel).add(handler);
    return () => handlers.get(channel)?.delete(handler);
  }
  window.redbotWS = {on:register, subscribe(channel) {
    if (!channels.includes(channel)) channels.push(channel);
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({action:'subscribe', channels:[channel]}));
  }};
  function persistSeq() { try { sessionStorage.setItem(key, JSON.stringify(seq)); } catch (_) {} }
  function touch(ts) {
    lastUpdate = ts || new Date().toISOString();
    try { sessionStorage.setItem('redbot-ws-last-update', lastUpdate); } catch (_) {}
    window.redbotSetServerTime?.(lastUpdate);
  }
  function deliver(msg) {
    if (!msg || !channels.includes(msg.channel) || !Number.isInteger(msg.seq) || !msg.ts || !msg.type) return;
    const previous = Number(seq[msg.channel]) || 0;
    if (msg.type !== 'snapshot' && msg.seq <= previous) return;
    if (msg.type === 'snapshot' && msg.seq < previous) return;
    seq[msg.channel] = msg.seq; persistSeq(); touch(msg.ts);
    for (const handler of handlers.get(msg.channel) || []) handler(msg);
    document.dispatchEvent(new CustomEvent('redbot:channel', {detail:msg}));
  }
  async function catchUp(channel, since) {
    let current = since, latest = since;
    for (let page = 0; page < 100; page++) {
      const url = `/api/ws/replay?channel=${encodeURIComponent(channel)}&since_seq=${current}`;
      const response = await fetch(url, {credentials:'same-origin', cache:'no-store'});
      if (response.status === 409) return false;
      if (!response.ok) throw new Error(`Replay: ${response.status}`);
      const data = await response.json(); latest = data.latest_seq;
      for (const msg of data.events) { deliver(msg); current = Math.max(current, msg.seq); }
      if (current >= latest || !data.events.length) return true;
    }
    return false;
  }
  function process(msg) {
    const previous = Number(seq[msg.channel]) || 0;
    if (msg.type !== 'snapshot' && msg.seq > previous + 1 && previous > 0) {
      buffer.push(msg);
      if (!replaying) {
        replaying = true;
        catchUp(msg.channel, previous).then(ok => {
          if (!ok) { delete seq[msg.channel]; persistSeq(); socket.send(JSON.stringify({action:'unsubscribe',channels:[msg.channel]})); socket.send(JSON.stringify({action:'subscribe',channels:[msg.channel]})); }
          replaying = false; const held = buffer.splice(0); held.forEach(process);
        }).catch(() => { replaying = false; disconnected(); });
      }
      return;
    }
    deliver(msg);
  }
  async function onOpen() {
    // sessionStorage переживает навигацию; первый WS на новой странице тоже
    // должен сверить seq с сервером (он мог полностью перезапуститься).
    const reconnect = connectedBefore || channels.some(channel => (Number(seq[channel]) || 0) > 0);
    connectedBefore = true; attempts = 0;
    replaying = reconnect; buffer = [];
    socket.send(JSON.stringify({action:'subscribe', channels}));
    if (reconnect) {
      const gaps = new Set();
      try {
        // Буферизуем live-сообщения (включая снимки) до завершения replay.
        for (const channel of channels) {
          const last = Number(seq[channel]) || 0;
          if (last && !(await catchUp(channel, last))) gaps.add(channel);
        }
      } catch (_) { disconnected(); }
      replaying = false;
      const queued = buffer.splice(0);
      for (const msg of queued) {
        if (gaps.has(msg.channel)) {
          delete seq[msg.channel];
          if (msg.type !== 'snapshot') continue;
        }
        // История решений доставлена полностью; snapshot последних 50 не
        // должен стирать более старые решения, полученные через replay.
        if (msg.type === 'snapshot' && msg.channel === 'dashboard.decisions' &&
            !gaps.has(msg.channel) && seq[msg.channel]) continue;
        process(msg);
      }
    }
    setConnected();
  }
  function connect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
    socket.onopen = () => { onOpen().catch(() => disconnected()); };
    socket.onmessage = event => {
      let msg; try { msg = JSON.parse(event.data); } catch (_) { return; }
      if (replaying) buffer.push(msg); else process(msg);
    };
    socket.onclose = event => {
      disconnected();
      if (event.code === 4401) return;
      const delay = Math.min(30000, 500 * (2 ** Math.min(attempts++, 6)));
      reconnectTimer = setTimeout(connect, delay);
    };
    socket.onerror = () => disconnected();
  }
  // Никакая страница не должна выдавать устаревшее за актуальное при обрыве.
  disconnected(); connect();
  const metrics = {
    cpu:['metric-cpu', x => x == null ? '—' : `${x.toFixed(1)}%`],
    ram_gui:['metric-ram', x => x == null ? '—' : `${(x/1048576).toFixed(0)} MB`],
    ram_duckdb:['metric-db', (x,p) => x == null ? 'н/д' : `${(x/1048576).toFixed(0)} / ${(p.ram_duckdb_limit/1048576).toFixed(0)} MB`],
    disk_free:['metric-disk', x => x == null ? 'н/д' : `${(x/(1024**3)).toFixed(1)} GB`],
    latency:['metric-latency', x => x == null ? 'н/д' : `${x} мс`],
  };
  const priorLevels = {};
  register('system.resources', ({payload:p}) => {
    if (!p || Array.isArray(p)) return;
    for (const [name, [id, format]] of Object.entries(metrics)) {
      const el = document.getElementById(id); if (!el) continue;
      el.querySelector('strong').textContent = format(p[name], p);
      const level = p.levels?.[name] || 'unknown';
      el.classList.remove('critical','warning'); if (['critical','warning'].includes(level)) el.classList.add(level);
      if (level === 'critical' && priorLevels[name] !== 'critical') window.redBotToast?.(`Критический порог: ${el.querySelector('small').textContent}`, 'critical');
      priorLevels[name] = level;
    }
  });
  register('system.mode', ({payload:p}) => {
    if (!p?.mode) return;
    const mode = p.mode.toUpperCase(); document.body.dataset.mode = mode;
    const stripe = document.getElementById('mode-stripe');
    if (stripe) { stripe.className = `mode-stripe mode-${p.mode}`; stripe.setAttribute('aria-label', `Контур исполнения: ${mode}`); }
    const badge = document.querySelector('.topbar .mode-badge');
    if (badge) { badge.className = `mode-badge ${p.mode}`; badge.textContent = `● ${mode}`; }
  });
  register('system.tasks', ({type,payload:p}) => {
    const indicator = document.getElementById('scheduler-indicator'); if (!indicator) return;
    if (type === 'system.paused') {
      indicator.classList.remove('online'); indicator.classList.add('offline');
      indicator.querySelector('span').textContent = 'Scheduler PAUSED';
      return;
    }
    if (!Array.isArray(p)) return;
    const active = p.some(task => ['RUNNING','HEALTHY','DEGRADED','WAITING'].includes(task.status));
    const degraded = p.some(task => task.status === 'DEGRADED');
    indicator.classList.toggle('online', active && !degraded);
    indicator.classList.toggle('offline', !active || degraded);
    indicator.querySelector('span').textContent = degraded ? 'Scheduler DEGRADED' : active ? 'Scheduler ON' : 'Scheduler STOP';
  });
  register('dashboard.decisions', ({type,payload:p}) => {
    const feed = document.getElementById('decision-feed'); if (!feed) return;
    if (type === 'snapshot') { for (const entry of [...(Array.isArray(p) ? p : [])].reverse()) addDecision(entry); return; }
    addDecision(p);
  });
  function addDecision(p) {
    const feed = document.getElementById('decision-feed'); if (!feed || !p?.id || feed.querySelector(`[data-decision-id="${p.id}"]`)) return;
    const row = document.createElement('button');
    row.type = 'button'; row.className = 'decision-row'; row.dataset.decisionId = p.id;
    row.dataset.decision = `${p.ticker} ${p.decision} ${p.thought_text}`;
    const mark = document.createElement('span'); mark.className = `decision-type decision-${p.decision}`;
    mark.textContent = p.decision === 'enter' ? '↗' : p.decision === 'reject' ? '×' : '━';
    const content = document.createElement('span'); content.className = 'decision-body';
    const title = document.createElement('strong'); title.textContent = `${p.ticker} · ${p.decision.toUpperCase()}`;
    const thought = document.createElement('span'); thought.textContent = p.thought_text;
    content.append(title, thought);
    const time = document.createElement('time'); time.className = 'mono muted'; time.textContent = p.created_at?.slice(11,16) || '';
    row.append(mark, content, time);
    row.addEventListener('click', async () => {
      const html = await fetch(`/dashboard/decision/${encodeURIComponent(p.id)}`, {credentials:'same-origin'}).then(r => r.text());
      document.getElementById('modal-root').innerHTML = html;
    });
    feed.prepend(row); while (feed.children.length > 100) feed.lastElementChild.remove();
  }
  register('system.notifications', ({type,payload:p}) => {
    if (type === 'snapshot') return;
    const level = p?.level?.toLowerCase() || 'info';
    if (['warning','error','critical'].includes(level)) window.redBotToast?.(p.text, level);
    const list = document.getElementById('notification-list'); if (!list) return;
    const row = document.createElement('div'); row.className = 'notification-item';
    row.textContent = `${p.level || 'INFO'} · ${p.text || 'Событие'}`; list.prepend(row);
    const count = document.getElementById('notification-count'); if (count) {
      count.hidden = false; count.textContent = Math.min(Number(count.textContent || 0) + 1, 99);
    }
  });
  register('control.logs', ({type,payload:p}) => {
    if (type === 'snapshot') return;
    const box = document.getElementById('live-log'); if (!box) return;
    document.getElementById('log-empty')?.remove();
    const select = document.querySelector('.log-tools select'), search = document.querySelector('.log-tools input');
    const level = select?.value || '', query = (search?.value || '').toLowerCase();
    const content = `${p.module} ${p.message}`.toLowerCase();
    if ((level && p.level !== level) || (query && !content.includes(query))) return;
    const row = document.createElement('div'); row.className = `log-line ${p.level?.toLowerCase()}`;
    for (const [cls, text] of [['log-ts',p.ts?.slice(11,19)],['log-level',p.level],['log-module',p.module],['log-msg',p.message]]) {
      const span = document.createElement('span'); span.className = cls; span.textContent = text || ''; row.append(span);
    }
    box.append(row); if (box.dataset.follow === 'true') box.scrollTop = box.scrollHeight;
    while (box.children.length > 250) box.firstElementChild.remove();
  });
  register('db_admin.jobs', ({type}) => {
    if (type === 'snapshot') return;
    document.body.dispatchEvent(new Event('db-jobs-updated'));
  });
  register('reasoning.scan', ({type}) => {
    if (type === 'snapshot') return;
    document.body.dispatchEvent(new Event('reasoning-scan-updated'));
  });
})();
