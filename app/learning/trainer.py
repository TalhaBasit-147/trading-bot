"""Nightly training job.

Pulls recent closed trades from the DB, builds features+labels, trains a
LightGBM binary classifier (win vs not-win), evaluates with a time-ordered
holdout, and saves the artifact.

Walk-forward: we use the oldest 80% as train, most-recent 20% as validation.
This avoids leakage from future trades into past inference.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from loguru import logger
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import LabelEncoder

from app.config import settings
from app.db.repo import recent_closed_trades, save_model_version
from app.learning.features import ALL_FEATURES, CATEGORICAL_FEATURES, NUMERIC_FEATURES, feature_dict_to_row


def _build_dataset(limit: int = 2000) -> Tuple[pd.DataFrame, np.ndarray]:
    rows = recent_closed_trades(limit)
    if not rows:
        return pd.DataFrame(columns=ALL_FEATURES), np.array([])
    records = []
    labels = []
    # oldest first (chronological)
    for t in reversed(rows):
        feat = t.features or {}
        feat = feature_dict_to_row(feat)
        feat["_ts"] = t.open_ts
        records.append(feat)
        labels.append(1 if (t.outcome or "").upper() == "WIN" else 0)
    df = pd.DataFrame(records)
    return df, np.array(labels, dtype=int)


def _encode(df: pd.DataFrame, encoders: Optional[dict] = None) -> Tuple[pd.DataFrame, dict]:
    encoders = encoders or {}
    out = df.copy()
    for c in CATEGORICAL_FEATURES:
        enc = encoders.get(c)
        if enc is None:
            enc = LabelEncoder()
            out[c] = enc.fit_transform(out[c].astype(str))
            encoders[c] = enc
        else:
            # unseen categories → map to a sentinel bucket
            known = set(enc.classes_)
            out[c] = out[c].astype(str).map(lambda v: v if v in known else enc.classes_[0])
            out[c] = enc.transform(out[c])
    return out, encoders


def train(min_trades: Optional[int] = None) -> Optional[str]:
    min_trades = min_trades or settings.ML_MIN_TRADES
    df, y = _build_dataset()
    if len(df) < min_trades:
        logger.info(f"Not enough trades to train: have {len(df)}, need {min_trades}")
        return None

    df = df.sort_values("_ts").reset_index(drop=True)
    X = df[ALL_FEATURES]
    X, encoders = _encode(X)
    cut = int(len(X) * 0.8)
    X_tr, X_val = X.iloc[:cut], X.iloc[cut:]
    y_tr, y_val = y[:cut], y[cut:]

    model = lgb.LGBMClassifier(
        n_estimators=400,
        learning_rate=0.03,
        num_leaves=31,
        max_depth=-1,
        min_child_samples=10,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_alpha=0.1,
        reg_lambda=0.1,
        random_state=42,
        verbose=-1,
    )
    model.fit(
        X_tr, y_tr,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(30)],
    )
    p_val = model.predict_proba(X_val)[:, 1]
    auc = float(roc_auc_score(y_val, p_val)) if len(set(y_val)) > 1 else float("nan")
    brier = float(brier_score_loss(y_val, p_val))
    logger.info(f"Trained on {len(X_tr)} / val {len(X_val)} trades. AUC={auc:.3f} Brier={brier:.3f}")

    path = Path(settings.ML_MODEL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": model,
        "encoders": encoders,
        "features": ALL_FEATURES,
        "numeric": NUMERIC_FEATURES,
        "categorical": CATEGORICAL_FEATURES,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "auc": auc,
        "brier": brier,
        "n_train": int(len(X_tr)),
        "n_val": int(len(X_val)),
    }
    joblib.dump(payload, path)
    save_model_version(str(path), int(len(X)), auc, brier, notes="walk-forward 80/20")
    logger.info(f"Model saved → {path}")
    return str(path)


if __name__ == "__main__":
    train()
