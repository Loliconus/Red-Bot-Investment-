/* Интерактивная арена соревнования алгоритмов «Синтетический трейдер» (CSP-safe 'self') */
(function () {
  "use strict";

  function initBacktestArena() {
    var dataEl = document.getElementById("bt-arena-json");
    if (!dataEl) {
      return;
    }
    var payload = null;
    try {
      payload = JSON.parse(dataEl.textContent || "{}");
    } catch (err) {
      return;
    }
    if (!payload || !payload.timestamps || !payload.timestamps.length) {
      return;
    }

    var nBars = payload.timestamps.length;
    var padL = 58.0;
    var plotW = 1000.0 - 58.0 - 24.0;

    // 1. Переключатели видимости кривых алгоритмов (без inline onclick)
    var toggleButtons = document.querySelectorAll(".bt-series-toggle");
    toggleButtons.forEach(function (btn) {
      if (btn.dataset.bound === "1") {
        return;
      }
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        var targetId = btn.getAttribute("data-series-id");
        if (!targetId) {
          return;
        }
        var lineEl = document.getElementById("svg-line-" + targetId);
        var areaEl = document.getElementById("svg-area-" + targetId);
        var isHidden = btn.getAttribute("data-hidden") === "1";
        if (isHidden) {
          btn.setAttribute("data-hidden", "0");
          btn.style.opacity = "1";
          if (lineEl) lineEl.style.display = "";
          if (areaEl) areaEl.style.display = "";
        } else {
          btn.setAttribute("data-hidden", "1");
          btn.style.opacity = "0.38";
          if (lineEl) lineEl.style.display = "none";
          if (areaEl) areaEl.style.display = "none";
        }
      });
    });

    // 2. Кнопки быстрого действия (Синхронизация БД свечей и Переобучение весов)
    var syncBtn = document.getElementById("btn-sync-duckdb");
    var syncInput = document.getElementById("input-force-exchange-sync");
    var weightsSelect = document.getElementById("select-weights-mode");
    var retrainBtn = document.getElementById("btn-retrain-models");
    var formEl = document.getElementById("bt-control-form");

    if (syncBtn && formEl && syncBtn.dataset.bound !== "1") {
      syncBtn.dataset.bound = "1";
      syncBtn.addEventListener("click", function () {
        if (syncInput) syncInput.value = "true";
        if (typeof formEl.requestSubmit === "function") {
          formEl.requestSubmit();
        } else {
          formEl.submit();
        }
        if (syncInput) syncInput.value = "false";
      });
    }

    if (retrainBtn && formEl && retrainBtn.dataset.bound !== "1") {
      retrainBtn.dataset.bound = "1";
      retrainBtn.addEventListener("click", function () {
        if (weightsSelect) weightsSelect.value = "retrain";
        if (typeof formEl.requestSubmit === "function") {
          formEl.requestSubmit();
        } else {
          formEl.submit();
        }
      });
    }

    // 3. Синхронизированное перекрестие и живой инспектор бара по 3 SVG-графикам
    var svgIds = ["bt-svg-equity", "bt-svg-price", "bt-svg-prob"];
    var crossIds = ["bt-cross-eq", "bt-cross-pr", "bt-cross-pb"];
    var inspector = document.getElementById("bt-live-inspector");

    function updateCrosshair(barIdx) {
      var idx = Math.max(0, Math.min(nBars - 1, barIdx));
      var xCoord = padL + (idx / Math.max(nBars - 1, 1)) * plotW;

      crossIds.forEach(function (cid) {
        var line = document.getElementById(cid);
        if (line) {
          line.setAttribute("x1", xCoord.toFixed(1));
          line.setAttribute("x2", xCoord.toFixed(1));
          line.style.display = "";
        }
      });

      if (!inspector) {
        return;
      }

      var regMap = {
        trend: "ТРЕНД (импульс)",
        chop: "БОКОВИК («пила»)",
        panic: "ПАНИКА / ОБВАЛ"
      };
      var regCode = payload.regimes[idx] || "chop";
      var splitTag = idx < (payload.train_split_index || 0)
        ? "ОБУЧЕНИЕ (TRAIN)"
        : "ТЕСТ ВСЛЕПУЮ (OUT-OF-SAMPLE)";

      var algosHtml = "";
      (payload.series || []).forEach(function (s) {
        var v = (s.values && s.values[idx] !== undefined) ? s.values[idx] : 0;
        var sign = v >= 0 ? "+" : "";
        algosHtml +=
          '<span style="display:inline-flex;align-items:center;gap:5px;margin-right:12px;">' +
          '<i style="width:9px;height:9px;border-radius:50%;display:inline-block;background:' +
          s.color + ';"></i>' +
          '<span>' + s.name + ':</span> ' +
          '<strong class="mono">' + sign + v.toFixed(2) + '%</strong></span>';
      });

      var sig = payload.signals[idx];
      var sigBadge = "";
      if (sig === "BUY") {
        sigBadge = '<span class="pill positive">▲ ВХОД В LONG: ' +
          (payload.signal_labels[idx] || "") + '</span>';
      } else if (sig === "EXIT") {
        sigBadge = '<span class="pill warning">▼ ВЫХОД ИЗ СДЕЛКИ: ' +
          (payload.signal_labels[idx] || "") + '</span>';
      } else if ((payload.positions[idx] || 0) > 0) {
        sigBadge = '<span class="pill positive">УДЕРЖАНИЕ LONG (' +
          payload.positions[idx].toFixed(0) + '% капитала)</span>';
      } else {
        sigBadge = '<span class="pill neutral">В КЭШЕ (0%)</span>';
      }

      inspector.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:6px;">' +
        '<div><strong>Бар #' + (idx + 1) + ' из ' + nBars + ' · ' + payload.timestamps[idx] + ' UTC</strong> ' +
        '<span class="pill neutral" style="margin-left:6px;">' + splitTag + '</span> ' +
        '<span class="pill neutral" style="margin-left:4px;">Режим HMM: ' + (regMap[regCode] || regCode) + '</span></div>' +
        '<div>' + sigBadge + '</div></div>' +
        '<div style="margin-bottom:6px;font-size:0.84rem;">' + algosHtml + '</div>' +
        '<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;font-size:0.83rem;color:var(--muted);">' +
        '<span>Цена: <strong class="mono" style="color:var(--fg);">' + payload.prices[idx].toFixed(2) + ' ₽</strong> · ' +
        'Тренд ℓ₁: <strong class="mono" style="color:#38bdf8;">' + payload.l1_trend[idx].toFixed(2) + ' ₽</strong> · ' +
        'P(тренд)=<strong class="mono" style="color:#22c55e;">' + payload.p_trend[idx].toFixed(1) + '%</strong> · ' +
        'P(рост)=<strong class="mono" style="color:#38bdf8;">' + payload.p_up[idx].toFixed(1) + '%</strong> · ' +
        'P(слом)=<strong class="mono" style="color:#f97316;">' + payload.p_break[idx].toFixed(1) + '%</strong></span>' +
        '<span style="color:var(--fg);">👉 ' + (payload.explanations[idx] || "") + '</span></div>';
    }

    svgIds.forEach(function (sid) {
      var svgEl = document.getElementById(sid);
      if (!svgEl || svgEl.dataset.bound === "1") {
        return;
      }
      svgEl.dataset.bound = "1";
      svgEl.addEventListener("mousemove", function (ev) {
        var rect = svgEl.getBoundingClientRect();
        if (rect.width <= 0) {
          return;
        }
        var relX = ((ev.clientX - rect.left) / rect.width) * 1000.0;
        var ratio = (relX - padL) / plotW;
        var barIdx = Math.round(ratio * (nBars - 1));
        updateCrosshair(barIdx);
      });
    });

    // Показываем последний бар по умолчанию
    updateCrosshair(nBars - 1);
  }

  document.addEventListener("DOMContentLoaded", initBacktestArena);
  document.addEventListener("htmx:afterSwap", initBacktestArena);
  document.addEventListener("htmx:afterSettle", initBacktestArena);
})();
