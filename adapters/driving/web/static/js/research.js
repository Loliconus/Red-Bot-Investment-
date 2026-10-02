(() => {
  "use strict";
  const root = document.getElementById("research-app");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const state = {
    run: null,
    report: null,
    selected: null,
    asset: null,
    timer: null,
    busy: false,
    activeTab: "overview",
  };
  const nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
  const money = (v) => (v == null ? "—" : nf.format(Number(v)) + " ₽");
  const percent = (v, digits = 1) =>
    v == null || !Number.isFinite(Number(v))
      ? "—"
      : (Number(v) * 100).toFixed(digits) + "%";
  const number = (v, digits = 2) =>
    v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toFixed(digits);
  const date = (v) =>
    v
      ? new Date(v).toLocaleDateString("ru-RU", {
          day: "2-digit",
          month: "short",
          year: "numeric",
        })
      : "—";
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const text = (id, value) => {
    $(id).textContent = value == null ? "—" : String(value);
  };
  const clearError = () => {
    $("lab-error").hidden = true;
  };
  const error = (message) => {
    text("lab-error", message);
    $("lab-error").hidden = false;
  };

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") {
      headers.set("Content-Type", "application/json");
      headers.set(
        "X-Red-Bot-CSRF",
        document.querySelector('meta[name="csrf-token"]').content,
      );
    }
    const response = await fetch(path, {
      ...options,
      headers,
      credentials: "same-origin",
    });
    const payload = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(payload.detail)
        ? payload.detail.map((e) => `${e.loc.join(".")}: ${e.msg}`).join("; ")
        : payload.detail;
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return payload;
  }

  function tab(name) {
    state.activeTab = name;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll("[data-pane]").forEach((pane) => {
      pane.hidden = pane.dataset.pane !== name;
    });
    requestAnimationFrame(drawCharts);
  }
  document
    .querySelectorAll("[data-tab]")
    .forEach((button) =>
      button.addEventListener("click", () => tab(button.dataset.tab)),
    );
  $("new-experiment").addEventListener("click", () => {
    tab("experiments");
    $("experiment-form").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("open-validation").addEventListener("click", (event) => {
    event.preventDefault();
    tab("validation");
  });
  $("asset-select").addEventListener("change", (event) => {
    state.asset = event.target.value;
    renderAsset();
    drawCharts();
  });
  $("calibration-head").addEventListener("change", drawCharts);

  function probabilityValue(id, value) {
    $(id).replaceChildren();
    if (value == null) {
      text(id, "—");
      return;
    }
    $(id).append(
      document.createTextNode((Number(value) * 100).toFixed(1)),
      el("small", "%"),
    );
  }
  function renderAsset() {
    const report = state.report;
    const latest = report?.latest?.find((row) => row.symbol === state.asset);
    for (const [key, field] of [
      ["trend", "p_trend"],
      ["up", "p_up"],
      ["break", "p_break"],
    ]) {
      const value = latest?.[field];
      probabilityValue(`p-${key}`, value);
      $(`p-${key}-meter`).style.width = value == null ? "0%" : percent(value);
    }
    if (!latest) return;
    const cfg = report.config;
    text(
      "asof-note",
      `${latest.symbol} · последний закрытый OOS-бар ${new Date(latest.asof).toLocaleString("ru-RU", { timeZone: "Europe/Moscow" })} MSK · это историческая оценка, не LIVE`,
    );
    text("trend-threshold", "≥ " + cfg.risk.trend_threshold);
    text(
      "direction-text",
      latest.p_up >= Number(cfg.risk.direction_threshold)
        ? "LONG bias"
        : "Нет LONG edge",
    );
    text(
      "horizon-text",
      cfg.horizon + " баров · " + cfg.interval.toUpperCase(),
    );
    const names = {
      trend: "Направленный",
      range: "Боковик / шум",
      panic: "Паника",
    };
    text("regime-label", names[latest.regime] || latest.regime);
    const reasons = [];
    if (latest.regime === "panic" || latest.regime_panic >= 0.6)
      reasons.push("Паника: новые позиции запрещены");
    if (latest.p_trend <= Number(cfg.risk.trend_threshold))
      reasons.push("P(trend) не выше порога входа");
    if (latest.p_up < Number(cfg.risk.direction_threshold))
      reasons.push("Нет подтверждённого LONG-направления");
    if (latest.p_break >= Number(cfg.risk.max_break_probability))
      reasons.push("Риск слома выше лимита");
    if (!reasons.length)
      reasons.push(
        "Вероятностные pre-gates пройдены; размер, лотность и портфель проверяются отдельно",
      );
    if (report.metrics.kill_reason)
      reasons.push("Kill switch: " + report.metrics.kill_reason);
    reasons.push("Исследовательская модель не подключена к брокеру");
    $("risk-reasons").replaceChildren(
      ...reasons.map((reason) => el("li", reason)),
    );
    text("risk-state", reasons.length > 2 ? "NO TRADE" : "RESEARCH ONLY");
    document
      .querySelectorAll("#asset-table tr[data-symbol]")
      .forEach((row) =>
        row.classList.toggle("selected", row.dataset.symbol === state.asset),
      );
  }

  function clearReport(run) {
    state.run = run;
    state.report = null;
    state.asset = null;
    text("run-meta", `RUN ${run.id.slice(0, 8)} / ОТЧЁТ ЕЩЁ НЕ ГОТОВ`);
    text(
      "source-chip",
      run.config?.source === "demo"
        ? "DEMO · ИСКУССТВЕННЫЕ ДАННЫЕ"
        : "ISS MOEX · HISTORY",
    );
    $("source-chip").classList.toggle("demo", run.config?.source === "demo");
    text(
      "timeframe-chip",
      run.config
        ? `${run.config.interval.toUpperCase()} → ${run.config.horizon} баров`
        : "Закрытые бары",
    );
    $("dataset-hash").title = "";
    $("model-hash").title = "";
    [
      "metric-return",
      "metric-sharpe",
      "metric-dd",
      "metric-trades",
      "metric-fees",
      "equity-period",
      "dsr-value",
      "pbo-value",
      "spa-value",
      "rc-value",
      "feature-count",
      "gate-count",
      "brier-note",
      "dataset-hash",
      "model-hash",
      "feature-version",
      "psi-alert",
      "cutoff-value",
      "cpcv-count",
      "asof-note",
      "trend-threshold",
      "direction-text",
      "horizon-text",
      "regime-label",
      "risk-state",
      "trial-count",
      "pbo-combos",
      "bootstrap-note",
    ].forEach((id) => text(id, "—"));
    [
      "asset-select",
      "asset-table",
      "feature-bars",
      "checklist",
      "cpcv-paths",
      "fold-table",
      "method-warnings",
      "risk-reasons",
    ].forEach((id) => $(id).replaceChildren());
    text("frozen-status", "Не раскрыт");
    text("frozen-chip", "Final OOS закрыт");
    text(
      "evidence-note",
      "Выбранный прогон ещё не имеет завершённого отчёта. Метрики другого эксперимента здесь не показываются.",
    );
    $("equity-empty").hidden = false;
    $("final-open").disabled = true;
    $("final-result").hidden = true;
    for (const id of ["download-json", "download-md", "download-predictions"]) {
      $(id).removeAttribute("href");
      $(id).classList.add("disabled");
      $(id).setAttribute("aria-disabled", "true");
    }
    renderAsset();
    drawCharts();
  }

  function renderReport(run) {
    state.run = run;
    state.report = run.report || null;
    const report = state.report;
    if (!report) return;
    text(
      "run-meta",
      `RUN ${run.id.slice(0, 8)} / ${report.environment.python} / ${report.registry_stage.toUpperCase()}`,
    );
    text(
      "source-chip",
      report.source === "demo"
        ? "DEMO · ИСКУССТВЕННЫЕ ДАННЫЕ"
        : "ISS MOEX · HISTORY",
    );
    $("source-chip").classList.toggle("demo", report.source === "demo");
    text(
      "timeframe-chip",
      report.config.interval.toUpperCase() +
        " → " +
        report.config.horizon +
        " баров",
    );
    const consumed = report.final_oos?.consumed;
    text(
      "frozen-chip",
      consumed
        ? "Final OOS использован"
        : "Frozen " + report.config.freeze_months + " месяцев",
    );
    const metrics = report.metrics;
    text("metric-return", percent(metrics.net_return, 2));
    $("metric-return").className =
      metrics.net_return < 0 ? "lab-negative" : "lab-positive";
    text("metric-sharpe", number(metrics.sharpe));
    text("metric-dd", percent(metrics.max_drawdown, 2));
    text("metric-trades", metrics.trades);
    text("metric-fees", "Комиссии: " + money(metrics.fees));
    const curve = report.equity_curve;
    text(
      "equity-period",
      curve.length
        ? date(curve[0].date) + " — " + date(curve.at(-1).date)
        : "Walk-forward",
    );
    $("equity-empty").hidden = curve.length > 0;
    if (
      !state.asset ||
      !report.latest.some((row) => row.symbol === state.asset)
    )
      state.asset = report.latest[0]?.symbol;
    $("asset-select").replaceChildren(
      ...report.latest.map((row) => {
        const option = el("option", row.symbol);
        option.value = row.symbol;
        return option;
      }),
    );
    $("asset-select").value = state.asset;
    $("asset-table").replaceChildren(
      ...report.latest.map((row) => {
        const tr = el("tr");
        tr.dataset.symbol = row.symbol;
        [
          row.symbol,
          percent(row.p_trend),
          percent(row.p_up),
          percent(row.p_break),
          { trend: "ТРЕНД", range: "ШУМ", panic: "ПАНИКА" }[row.regime] ||
            row.regime,
        ].forEach((value) => tr.append(el("td", value)));
        tr.addEventListener("click", () => {
          state.asset = row.symbol;
          $("asset-select").value = row.symbol;
          renderAsset();
          drawCharts();
        });
        return tr;
      }),
    );
    const importance = Object.entries(report.feature_importance || {})
      .filter(([name]) => report.selected_features.includes(name))
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8);
    const largest = importance[0]?.[1] || 1;
    $("feature-bars").replaceChildren(
      ...importance.map(([name, value]) => {
        const row = el("div", null, "lab-feature-row");
        const label = el("span", name);
        label.title = name;
        const track = el("div", null, "lab-feature-track");
        const fill = el("i");
        fill.style.width = percent(value / largest);
        track.append(fill);
        row.append(label, track, el("span", number(value, 3)));
        return row;
      }),
    );
    text("feature-count", report.selected_features.length + " признаков");
    const stats = report.statistics;
    text("dsr-value", percent(stats.dsr?.dsr));
    text("pbo-value", percent(stats.pbo?.probability));
    text("spa-value", number(stats.spa?.p_value, 3));
    text("rc-value", number(stats.reality_check?.p_value, 3));
    text("trial-count", "Учтено trials: " + stats.trials);
    text(
      "pbo-combos",
      "CSCV комбинации: " + (stats.pbo?.combinations ?? "недостаточно данных"),
    );
    text(
      "bootstrap-note",
      stats.bootstrap
        ? `${stats.bootstrap.reps} reps · block ${stats.bootstrap.block_size} дней`
        : "Недостаточно вариации / дней",
    );
    $("evidence-note").replaceChildren(
      el(
        "strong",
        report.source === "demo"
          ? "DEMO не является свидетельством alpha. "
          : "Доходность ещё не доказана. ",
      ),
      document.createTextNode(
        consumed
          ? "Final OOS уже использован; повторное раскрытие и подбор на этом study запрещены."
          : "Эти метрики — development OOS. Последние 6–12 месяцев не читались.",
      ),
    );
    $("checklist").replaceChildren(
      ...report.checks.map((check) => {
        const li = el("li");
        li.append(
          el(
            "span",
            check.passed ? "✓" : "○",
            "lab-check-icon" + (check.passed ? " pass" : ""),
          ),
          el("span", check.label),
        );
        return li;
      }),
    );
    text(
      "gate-count",
      report.checks.filter((check) => check.passed).length +
        "/" +
        report.checks.length,
    );
    text(
      "frozen-status",
      consumed
        ? "Использован навсегда"
        : "Закрыт · " + report.config.freeze_months + " месяцев",
    );
    text("cutoff-value", date(report.dataset.cutoff));
    text("dataset-hash", report.dataset.dataset_id.slice(0, 16) + "…");
    $("dataset-hash").title = report.dataset.dataset_id;
    text("model-hash", report.candidate_model_hash.slice(0, 16) + "…");
    $("model-hash").title = report.candidate_model_hash;
    text("feature-version", report.feature_version);
    text(
      "psi-alert",
      report.monitoring.drift_alert ? "DRIFT > 0.25" : "Ниже порога",
    );
    $("final-open").disabled =
      consumed ||
      run.status !== "completed" ||
      run.final_status?.status === "running";
    const cpcv = report.cpcv;
    text(
      "cpcv-count",
      cpcv.computed
        ? `${cpcv.combinations} fits / ${cpcv.paths.length} paths`
        : "Отключён",
    );
    const maxPath = Math.max(
      0.01,
      ...(cpcv.paths || []).map((path) => Math.abs(path.net_return || 0)),
    );
    $("cpcv-paths").replaceChildren(
      ...(cpcv.paths || []).map((path) => {
        const row = el("div", null, "lab-path-row");
        const track = el("div", null, "lab-path-track");
        const fill = el("i", null, path.net_return < 0 ? "negative" : "");
        fill.style.width = percent(Math.abs(path.net_return) / maxPath);
        track.append(fill);
        row.append(
          el("span", "PATH 0" + path.path),
          track,
          el(
            "strong",
            percent(path.net_return, 2),
            path.net_return < 0 ? "lab-negative" : "lab-positive",
          ),
        );
        row.title = `Sharpe ${number(path.sharpe)} · DD ${percent(path.max_drawdown)} · trades ${path.trades}`;
        return row;
      }),
    );
    if (!cpcv.computed)
      $("cpcv-paths").append(
        el("p", cpcv.reason || "CPCV не рассчитан", "lab-empty"),
      );
    $("fold-table").replaceChildren(
      ...report.folds.map((fold) => {
        const tr = el("tr");
        const outer = fold.outer;
        const brier = ["trend", "up", "break"]
          .map((head) => number(fold.probabilities[head].brier, 3))
          .join(" / ");
        [
          String(fold.fold).padStart(2, "0"),
          date(fold.test_start) + " — " + date(fold.test_end),
          `${outer.final_train_size} / ${outer.test_size} t`,
          outer.rows_removed_by_purge,
          fold.model.selected_features,
          brier,
          outer.temporal_leakage_free ? "✓ PIT PASS" : "FAIL",
        ].forEach((value) => tr.append(el("td", value)));
        return tr;
      }),
    );
    $("method-warnings").replaceChildren(
      ...[
        ...report.warnings,
        "Foundation models / PatchTST / TFT / GAT / CNN не реализованы в этом baseline и не выдаются за проверенные улучшения.",
      ].map((value) => el("li", value)),
    );
    for (const [id, name] of [
      ["download-json", "report.json"],
      ["download-md", "report.md"],
      ["download-predictions", "predictions.parquet"],
    ]) {
      $(id).href = `/api/backtest/runs/${run.id}/artifacts/${name}`;
      $(id).classList.remove("disabled");
      $(id).setAttribute("aria-disabled", "false");
    }
    $("final-result").hidden = !run.final_report;
    if (run.final_report) {
      const f = run.final_report;
      text(
        "final-result",
        `FINAL OOS · ${f.source.toUpperCase()} · net ${percent(f.metrics.net_return, 2)} · Sharpe ${number(f.metrics.sharpe)} · DD ${percent(f.metrics.max_drawdown, 2)} · ${f.metrics.trades} сделок. Без fit / selection / recalibration. LIVE по-прежнему OFF.`,
      );
    }
    renderAsset();
    drawCharts();
  }

  function progress(run) {
    const job = run.final_status || run;
    const running = job.status === "running";
    state.busy = running;
    $("run-progress").hidden =
      !running && !["failed", "cancelled", "interrupted"].includes(job.status);
    $("progress-spinner").hidden = !running;
    text(
      "progress-stage",
      {
        queued: "Запуск worker",
        data: "История и snapshot",
        features: "Point-in-time признаки",
        walk_forward: "Purged walk-forward",
        backtest: "Риск и исполнение",
        cpcv: "Combinatorial purged CV",
        statistics: "Контроль переобучения",
        registry: "Фиксация модели",
        final_oos: "FINAL OOS · одноразовый тест",
        failed: "Прогон не завершён",
        cancelled: "Worker остановлен",
        interrupted: "Worker прерван",
      }[job.stage] || job.stage,
    );
    text("progress-percent", (job.progress || 0) + "%");
    $("progress-bar").style.width = (job.progress || 0) + "%";
    text("progress-detail", job.detail);
    $("cancel-run").hidden = !running;
    $("launch-experiment").disabled = running;
    if (running) {
      clearTimeout(state.timer);
      state.timer = setTimeout(() => loadRun(run.id, true), 1600);
    }
  }

  async function loadRun(id, poll = false) {
    clearTimeout(state.timer);
    state.selected = id;
    try {
      let run = await api(`/api/backtest/runs/${id}?report=${!poll}`);
      if (state.selected !== id) return;
      if (
        poll &&
        run.status === "completed" &&
        (state.report?.run_id !== id ||
          run.final_status?.status === "completed" ||
          run.final_status?.status === "failed")
      )
        run = await api(`/api/backtest/runs/${id}`);
      if (state.selected !== id) return;
      if (run.report) renderReport(run);
      else if (state.report?.run_id !== id) clearReport(run);
      progress(run);
      if (run.status === "completed" && !state.busy) refreshRuns(false);
      const url = new URL(window.location.href);
      url.searchParams.set("run", id);
      history.replaceState({}, "", url);
    } catch (exc) {
      error(exc.message);
      $("launch-experiment").disabled = false;
    }
  }

  async function refreshRuns(select = false) {
    try {
      const data = await api("/api/backtest/runs");
      text("run-count", data.runs.length);
      $("run-history").replaceChildren(
        ...data.runs.map((run) => {
          const tr = el("tr");
          const id = el("td", run.id.slice(0, 8));
          const universe = el("td");
          universe.append(
            el("span", run.source?.toUpperCase() || "—"),
            el("small", (run.symbols || []).join(" · "), "lab-mini"),
          );
          const status = el("td");
          status.append(
            el(
              "span",
              {
                completed: "Завершён",
                running: "Выполняется",
                failed: "Ошибка",
                cancelled: "Отменён",
                interrupted: "Прерван",
              }[run.status] || run.status,
              "lab-status " + run.status,
            ),
          );
          const action = el("td");
          const button = el("button", "Открыть ↗", "lab-button ghost");
          button.type = "button";
          button.addEventListener("click", () => {
            tab("overview");
            loadRun(run.id);
          });
          action.append(button);
          tr.append(
            id,
            universe,
            el("td", run.interval?.toUpperCase() || "—"),
            el("td", date(run.started_at)),
            status,
            action,
          );
          return tr;
        }),
      );
      if (!data.runs.length) {
        const tr = el("tr");
        const td = el(
          "td",
          "Пока нет экспериментов. Запустите DEMO или ISS MOEX.",
          "lab-empty",
        );
        td.colSpan = 6;
        tr.append(td);
        $("run-history").append(tr);
      }
      if (select) {
        const requested = new URLSearchParams(location.search).get("run");
        const run =
          data.runs.find((r) => r.id === requested) ||
          data.runs.find((r) => r.status === "running") ||
          data.runs.find((r) => r.status === "completed") ||
          data.runs[0];
        if (run) await loadRun(run.id);
      }
    } catch (exc) {
      error(exc.message);
    }
  }
  $("refresh-runs").addEventListener("click", () => refreshRuns(false));
  $("cancel-run").addEventListener("click", async () => {
    if (!state.selected) return;
    try {
      await api(`/api/backtest/runs/${state.selected}/cancel`, {
        method: "POST",
        body: "{}",
      });
      await loadRun(state.selected);
      refreshRuns(false);
    } catch (exc) {
      error(exc.message);
    }
  });

  function fraction(value) {
    // Percent → fraction как десятичный текст, без binary-money arithmetic.
    const [whole, decimals = ""] = String(value).split(".");
    const digits = (whole + decimals)
      .replace(/^0+(?=\d)/, "")
      .padStart(decimals.length + 3, "0");
    const split = digits.length - decimals.length - 2;
    return digits.slice(0, split) + "." + digits.slice(split);
  }
  $("experiment-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    const form = new FormData(event.currentTarget);
    const payload = {
      source: form.get("source"),
      symbols: String(form.get("symbols"))
        .split(/[\s,;]+/)
        .filter(Boolean)
        .map((s) => s.toUpperCase()),
      start: form.get("start"),
      end: form.get("end"),
      interval: form.get("interval"),
      horizon: Number(form.get("horizon")),
      freeze_months: Number(form.get("freeze_months")),
      iterations: Number(form.get("iterations")),
      folds: Number(form.get("folds")),
      cpcv_groups: Number(form.get("cpcv_groups")),
      embargo_bars: Number(form.get("embargo_bars")),
      cpcv: form.has("cpcv"),
      layer_b: form.has("layer_b"),
      seed: Number(form.get("seed")),
      additional_trials: Number(form.get("additional_trials")),
      l1_auxiliary: form.has("l1_auxiliary"),
      mlflow_tracking: form.has("mlflow_tracking"),
      initial_capital: form.get("initial_capital"),
      risk: {
        trend_threshold: form.get("trend_threshold"),
        commission_bps: form.get("commission_bps"),
        slippage_bps: form.get("slippage_bps"),
        risk_per_trade: fraction(form.get("risk_per_trade")),
        max_daily_drawdown: fraction(form.get("max_daily_drawdown")),
      },
    };
    $("launch-experiment").disabled = true;
    try {
      const run = await api("/api/backtest/runs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.report = null;
      state.asset = null;
      await loadRun(run.id);
      refreshRuns(false);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (exc) {
      error(exc.message);
      $("launch-experiment").disabled = false;
    }
  });
  $("final-open").addEventListener("click", () => {
    $("final-confirmation").value = "";
    $("final-confirm").disabled = true;
    $("final-dialog").showModal();
  });
  $("final-confirmation").addEventListener("input", (event) => {
    $("final-confirm").disabled = event.target.value !== "ОТКРЫТЬ FINAL OOS";
  });
  $("final-cancel").addEventListener("click", () => $("final-dialog").close());
  $("final-confirm").addEventListener("click", async () => {
    const id = state.selected;
    $("final-confirm").disabled = true;
    try {
      await api(`/api/backtest/runs/${id}/final`, {
        method: "POST",
        body: JSON.stringify({ confirmation: $("final-confirmation").value }),
      });
      $("final-dialog").close();
      await loadRun(id);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (exc) {
      $("final-dialog").close();
      error(exc.message);
    }
  });

  const colors = {
    strategy: "#62dfc4",
    imoex: "#899b9e",
    ma: "#d7bc7a",
    rsi: "#b998ef",
    trend: "#62dfc4",
    up: "#7ba9ff",
    break: "#f2aa72",
  };
  function canvasContext(canvas) {
    const width = canvas.clientWidth,
      height = canvas.clientHeight;
    if (!width || !height) return null;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, width, height };
  }
  function lineChart(id, rows, series, options = {}) {
    const canvas = $(id),
      c = canvasContext(canvas);
    if (!c) return;
    const { ctx, width, height } = c,
      left = options.probability ? 43 : 65,
      right = 18,
      top = 15,
      bottom = 30;
    const w = width - left - right,
      h = height - top - bottom;
    const values = rows
      .flatMap((row) => series.map((s) => row[s.key]))
      .filter((value) => value != null && Number.isFinite(Number(value)))
      .map(Number);
    let min = options.probability ? 0 : Math.min(...values),
      max = options.probability ? 1 : Math.max(...values);
    if (!values.length) {
      min = options.probability ? 0 : 900000;
      max = options.probability ? 1 : 1100000;
    }
    if (max === min) {
      min *= 0.99;
      max *= 1.01;
    }
    if (!options.probability) {
      const padding = (max - min) * 0.14;
      min -= padding;
      max += padding;
    }
    const y = (value) => top + h - ((value - min) / (max - min)) * h;
    const x = (index) => left + (index / Math.max(1, rows.length - 1)) * w;
    const light = document.documentElement.dataset.theme === "light";
    ctx.font = "9px system-ui";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const value = min + ((max - min) * i) / 4,
        py = y(value);
      ctx.strokeStyle = light ? "#dce7e2" : "#28363b";
      ctx.lineWidth = 0.6;
      ctx.beginPath();
      ctx.moveTo(left, py);
      ctx.lineTo(left + w, py);
      ctx.stroke();
      ctx.fillStyle = light ? "#607a7c" : "#668185";
      ctx.textAlign = "right";
      ctx.fillText(
        options.probability
          ? percent(value, 0)
          : (value / 1000000).toFixed(2) + " млн",
        left - 10,
        py,
      );
    }
    if (!rows.length) return;
    ctx.textAlign = "center";
    const ticks = Math.min(5, rows.length);
    for (let i = 0; i < ticks; i++) {
      const index = Math.round(
        (i * (rows.length - 1)) / Math.max(1, ticks - 1),
      );
      const moment = rows[index][options.dateKey || "date"];
      ctx.fillText(
        new Date(moment).toLocaleDateString("ru-RU", {
          month: "short",
          ...(options.intraday ? { day: "2-digit" } : { year: "2-digit" }),
        }),
        x(index),
        height - 10,
      );
    }
    for (const s of series) {
      ctx.beginPath();
      let started = false;
      rows.forEach((row, index) => {
        const value = row[s.key];
        if (value == null || !Number.isFinite(Number(value))) {
          started = false;
          return;
        }
        const px = x(index),
          py = y(Number(value));
        if (!started) {
          ctx.moveTo(px, py);
          started = true;
        } else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.key === "strategy" ? 1.9 : 1.2;
      ctx.setLineDash(s.key === "imoex" ? [4, 4] : []);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    canvas.onmousemove = (event) => {
      const index = Math.max(
        0,
        Math.min(
          rows.length - 1,
          Math.round(((event.offsetX - left) / w) * (rows.length - 1)),
        ),
      );
      const row = rows[index];
      canvas.title =
        date(row[options.dateKey || "date"]) +
        " · " +
        series
          .map(
            (s) =>
              s.name +
              ": " +
              (options.probability ? percent(row[s.key]) : money(row[s.key])),
          )
          .join(" · ");
    };
  }
  function calibrationChart() {
    const c = canvasContext($("calibration-chart"));
    if (!c) return;
    const { ctx, width, height } = c,
      left = 42,
      top = 15,
      w = width - 65,
      h = height - 47;
    const x = (v) => left + v * w,
      y = (v) => top + h - v * h;
    ctx.font = "9px system-ui";
    ctx.fillStyle = "#698286";
    ctx.strokeStyle = "#2d3e42";
    ctx.lineWidth = 0.6;
    for (let i = 0; i <= 4; i++) {
      const value = i / 4;
      ctx.beginPath();
      ctx.moveTo(x(0), y(value));
      ctx.lineTo(x(1), y(value));
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.fillText(percent(value, 0), left - 8, y(value) + 3);
      ctx.textAlign = "center";
      ctx.fillText(percent(value, 0), x(value), height - 10);
    }
    ctx.strokeStyle = "#6f878988";
    ctx.setLineDash([4, 5]);
    ctx.beginPath();
    ctx.moveTo(x(0), y(0));
    ctx.lineTo(x(1), y(1));
    ctx.stroke();
    ctx.setLineDash([]);
    const head = $("calibration-head").value;
    const report = state.report?.probabilities?.[head];
    if (!report) return;
    const bins = (report.reliability || []).filter((bin) => bin.count > 0);
    ctx.strokeStyle = colors[head];
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    bins.forEach((bin, index) => {
      if (index === 0) ctx.moveTo(x(bin.predicted), y(bin.observed));
      else ctx.lineTo(x(bin.predicted), y(bin.observed));
    });
    ctx.stroke();
    bins.forEach((bin) => {
      ctx.beginPath();
      ctx.fillStyle = colors[head];
      ctx.arc(
        x(bin.predicted),
        y(bin.observed),
        Math.min(5, 2 + Math.sqrt(bin.count) / 10),
        0,
        2 * Math.PI,
      );
      ctx.fill();
    });
    text(
      "brier-note",
      `OOS Brier ${number(report.brier, 3)} · raw ${number(report.raw_brier, 3)} · n=${report.samples}${head === "up" ? " (только trend)" : ""}`,
    );
  }
  function drawCharts() {
    lineChart("equity-chart", state.report?.equity_curve || [], [
      { key: "imoex", color: colors.imoex, name: "IMOEX" },
      { key: "ma", color: colors.ma, name: "EMA" },
      { key: "rsi", color: colors.rsi, name: "RSI" },
      { key: "strategy", color: colors.strategy, name: "Модель" },
    ]);
    const points = state.report?.probability_series?.[state.asset] || [];
    lineChart(
      "probability-chart",
      points,
      [
        { key: "p_trend", color: colors.trend, name: "P(trend)" },
        { key: "p_up", color: colors.up, name: "P(up|trend)" },
        { key: "p_break", color: colors.break, name: "P(break)" },
      ],
      { probability: true, dateKey: "asof", intraday: true },
    );
    calibrationChart();
  }
  new ResizeObserver(drawCharts).observe(root);
  new MutationObserver(drawCharts).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.selected)
      loadRun(state.selected, true);
  });
  refreshRuns(true);
  drawCharts();
})();
