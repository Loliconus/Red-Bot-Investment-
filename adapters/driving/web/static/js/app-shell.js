/* Global HTMX/Alpine shell. All requests stay on this origin; no localhost calls. */
(function () {
  'use strict';
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  window.redBotToast = function (message, level = 'info') {
    const host = document.getElementById('toast-stack');
    if (!host) return;
    const toast = document.createElement('div');
    toast.className = `toast ${level}`;
    toast.textContent = message;
    host.prepend(toast);
    setTimeout(() => toast.remove(), 6000);
  };
  window.redBotShell = function () {
    return {
      sidebarCollapsed: false, mobileOpen: false, paletteOpen: false, notificationsOpen: false,
      clockOffset: 0,
      init() {
        try {
          this.sidebarCollapsed = localStorage.getItem('redbot-sidebar') === 'collapsed';
          document.documentElement.dataset.theme = localStorage.getItem('redbot-theme') || 'dark';
        } catch (_) { /* private browsing still works */ }
        this.updateClock();
        setInterval(() => this.updateClock(), 1000);
        let pendingG = false;
        document.addEventListener('keydown', (event) => {
          const editing = event.target.closest?.('input, textarea, select, [contenteditable="true"], .cm-editor');
          if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
            event.preventDefault(); this.openPalette(); return;
          }
          if (event.key === 'Escape') {
            this.paletteOpen = false; this.notificationsOpen = false;
            const modal = document.getElementById('modal-root'); if (modal) modal.innerHTML = '';
            return;
          }
          if (editing || event.altKey || event.ctrlKey || event.metaKey) return;
          if (event.key === '?') { event.preventDefault(); this.openPalette('?'); return; }
          if (pendingG) {
            pendingG = false;
            const paths = {d:'/',m:'/reasoning',i:'/instruments',s:'/strategy',r:'/risk',
                           e:'/settings',j:'/journal',c:'/control',a:'/admin/storage',
                           b:'/backtest',
                           h:document.querySelector('.sidebar-nav a[title^="Рынок"]')?.getAttribute('href')};
            if (paths[event.key]) { event.preventDefault(); window.location.href = paths[event.key]; }
          } else if (event.key === 'g') {
            pendingG = true; setTimeout(() => { pendingG = false; }, 1300);
          }
        });
        window.redbotSetServerTime = (dateString) => {
          const parsed = Date.parse(dateString);
          if (Number.isFinite(parsed)) this.clockOffset = parsed - Date.now();
          this.updateClock();
        };
      },
      toggleSidebar() {
        this.sidebarCollapsed = !this.sidebarCollapsed;
        try { localStorage.setItem('redbot-sidebar', this.sidebarCollapsed ? 'collapsed' : 'expanded'); } catch (_) {}
      },
      toggleTheme() {
        const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        document.documentElement.dataset.theme = theme;
        try { localStorage.setItem('redbot-theme', theme); } catch (_) {}
      },
      openPalette(query = '') {
        this.paletteOpen = true;
        this.$nextTick(() => {
          const input = document.getElementById('command-input');
          if (input) { input.value = query; input.dispatchEvent(new Event('input')); input.focus(); }
        });
      },
      updateClock() {
        const now = new Date(Date.now() + this.clockOffset);
        const parts = new Intl.DateTimeFormat('en-GB', {timeZone:'Europe/Moscow',
          weekday:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(now);
        const field = name => parts.find(part => part.type === name)?.value || '00';
        const clock = document.getElementById('moex-clock');
        if (clock) clock.textContent = `${field('hour')}:${field('minute')}:${field('second')}`;
        const weekday = field('weekday'); const minute = Number(field('hour')) * 60 + Number(field('minute'));
        const workday = weekday !== 'Sat' && weekday !== 'Sun';
        let phase = 'Закрыта';
        if (workday && minute >= 410 && minute < 600) phase = 'Предторги';
        else if (workday && minute >= 600 && minute < 1130) phase = 'Открыта';
        else if (workday && minute >= 1130 && minute < 1430) phase = 'Post-market';
        const el = document.getElementById('moex-phase');
        if (el) { el.textContent = phase; el.title = 'Ориентировочная фаза MOEX, не официальный торговый календарь'; }
      },
      async logout() {
        try { await fetch('/logout', {method:'POST', credentials:'same-origin', headers:{'X-Red-Bot-CSRF':csrf()}}); }
        finally { window.location.href = '/login'; }
      },
    };
  };
  window.killConfirm = () => ({
    seconds: 3,
    init() {
      const timer = setInterval(() => {
        if (this.seconds > 0) this.seconds -= 1;
        if (this.seconds <= 0) clearInterval(timer);
      }, 1000);
    },
  });
  document.body?.addEventListener('token-saved', () => {
    const field = document.querySelector('.token-form input[name="api_token"]');
    if (field) field.value = '';
  });
  document.addEventListener('htmx:configRequest', (event) => {
    event.detail.headers['X-Red-Bot-CSRF'] = csrf();
  });
  document.addEventListener('htmx:beforeSwap', (event) => {
    const status = event.detail.xhr?.status || 0;
    if ([400, 409, 422].includes(status) && event.detail.xhr?.getResponseHeader('content-type')?.includes('text/html')) {
      event.detail.shouldSwap = true; event.detail.isError = false;
    } else if (status === 401 || status === 403) {
      window.redBotToast('Сессия истекла или запрос отклонён', 'error');
      if (status === 401) window.location.href = '/login';
    }
  });
  document.addEventListener('input', (event) => {
    if (event.target.id === 'decision-search') {
      const query = event.target.value.toLowerCase();
      document.querySelectorAll('.decision-row').forEach(row => {
        row.hidden = !row.dataset.decision.toLowerCase().includes(query);
      });
    }
  });
  document.addEventListener('click', (event) => {
    const th = event.target.closest?.('.sortable-table th[data-sort]');
    if (th) {
      const table = th.closest('table'), tbody = table.querySelector('tbody');
      const index = [...th.parentElement.children].indexOf(th);
      const descending = th.dataset.descending !== 'true';
      th.dataset.descending = String(descending);
      const values = [...tbody.querySelectorAll('tr')];
      values.sort((a, b) => {
        const left = a.children[index]?.textContent?.trim() || '';
        const right = b.children[index]?.textContent?.trim() || '';
        const cmp = th.dataset.sort === 'number'
          ? (Number(left.replace(/\s/g, '')) - Number(right.replace(/\s/g, '')))
          : left.localeCompare(right, 'ru');
        return descending ? -cmp : cmp;
      });
      values.forEach(row => tbody.appendChild(row));
    }
    const history = event.target.closest?.('[data-sql-history]');
    if (history) {
      const query = history.dataset.sqlHistory;
      const textarea = document.getElementById('sql-input'); if (textarea) textarea.value = query;
      if (window.redbotSqlEditor) window.redbotSqlEditor.dispatch({changes:{from:0,to:window.redbotSqlEditor.state.doc.length,insert:query}});
    }
  });
})();
