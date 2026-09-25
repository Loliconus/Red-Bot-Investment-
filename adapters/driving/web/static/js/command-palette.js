/* Incremental command palette: navigation, prefix search, last 10 actions. */
(function () {
  'use strict';
  const input = document.getElementById('command-input');
  const results = document.getElementById('command-results');
  if (!input || !results) return;
  const links = [...document.querySelectorAll('.sidebar-nav a')].map(node => ({
    label:node.querySelector('.nav-label')?.textContent || node.title,
    url:node.getAttribute('href'), category:'РАЗДЕЛ', hint:'Переход', icon:node.querySelector('.nav-icon')?.textContent || '↗'
  }));
  links.push({label:'Kill-switch · остановить бот',url:'/security',category:'КРИТИЧЕСКОЕ',hint:'Подтверждение на странице',icon:'■'});
  links.push({label:'Перезапуск scheduler',url:'/control',category:'УПРАВЛЕНИЕ',hint:'Пульт управления',icon:'↻'});
  let rows = [], active = 0, pending;
  const shortcuts = ['Ctrl/Cmd + K · открыть палитру','g, затем c/d/r/i/j/b/a/s · перейти к разделу',
                     'g, затем h · график','Ctrl + Enter · SQL-консоль','Esc · закрыть модал','? · показать клавиши'];
  function render(list) {
    rows = list.slice(0, 25); active = 0; results.replaceChildren();
    if (!rows.length) { const empty = document.createElement('div'); empty.className='empty-subtle'; empty.textContent='Нет совпадений'; results.append(empty); return; }
    rows.forEach((item, index) => {
      const button = document.createElement('button'); button.type='button';
      button.className='command-result' + (index === active ? ' active' : '');
      const symbol = document.createElement('span'); symbol.className='result-icon'; symbol.textContent=item.icon || '↗';
      const body = document.createElement('span');
      const label = document.createElement('strong'); label.textContent=item.label;
      const sub = document.createElement('small'); sub.textContent=item.category;
      body.append(label, sub);
      const hint = document.createElement('span'); hint.textContent=item.hint || '↗';
      button.append(symbol, body, hint);
      button.addEventListener('click', () => { if (item.url) window.location.href = item.url; });
      results.append(button);
    });
  }
  async function update() {
    const q = input.value.trim().toLowerCase();
    if (q === '?') { render(shortcuts.map(label => ({label,category:'ГОРЯЧАЯ КЛАВИША',icon:'⌘'}))); return; }
    const staticMatches = links.filter(item => (item.label + item.category).toLowerCase().includes(q));
    try {
      const response = await fetch('/api/search?q=' + encodeURIComponent(q), {credentials:'same-origin'});
      if (!response.ok) throw new Error('Search unavailable');
      const data = await response.json();
      const entities = (data.entities || []).map(item => ({...item,icon:'◈'}));
      const actions = !q ? (data.actions || []).map(item => ({
        label:item.label,url:item.url,category:'НЕДАВНЕЕ ДЕЙСТВИЕ',icon:'↻',hint:'Повторить переход'
      })) : [];
      if (input.value.trim().toLowerCase() === q) render([...staticMatches,...entities,...actions]);
    } catch (_) { render(staticMatches); }
  }
  input.addEventListener('input', () => { clearTimeout(pending); pending=setTimeout(update, 95); });
  input.addEventListener('focus', update);
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') { event.preventDefault(); active=Math.min(active+1,rows.length-1); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); active=Math.max(active-1,0); }
    else if (event.key === 'Enter') { event.preventDefault(); if (rows[active]?.url) window.location.href=rows[active].url; }
    else return;
    [...results.querySelectorAll('.command-result')].forEach((node,index)=>node.classList.toggle('active',index===active));
  });
})();
