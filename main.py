"""
IMOEX Candles Collector
------------------------
Собирает исторические свечи по всем акциям, входящим в индекс IMOEX,
а также по самому индексу IMOEX, через официальный T-Invest API SDK
от Т-Банка — пакет t-tech-investments.

Хранилище - SQLite (candles.db), PRIMARY KEY (ticker, timeframe, ts)
исключает дубликаты на уровне БД при повторных докачках.

GUI: вкладка "Лог" + вкладка "График" с переключением тикера и таймфрейма.
"""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
import tkinter as tk
import warnings
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import pandas as pd
import requests

# Глушим DeprecatedWarning от shares()/find_instrument() - методы рабочие,
# просто SDK предупреждает о будущей смене сигнатуры (request-модели с 1.0.0).
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Best-effort попытка отключить телеметрию Sentry, которую тянет SDK.
# Гарантий нет (SDK не документирует явный opt-out), но хуже не будет.
os.environ.setdefault("SENTRY_DSN", "")

import matplotlib
matplotlib.use("TkAgg")
import mplfinance as mpf
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

try:
    from t_tech.invest import CandleInterval, Client
    from t_tech.invest.utils import now
except ModuleNotFoundError:
    from t_tech.invest.grpc import CandleInterval, Client  # type: ignore
    from t_tech.invest.grpc.utils import now  # type: ignore

IMOEX_ANALYTICS_URL = (
    "https://iss.moex.com/iss/statistics/engines/stock/markets/index/analytics/IMOEX.json"
)
DB_PATH = Path("candles.db")

INTERVALS = {
    "1 минута": CandleInterval.CANDLE_INTERVAL_1_MIN,
    "5 минут": CandleInterval.CANDLE_INTERVAL_5_MIN,
    "15 минут": CandleInterval.CANDLE_INTERVAL_15_MIN,
    "1 час": CandleInterval.CANDLE_INTERVAL_HOUR,
    "1 день": CandleInterval.CANDLE_INTERVAL_DAY,
}
# timeframe в БД храним как стабильный enum-код (не зависит от языка интерфейса)
LABEL_TO_CODE = {label: interval.name for label, interval in INTERVALS.items()}
CODE_TO_LABEL = {v: k for k, v in LABEL_TO_CODE.items()}


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            ticker    TEXT    NOT NULL,
            timeframe TEXT    NOT NULL,
            ts        TEXT    NOT NULL,
            open      REAL    NOT NULL,
            high      REAL    NOT NULL,
            low       REAL    NOT NULL,
            close     REAL    NOT NULL,
            volume    INTEGER NOT NULL,
            PRIMARY KEY (ticker, timeframe, ts)
        )
        """
    )
    conn.commit()
    conn.close()


def save_candles(conn: sqlite3.Connection, ticker: str, timeframe: str, candles) -> int:
    """INSERT OR IGNORE - дубликаты по (ticker, timeframe, ts) физически невозможны."""
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM candles WHERE ticker=? AND timeframe=?", (ticker, timeframe)
    )
    before = cur.fetchone()[0]

    rows = [
        (
            ticker,
            timeframe,
            c.time.isoformat(),
            quotation_to_float(c.open),
            quotation_to_float(c.high),
            quotation_to_float(c.low),
            quotation_to_float(c.close),
            c.volume,
        )
        for c in candles
    ]
    cur.executemany(
        """
        INSERT OR IGNORE INTO candles
            (ticker, timeframe, ts, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    cur.execute(
        "SELECT COUNT(*) FROM candles WHERE ticker=? AND timeframe=?", (ticker, timeframe)
    )
    after = cur.fetchone()[0]
    return after - before


def list_tickers() -> list[str]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT ticker FROM candles ORDER BY ticker").fetchall()
    conn.close()
    return [r[0] for r in rows]


def list_timeframes(ticker: str) -> list[str]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT timeframe FROM candles WHERE ticker=? ORDER BY timeframe", (ticker,)
    ).fetchall()
    conn.close()
    return [CODE_TO_LABEL.get(r[0], r[0]) for r in rows]


def load_candles_df(ticker: str, timeframe_code: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT ts as Date, open as Open, high as High, low as Low,
               close as Close, volume as Volume
        FROM candles
        WHERE ticker=? AND timeframe=?
        ORDER BY ts
        """,
        conn,
        params=(ticker, timeframe_code),
        parse_dates=["Date"],
    )
    conn.close()
    return df.set_index("Date")


# --------------------------------------------------------------------------
# Данные о составе индекса и инструментах
# --------------------------------------------------------------------------

def get_imoex_tickers() -> list[str]:
    resp = requests.get(IMOEX_ANALYTICS_URL, params={"iss.meta": "off"}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    block = payload["analytics"]
    columns = block["columns"]
    data = block["data"]
    if "ticker" in columns:
        idx = columns.index("ticker")
    elif "secid" in columns:
        idx = columns.index("secid")
    else:
        raise RuntimeError(f"Не нашёл колонку с тикером, есть: {columns}")
    return sorted({row[idx] for row in data if row[idx]})


def build_share_map(client: Client) -> dict[str, str]:
    shares = client.instruments.shares().instruments
    return {s.ticker: s.uid for s in shares if s.class_code == "TQBR"}


def find_index_uid(client: Client, query: str = "IMOEX") -> str:
    found = client.instruments.find_instrument(query=query).instruments
    for i in found:
        if i.ticker == query:
            return i.uid
    if found:
        return found[0].uid
    raise RuntimeError(f"Индекс {query} не найден через find_instrument")


def quotation_to_float(q) -> float:
    return q.units + q.nano / 1e9


# --------------------------------------------------------------------------
# Скачивание свечей с retry на RESOURCE_EXHAUSTED
# --------------------------------------------------------------------------

def fetch_candles_with_retry(
    client, uid, from_, interval, ticker, log_q, stop_event, max_retries=5
):
    delay = 5
    for attempt in range(1, max_retries + 1):
        if stop_event.is_set():
            return []
        candles = []
        try:
            for c in client.get_all_candles(instrument_id=uid, from_=from_, interval=interval):
                candles.append(c)
                if stop_event.is_set():
                    break
            return candles
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            if "RESOURCE_EXHAUSTED" in msg:
                log_q.put(
                    f"    {ticker}: лимит запросов API (RESOURCE_EXHAUSTED), "
                    f"жду {delay}s, попытка {attempt}/{max_retries}"
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            log_q.put(f"[!] {ticker}: ошибка {msg}")
            return candles
    log_q.put(f"[!] {ticker}: превышено число попыток после RESOURCE_EXHAUSTED, пропуск")
    return []


def _days_delta(days: int):
    from datetime import timedelta
    return timedelta(days=days)


def run_download(token, days_back, interval_label, log_q, stop_event, progress_q, done_q):
    interval = INTERVALS[interval_label]
    timeframe_code = LABEL_TO_CODE[interval_label]
    conn = sqlite3.connect(DB_PATH)
    try:
        with Client(token) as client:
            log_q.put("Получаю состав индекса IMOEX с ISS MOEX...")
            tickers = get_imoex_tickers()
            log_q.put(f"В индексе {len(tickers)} инструментов")

            log_q.put("Загружаю справочник акций T-Invest API...")
            share_map = build_share_map(client)

            log_q.put("Ищу instrument_uid индекса IMOEX...")
            index_uid = find_index_uid(client, "IMOEX")

            targets = [("IMOEX", index_uid)]
            for t in tickers:
                uid = share_map.get(t)
                if uid:
                    targets.append((t, uid))
                else:
                    log_q.put(f"[!] {t}: нет в справочнике акций T-Invest API, пропуск")

            from_ = now() - _days_delta(days_back)
            total = len(targets)

            for n, (ticker, uid) in enumerate(targets, start=1):
                if stop_event.is_set():
                    log_q.put("Остановлено пользователем.")
                    break
                log_q.put(f"[{n}/{total}] {ticker}: скачиваю свечи...")
                candles = fetch_candles_with_retry(
                    client, uid, from_, interval, ticker, log_q, stop_event
                )
                if candles:
                    saved = save_candles(conn, ticker, timeframe_code, candles)
                    log_q.put(
                        f"    {ticker}: получено {len(candles)}, новых сохранено {saved}"
                    )
                progress_q.put((n, total))
                time.sleep(1.0)  # бережнее к лимитам API

            log_q.put("Готово.")
    except Exception as e:  # noqa: BLE001
        log_q.put(f"[ОШИБКА] {e}")
        log_q.put(
            "Если ошибка про SSL/certificate verify failed - "
            "поставь сертификаты Russian Trusted Root CA + Sub CA "
            "(см. сайт Госуслуг) в системное хранилище сертификатов."
        )
    finally:
        conn.close()
        done_q.put(True)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IMOEX Candles Collector — T-Invest API (t-tech-investments)")
        self.geometry("920x700")

        init_db()

        self.log_q: queue.Queue = queue.Queue()
        self.progress_q: queue.Queue = queue.Queue()
        self.done_q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.chart_canvas = None

        self._build_ui()
        self._bind_universal_clipboard()
        self._refresh_ticker_list()
        self.after(150, self._poll_queues)

    # ---------------- Фикс Ctrl+V/C/X/A на русской раскладке ----------------
    # Баг Tkinter на Windows: <Control-v> матчится по символу раскладки,
    # а не по физической клавише. event.keycode - код физической клавиши,
    # одинаковый в любой раскладке, поэтому биндим по нему.
    def _bind_universal_clipboard(self):
        self.bind_all("<Control-KeyPress>", self._on_ctrl_key, add="+")

    @staticmethod
    def _on_ctrl_key(event):
        widget = event.widget
        if not isinstance(widget, (tk.Entry, ttk.Entry)):
            return
        if event.keycode == 86:  # V
            widget.event_generate("<<Paste>>")
            return "break"
        if event.keycode == 67:  # C
            widget.event_generate("<<Copy>>")
            return "break"
        if event.keycode == 88:  # X
            widget.event_generate("<<Cut>>")
            return "break"
        if event.keycode == 65:  # A
            widget.selection_range(0, "end")
            return "break"

    # ---------------- UI построение ----------------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Токен T-Invest API:").grid(row=0, column=0, sticky="w")
        self.token_var = tk.StringVar(value=os.environ.get("INVEST_TOKEN", ""))
        ttk.Entry(top, textvariable=self.token_var, show="*", width=55).grid(
            row=0, column=1, columnspan=3, sticky="we", pady=4
        )

        ttk.Label(top, text="Интервал свечей:").grid(row=1, column=0, sticky="w")
        self.interval_var = tk.StringVar(value="1 день")
        ttk.Combobox(
            top,
            textvariable=self.interval_var,
            values=list(INTERVALS.keys()),
            state="readonly",
            width=14,
        ).grid(row=1, column=1, sticky="w")

        ttk.Label(top, text="Глубина истории, дней:").grid(row=1, column=2, sticky="w")
        self.days_var = tk.StringVar(value="365")
        ttk.Entry(top, textvariable=self.days_var, width=10).grid(row=1, column=3, sticky="w")

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="Начать загрузку", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Остановить", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **pad)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, **pad)

        log_tab = ttk.Frame(self.notebook)
        chart_tab = ttk.Frame(self.notebook)
        self.notebook.add(log_tab, text="Лог")
        self.notebook.add(chart_tab, text="График")

        self.log = scrolledtext.ScrolledText(log_tab, height=24, state="disabled")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)

        self._build_chart_tab(chart_tab)

    def _build_chart_tab(self, parent):
        controls = ttk.Frame(parent)
        controls.pack(fill="x", padx=4, pady=4)

        ttk.Label(controls, text="Инструмент:").pack(side="left")
        self.ticker_var = tk.StringVar()
        self.ticker_combo = ttk.Combobox(
            controls, textvariable=self.ticker_var, state="readonly", width=15
        )
        self.ticker_combo.pack(side="left", padx=6)
        self.ticker_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_timeframe_list())

        ttk.Label(controls, text="Таймфрейм:").pack(side="left", padx=(12, 0))
        self.timeframe_var = tk.StringVar()
        self.timeframe_combo = ttk.Combobox(
            controls, textvariable=self.timeframe_var, state="readonly", width=12
        )
        self.timeframe_combo.pack(side="left", padx=6)

        ttk.Button(
            controls, text="Обновить список", command=self._refresh_ticker_list
        ).pack(side="left", padx=4)
        ttk.Button(
            controls, text="Показать график", command=self.show_chart
        ).pack(side="left", padx=4)

        self.chart_frame = ttk.Frame(parent)
        self.chart_frame.pack(fill="both", expand=True, padx=4, pady=4)

    # ---------------- Логика ----------------

    def _refresh_ticker_list(self):
        tickers = list_tickers()
        self.ticker_combo["values"] = tickers
        if tickers and not self.ticker_var.get():
            self.ticker_var.set(tickers[0])
        self._refresh_timeframe_list()

    def _refresh_timeframe_list(self):
        ticker = self.ticker_var.get()
        if not ticker:
            self.timeframe_combo["values"] = []
            return
        timeframes = list_timeframes(ticker)
        self.timeframe_combo["values"] = timeframes
        if timeframes and self.timeframe_var.get() not in timeframes:
            self.timeframe_var.set(timeframes[0])

    def show_chart(self):
        ticker = self.ticker_var.get().strip()
        tf_label = self.timeframe_var.get().strip()
        if not ticker or not tf_label:
            messagebox.showwarning("Нет данных", "Выбери инструмент и таймфрейм")
            return

        tf_code = LABEL_TO_CODE.get(tf_label, tf_label)
        df = load_candles_df(ticker, tf_code)
        if df.empty:
            messagebox.showwarning("Пусто", "Для этой пары тикер/таймфрейм нет свечей")
            return

        if self.chart_canvas is not None:
            self.chart_canvas.get_tk_widget().destroy()
            self.chart_canvas = None

        fig = Figure(figsize=(9, 6), dpi=100)
        # Два отдельных подграфика друг под другом (не twinx!),
        # иначе объём визуально "заслоняет" свечи.
        gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.05)
        ax_candle = fig.add_subplot(gs[0])
        ax_volume = fig.add_subplot(gs[1], sharex=ax_candle)

        mpf.plot(
            df,
            type="candle",
            ax=ax_candle,
            volume=ax_volume,
            style="yahoo",
            datetime_format="%d-%m %H:%M",
            xrotation=20,
            warn_too_much_data=len(df) + 10,  # глушим предупреждение mplfinance
        )
        ax_candle.set_title(f"{ticker} ({tf_label})")
        ax_candle.set_xticklabels([])  # подписи дат - только на нижнем графике

        self.chart_canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        self.chart_canvas.draw()
        self.chart_canvas.get_tk_widget().pack(fill="both", expand=True)

    def _log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("Ошибка", "Укажите токен T-Invest API")
            return
        try:
            days_back = int(self.days_var.get())
        except ValueError:
            messagebox.showerror("Ошибка", "Глубина истории должна быть целым числом")
            return

        interval_label = self.interval_var.get()
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        self.worker = threading.Thread(
            target=run_download,
            args=(
                token,
                days_back,
                interval_label,
                self.log_q,
                self.stop_event,
                self.progress_q,
                self.done_q,
            ),
            daemon=True,
        )
        self.worker.start()

    def stop(self):
        self.stop_event.set()

    def _poll_queues(self):
        while not self.log_q.empty():
            self._log(self.log_q.get_nowait())
        while not self.progress_q.empty():
            n, total = self.progress_q.get_nowait()
            self.progress["maximum"] = total
            self.progress["value"] = n
        while not self.done_q.empty():
            self.done_q.get_nowait()
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.worker = None
            self._refresh_ticker_list()
        self.after(150, self._poll_queues)


if __name__ == "__main__":
    App().mainloop()