"""Сохранение и загрузка обученных весов «Синтетического трейдера» на диск (``data/models/``).

Предоставляет прозрачное и проверяемое хранилище весов нейросети (Temporal MLP)
и трёхголового градиентного бустинга (CatBoost Triad):
- Каждый чекпоинт сохраняется в человекочитаемый JSON-файл
  ``data/models/weights_{ticker}_{timeframe}.json`` с тензорами весов ``W1, b1, W2, b2, W3, b3``,
  структурой симметричных деревьев, кривыми потерь (Loss Curve) по эпохам и SHA-256 хешем.
- Поддерживает как обучение с нуля с записью весов на диск, так и прямое чтение
  уже сохранённых весов с диска для честного инференса без переобучения.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.synthetic.catboost_model import SyntheticCatBoostTriadModel
from core.synthetic.neural_model import TemporalMLPNetwork

DEFAULT_MODELS_DIR = Path("data/models")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelWeightsManifest:
    """Паспорт сохранённых весов модели на диске для отображения оператору в GUI."""

    checkpoint_path: str
    file_exists: bool
    file_size_bytes: int
    sha256_fingerprint: str
    trained_at_utc: str
    loaded_from_disk: bool
    train_bars_count: int
    oos_bars_count: int
    selected_features: tuple[str, ...]
    # Параметры нейросети
    nn_architecture: str
    nn_backend: str
    nn_total_parameters: int
    nn_epochs: int
    nn_initial_loss: float
    nn_final_loss: float
    nn_val_loss: float
    nn_train_loss_curve: tuple[float, ...]
    nn_val_loss_curve: tuple[float, ...]
    nn_weight_sample: tuple[tuple[float, ...], ...]
    # Параметры бустинга
    gbdt_backend: str
    gbdt_total_trees: int
    gbdt_total_leaves: int
    gbdt_val_logloss_trend: float
    gbdt_val_logloss_up: float
    gbdt_val_logloss_break: float


def checkpoint_file_path(
    ticker: str,
    timeframe: str,
    *,
    models_dir: Path = DEFAULT_MODELS_DIR,
) -> Path:
    safe_ticker = "".join(ch for ch in ticker.upper() if ch.isalnum()) or "SBER"
    safe_tf = "".join(ch for ch in timeframe.lower() if ch.isalnum()) or "1h"
    return models_dir / f"weights_{safe_ticker}_{safe_tf}.json"


def save_model_checkpoint(
    *,
    ticker: str,
    timeframe: str,
    triad_model: SyntheticCatBoostTriadModel,
    neural_net: TemporalMLPNetwork,
    selected_features: tuple[str, ...],
    train_bars_count: int,
    oos_bars_count: int,
    models_dir: Path = DEFAULT_MODELS_DIR,
) -> ModelWeightsManifest:
    """Сериализует обученные веса нейросети и бустинга в ``data/models/weights_*.json``."""
    path = checkpoint_file_path(ticker, timeframe, models_dir=models_dir)
    trained_at = datetime.now(tz=UTC).isoformat()

    nn_dict = neural_net.to_weights_dict()
    gbdt_dict = triad_model.to_weights_dict()

    payload: dict[str, Any] = {
        "schema_version": "synthetic-trader-weights-v2",
        "ticker": ticker.upper(),
        "timeframe": timeframe.lower(),
        "trained_at_utc": trained_at,
        "train_bars_count": train_bars_count,
        "oos_bars_count": oos_bars_count,
        "selected_features": list(selected_features),
        "neural_network": nn_dict,
        "catboost_triad": gbdt_dict,
    }

    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    raw_bytes = serialized.encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()[:16]

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw_bytes)
        file_exists = path.exists()
        file_size = path.stat().st_size if file_exists else len(raw_bytes)
    except OSError:
        file_exists = False
        file_size = len(raw_bytes)

    return _build_manifest_from_payload(
        payload,
        checkpoint_path=str(path),
        file_exists=file_exists,
        file_size_bytes=file_size,
        sha256_fingerprint=digest,
        loaded_from_disk=False,
    )


def load_model_checkpoint(
    *,
    ticker: str,
    timeframe: str,
    triad_model: SyntheticCatBoostTriadModel,
    neural_net: TemporalMLPNetwork,
    expected_feature_count: int | None = None,
    models_dir: Path = DEFAULT_MODELS_DIR,
) -> ModelWeightsManifest | None:
    """Загружает сохранённые веса нейросети и бустинга с диска, если файл существует."""
    path = checkpoint_file_path(ticker, timeframe, models_dir=models_dir)
    if not path.exists():
        return None
    try:
        raw_bytes = path.read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
        payload = json.loads(raw_bytes.decode("utf-8"))
        saved_features = tuple(str(x) for x in payload.get("selected_features", []))
        if (
            expected_feature_count is not None
            and len(saved_features) != expected_feature_count
        ):
            return None
        ok_nn = neural_net.load_weights_dict(payload["neural_network"])
        ok_gb = triad_model.load_weights_dict(payload["catboost_triad"])
        if not (ok_nn and ok_gb):
            return None
        return _build_manifest_from_payload(
            payload,
            checkpoint_path=str(path),
            file_exists=True,
            file_size_bytes=len(raw_bytes),
            sha256_fingerprint=digest,
            loaded_from_disk=True,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _build_manifest_from_payload(
    payload: dict[str, Any],
    *,
    checkpoint_path: str,
    file_exists: bool,
    file_size_bytes: int,
    sha256_fingerprint: str,
    loaded_from_disk: bool,
) -> ModelWeightsManifest:
    nn = payload.get("neural_network", {})
    gb = payload.get("catboost_triad", {})
    tr_curve = tuple(float(x) for x in nn.get("train_loss_curve", [0.693]))
    val_curve = tuple(float(x) for x in nn.get("val_loss_curve", [0.693]))
    w1_raw = nn.get("W1", [])
    w_sample = tuple(
        tuple(float(v) for v in row[:6]) for row in w1_raw[:4]
    )

    h_tr = gb.get("head_trend", {})
    h_up = gb.get("head_up", {})
    h_br = gb.get("head_break", {})

    return ModelWeightsManifest(
        checkpoint_path=checkpoint_path,
        file_exists=file_exists,
        file_size_bytes=file_size_bytes,
        sha256_fingerprint=sha256_fingerprint,
        trained_at_utc=str(payload.get("trained_at_utc", "")),
        loaded_from_disk=loaded_from_disk,
        train_bars_count=int(payload.get("train_bars_count", 0)),
        oos_bars_count=int(payload.get("oos_bars_count", 0)),
        selected_features=tuple(str(x) for x in payload.get("selected_features", [])),
        nn_architecture=str(nn.get("architecture", "MLP")),
        nn_backend=str(nn.get("backend", "Adam Backprop")),
        nn_total_parameters=int(nn.get("total_parameters", 0)),
        nn_epochs=int(nn.get("epochs", len(tr_curve))),
        nn_initial_loss=tr_curve[0] if tr_curve else 0.693,
        nn_final_loss=tr_curve[-1] if tr_curve else 0.693,
        nn_val_loss=val_curve[-1] if val_curve else 0.693,
        nn_train_loss_curve=tr_curve,
        nn_val_loss_curve=val_curve,
        nn_weight_sample=w_sample,
        gbdt_backend=str(h_tr.get("backend_name", "Oblivious GBDT")),
        gbdt_total_trees=int(gb.get("total_trees", 0)),
        gbdt_total_leaves=int(gb.get("total_leaves", 0)),
        gbdt_val_logloss_trend=float(h_tr.get("validation_log_loss", 0.5)),
        gbdt_val_logloss_up=float(h_up.get("validation_log_loss", 0.5)),
        gbdt_val_logloss_break=float(h_br.get("validation_log_loss", 0.5)),
    )
