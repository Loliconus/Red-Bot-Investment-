/* Chart-only overlays. Financial values are strings until converted for canvas rendering. */
(function () {
  'use strict';
  const root = document.querySelector('[data-chart-uid]'); if (!root) return;
  const uid = root.dataset.chartUid;
  const container = document.getElementById('price-chart');
  const oscillator = document.getElementById('oscillator-chart');
  let chart, candleSeries, oscChart, rsiSeries, macdSeries, imoexSeries;
  let fibLines = [], markerLines = [], frame = '1d', series = [], latestBook = null, pendingBar = null, lastTick = 0;
  const toBar = b => ({time:b.time,open:Number(b.open),high:Number(b.high),low:Number(b.low),close:Number(b.close)});
  const colors = () => document.documentElement.dataset.theme === 'light'
    ? {background:'#ffffff',text:'#607883',grid:'#e5edef',up:'#178764',down:'#be4653'}
    : {background:'#121e2b',text:'#8295a4',grid:'#233345',up:'#62dcb0',down:'#f1757c'};
  function initChart() {
    if (!window.LightweightCharts) {
      container.innerHTML = '<div class="chart-loading">Библиотека графика недоступна. Остальные экраны работают.</div>';
      return false;
    }
    const palette = colors();
    chart = LightweightCharts.createChart(container, {width:container.clientWidth,height:410,
      layout:{background:{type:'solid',color:palette.background},textColor:palette.text,fontFamily:'ui-monospace, monospace',fontSize:10},
      grid:{vertLines:{color:palette.grid},horzLines:{color:palette.grid}},
      timeScale:{timeVisible:true,secondsVisible:false,borderColor:palette.grid},
      rightPriceScale:{borderColor:palette.grid},
      crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    });
    candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor:palette.up,downColor:palette.down,borderUpColor:palette.up,
      borderDownColor:palette.down,wickUpColor:palette.up,wickDownColor:palette.down,
    });
    oscillator.textContent = '';
    oscChart = LightweightCharts.createChart(oscillator, {width:oscillator.clientWidth,height:120,
      layout:{background:{type:'solid',color:palette.background},textColor:palette.text,fontSize:9},
      grid:{vertLines:{visible:false},horzLines:{color:palette.grid}},
      timeScale:{timeVisible:true,visible:false},rightPriceScale:{borderVisible:false,scaleMargins:{top:.15,bottom:.15}},
    });
    rsiSeries = oscChart.addSeries(LightweightCharts.LineSeries,{color:'#f2bc63',lineWidth:2,priceLineVisible:false});
    macdSeries = oscChart.addSeries(LightweightCharts.LineSeries,{color:'#62dcb0',lineWidth:1,priceLineVisible:false});
    chart.timeScale().subscribeVisibleLogicalRangeChange(range => { if (range) oscChart.timeScale().setVisibleLogicalRange(range); });
    chart.subscribeCrosshairMove(param => {
      if (!param.time) return;
      const candle = param.seriesData.get(candleSeries);
      const ohlc = document.getElementById('chart-ohlc');
      if (ohlc && candle) ohlc.textContent = `O ${candle.open} · H ${candle.high} · L ${candle.low} · C ${candle.close}`;
      try { oscChart.setCrosshairPosition(50, param.time, rsiSeries); } catch (_) { /* library version */ }
    });
    new ResizeObserver(() => {
      chart?.applyOptions({width:container.clientWidth});
      oscChart?.applyOptions({width:oscillator.clientWidth});
    }).observe(container);
    return true;
  }
  function applyFib(fibonacci, reasoning) {
    fibLines.forEach(line => candleSeries.removePriceLine(line)); fibLines=[];
    if (!document.querySelector('[data-overlay="fib"]')?.checked) return;
    const weight = reasoning?.reasoning?.find(step => step.module === 'fibonacci')?.weight || '—';
    Object.entries(fibonacci || {}).filter(([name]) => name.startsWith('fib_')).forEach(([name,price]) => {
      fibLines.push(candleSeries.createPriceLine({price:Number(price), color:'#f2bc63',lineWidth:1,
        lineStyle:LightweightCharts.LineStyle.Dotted,axisLabelVisible:true,
        title:`${name.replace('fib_','Fibo ')} / вклад ${weight}`}));
    });
  }
  function applyMarkers(markers) {
    markerLines.forEach(line => candleSeries.removePriceLine(line)); markerLines=[];
    if (!document.querySelector('[data-overlay="markers"]')?.checked) return;
    (markers || []).forEach(marker => {
      markerLines.push(candleSeries.createPriceLine({price:Number(marker.price),color:'#62dcb0',
        lineStyle:LightweightCharts.LineStyle.Dashed,lineWidth:1,
        title:`Вход ${marker.status} · TradePlan ${marker.plan_id.slice(0,8)}`}));
    });
  }
  function applyBenchmark(benchmark) {
    if (imoexSeries) { chart.removeSeries(imoexSeries); imoexSeries=null; }
    if (!document.querySelector('[data-overlay="imoex"]')?.checked || !benchmark?.length) return;
    imoexSeries = chart.addSeries(LightweightCharts.LineSeries, {color:'#99a9b5',lineWidth:1,
      priceScaleId:'left',priceLineVisible:false,lastValueVisible:false});
    chart.priceScale('left').applyOptions({visible:true,scaleMargins:{top:.8,bottom:0}});
    imoexSeries.setData(benchmark.map(b => ({time:b.time,value:Number(b.value)})));
  }
  function ema(values, period) {
    const alpha = 2/(period+1); let prev=values[0];
    return values.map(value => {prev=value*alpha+prev*(1-alpha); return prev;});
  }
  function oscillators(bars) {
    if (!bars.length) {rsiSeries.setData([]);macdSeries.setData([]);return;}
    const closes=bars.map(c=>c.close), fast=ema(closes,12),slow=ema(closes,26);
    const macd=bars.map((b,i)=>({time:b.time,value:fast[i]-slow[i]}));
    let gain=0, loss=0;
    const rsi=bars.map((bar,index)=>{
      if (index) {const delta=closes[index]-closes[index-1];gain=(gain*13+Math.max(delta,0))/14;loss=(loss*13+Math.max(-delta,0))/14;}
      return {time:bar.time,value:index<14?50:loss===0?100:100-100/(1+gain/loss)};
    });
    rsiSeries.setData(rsi);
    // MACD is normalized around RSI 50 for display on shared oscillator scale.
    const magnitude=Math.max(...macd.map(m=>Math.abs(m.value)),1);
    macdSeries.setData(macd.map(m=>({time:m.time,value:50+m.value/magnitude*35})));
  }
  function renderBook(bids,asks) {
    const node=document.getElementById('orderbook'); if (!node) return;
    node.replaceChildren();
    if (!bids?.length && !asks?.length) {node.textContent='Стакан недоступен';return;}
    for (const [side,levels] of [['ask',asks],['bid',bids]]) {
      for (const level of levels||[]) {
        const row=document.createElement('div');row.className=`book-row ${side}`;
        const price=document.createElement('span');price.textContent=level.price;
        const amount=document.createElement('span');amount.textContent=level.quantity;
        row.append(price,amount);node.append(row);
      }
    }
  }
  function renderReasoning(item) {
    const box=document.getElementById('chart-reasoning'); if (!box) return;
    box.replaceChildren();
    if (!item) {box.textContent='Решений ещё нет';return;}
    const title=document.createElement('strong');title.textContent=`${item.decision.toUpperCase()} · ${item.confluence_score}`;box.append(title);
    const text=document.createElement('p');text.textContent=item.thought_text;box.append(text);
    for (const step of item.reasoning||[]) {
      const row=document.createElement('div');row.className='reason-step';
      [step.module,step.signal,step.weight].forEach(value=>{const span=document.createElement('span');span.textContent=value;row.append(span);});
      box.append(row);
    }
  }
  let currentData;
  async function load(timeframe) {
    frame=timeframe;
    document.querySelectorAll('[data-timeframe]').forEach(button=>button.classList.toggle('selected',button.dataset.timeframe===frame));
    try {
      const response=await fetch(`/api/chart/${encodeURIComponent(uid)}?timeframe=${encodeURIComponent(frame)}`);
      if (!response.ok) throw new Error(`Нет данных: HTTP ${response.status}`);
      currentData=await response.json();
      series=currentData.bars.map(toBar);
      candleSeries.setData(series);oscillators(series);
      chart.timeScale().fitContent();oscChart.timeScale().fitContent();
      applyFib(currentData.fibonacci,currentData.reasoning);
      applyMarkers(currentData.markers);applyBenchmark(currentData.benchmark);
      renderBook(currentData.bids,currentData.asks);renderReasoning(currentData.reasoning);
      if (!series.length) window.redBotToast?.('Нет свечей за выбранный период', 'warning');
    } catch (err) {container.querySelector('.chart-loading')?.remove();window.redBotToast?.(String(err.message), 'error');}
  }
  if (!initChart()) return;
  document.querySelectorAll('[data-timeframe]').forEach(button=>button.addEventListener('click',()=>load(button.dataset.timeframe)));
  document.querySelectorAll('[data-overlay]').forEach(box=>box.addEventListener('change',()=>{
    if (!currentData) return;
    if (box.dataset.overlay==='fib') applyFib(currentData.fibonacci,currentData.reasoning);
    if (box.dataset.overlay==='markers') applyMarkers(currentData.markers);
    if (box.dataset.overlay==='imoex') applyBenchmark(currentData.benchmark);
    if (box.dataset.overlay==='mfe' && box.checked) window.redBotToast?.('MFE/MAE: нет фактических наблюдений для оверлея','warning');
  }));
  const subscribe=()=>{
    if (!window.redbotWS) {setTimeout(subscribe,50);return;}
    window.redbotWS.on(`chart.${uid}`,({type,payload})=>{
      if (type==='book.update') {latestBook=payload; if (Date.now()-lastTick>=250) {renderBook(latestBook.bids,latestBook.asks);lastTick=Date.now();}return;}
      if (type!=='bar.update' || frame!=='1m') return;
      pendingBar=payload;
      if (Date.now()-lastTick<250) {setTimeout(flushBar,250);return;} flushBar();
    });
  };
  function flushBar() {
    if (!pendingBar || Date.now()-lastTick<250) return;
    const bar=toBar(pendingBar); pendingBar=null;
    try {candleSeries.update(bar);} catch (_) { /* initial data not yet loaded */ }
    lastTick=Date.now();
  }
  load(frame); subscribe();
})();
