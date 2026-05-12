"""Live inference wrapper.

Loads the latest model on startup, exposes `score(features_dict) -> prob`.
Safe to call if no model exists yet (returns None → engine falls back to rules only).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import joblib
import pandas as pd
from loguru import logger

from app.config import settings
from app.learning.features import ALL_FEATURES, CATEGORICAL_FEATURES, feature_dict_to_row


class Scorer:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or settings.ML_MODEL_PATH)
        self.payload = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            logger.info(f"No model at {self.path} yet — rules-only mode.")
            return
        try:
            self.payload = joblib.load(self.path)
            logger.info(f"Loaded model: trained_at={self.payload.get('trained_at')} AUC={self.payload.get('auc')}")
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            self.payload = None

    def reload(self) -> None:
        self._load()

    def available(self) -> bool:
        return self.payload is not None

    def score(self, features: dict) -> Optional[float]:
        if self.payload is None:
            return None
        try:
            row = feature_dict_to_row(features)
            df = pd.DataFrame([row])[ALL_FEATURES].copy()
            encoders = self.payload["encoders"]
            for c in CATEGORICAL_FEATURES:
                enc = encoders[c]
                val = str(df[c].iloc[0])
                if val not in set(enc.classes_):
                    val = enc.classes_[0]
                df[c] = enc.transform([val])
            prob = float(self.payload["model"].predict_proba(df)[:, 1][0])
            return prob
        except Exception as e:
            logger.warning(f"Scorer failed: {e}")
            return None
