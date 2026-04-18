"""ML model for stock price prediction using Gradient Boosting.

Uses walk-forward validation to avoid look-ahead bias:
- Train on historical data up to time T
- Predict and evaluate on T+1 to T+N
- Slide the window forward and repeat
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
)

from .ml_features import engineer_features, create_target, get_feature_columns, prepare_dataset


MODEL_DIR = Path(__file__).parent.parent / "models"


class MLPredictor:
    """Gradient Boosting model for stock buy/sell prediction."""

    def __init__(self, params: dict = None):
        model_params = params or {}
        self.model = GradientBoostingClassifier(
            n_estimators=model_params.get("n_estimators", 200),
            max_depth=model_params.get("max_depth", 4),
            learning_rate=model_params.get("learning_rate", 0.05),
            subsample=model_params.get("subsample", 0.8),
            min_samples_leaf=model_params.get("min_samples_leaf", 20),
            max_features=model_params.get("max_features", "sqrt"),
            random_state=42,
        )
        self.feature_cols = get_feature_columns()
        self.is_trained = False
        self.training_metrics: dict = {}

    def train(self, X: pd.DataFrame, y: pd.Series) -> dict:
        """Train the model on prepared features.

        Args:
            X: Feature DataFrame
            y: Target Series (0/1)

        Returns:
            Dict with training metrics
        """
        self.model.fit(X[self.feature_cols], y)
        self.is_trained = True

        # Training accuracy
        train_pred = self.model.predict(X[self.feature_cols])
        train_proba = self.model.predict_proba(X[self.feature_cols])[:, 1]

        self.training_metrics = {
            "samples": len(y),
            "positive_pct": f"{y.mean() * 100:.1f}%",
            "accuracy": accuracy_score(y, train_pred),
            "precision": precision_score(y, train_pred, zero_division=0),
            "recall": recall_score(y, train_pred, zero_division=0),
            "f1": f1_score(y, train_pred, zero_division=0),
            "auc_roc": roc_auc_score(y, train_proba) if len(y.unique()) > 1 else 0,
        }

        return self.training_metrics

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Predict probability of positive class (price going up).

        Returns:
            Array of probabilities [0, 1]
        """
        if not self.is_trained:
            raise RuntimeError("Model not trained. Run train() first.")
        return self.model.predict_proba(X[self.feature_cols])[:, 1]

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Predict binary class."""
        if not self.is_trained:
            raise RuntimeError("Model not trained. Run train() first.")
        return self.model.predict(X[self.feature_cols])

    def get_feature_importance(self, top_n: int = 20) -> List[Tuple[str, float]]:
        """Get top N most important features."""
        if not self.is_trained:
            return []
        importances = self.model.feature_importances_
        indices = np.argsort(importances)[::-1][:top_n]
        return [(self.feature_cols[i], importances[i]) for i in indices]

    def save(self, name: str = "default"):
        """Save model to disk."""
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        model_path = MODEL_DIR / f"{name}.pkl"
        meta_path = MODEL_DIR / f"{name}_meta.json"

        with open(model_path, "wb") as f:
            pickle.dump(self.model, f)

        with open(meta_path, "w") as f:
            json.dump({
                "feature_cols": self.feature_cols,
                "training_metrics": {
                    k: float(v) if isinstance(v, (np.floating, float)) else v
                    for k, v in self.training_metrics.items()
                },
                "is_trained": self.is_trained,
            }, f, indent=2)

    def load(self, name: str = "default") -> bool:
        """Load model from disk. Returns True if successful."""
        model_path = MODEL_DIR / f"{name}.pkl"
        meta_path = MODEL_DIR / f"{name}_meta.json"

        if not model_path.exists():
            return False

        with open(model_path, "rb") as f:
            self.model = pickle.load(f)

        if meta_path.exists():
            with open(meta_path) as f:
                meta = json.load(f)
                self.training_metrics = meta.get("training_metrics", {})

        self.is_trained = True
        return True


def walk_forward_evaluate(
    data: Dict[str, pd.DataFrame],
    params: dict,
    benchmark_df: pd.DataFrame = None,
    n_splits: int = 5,
    forward_days: int = 5,
    threshold: float = 0.03,
    ml_params: dict = None,
) -> dict:
    """Walk-forward validation — the gold standard for financial ML.

    Splits time series into sequential folds:
    - Fold 1: Train [0..T1], Test [T1..T2]
    - Fold 2: Train [0..T2], Test [T2..T3]
    - etc.

    Never lets the model see future data.

    Returns:
        Dict with per-fold and aggregate metrics
    """
    X, y = prepare_dataset(data, params, benchmark_df, forward_days, threshold)

    if len(X) < 200:
        return {"error": f"Not enough data ({len(X)} samples). Need at least 200."}

    # Sort by date index
    X = X.sort_index()
    y = y.loc[X.index]

    fold_size = len(X) // (n_splits + 1)
    results = []

    for fold in range(n_splits):
        train_end = fold_size * (fold + 2)
        test_start = train_end
        test_end = min(test_start + fold_size, len(X))

        if test_end <= test_start:
            break

        X_train = X.iloc[:train_end]
        y_train = y.iloc[:train_end]
        X_test = X.iloc[test_start:test_end]
        y_test = y.iloc[test_start:test_end]

        if len(y_train.unique()) < 2 or len(y_test) < 10:
            continue

        model = MLPredictor(ml_params)

        # Replace inf
        X_train = X_train.replace([np.inf, -np.inf], np.nan).fillna(0)
        X_test = X_test.replace([np.inf, -np.inf], np.nan).fillna(0)

        model.train(X_train, y_train)
        pred = model.predict(X_test)
        proba = model.predict_proba(X_test)

        fold_result = {
            "fold": fold + 1,
            "train_size": len(X_train),
            "test_size": len(X_test),
            "accuracy": accuracy_score(y_test, pred),
            "precision": precision_score(y_test, pred, zero_division=0),
            "recall": recall_score(y_test, pred, zero_division=0),
            "f1": f1_score(y_test, pred, zero_division=0),
        }

        try:
            fold_result["auc_roc"] = roc_auc_score(y_test, proba)
        except ValueError:
            fold_result["auc_roc"] = 0.0

        results.append(fold_result)

    if not results:
        return {"error": "No valid folds produced"}

    # Aggregate
    avg_metrics = {}
    for key in ["accuracy", "precision", "recall", "f1", "auc_roc"]:
        values = [r[key] for r in results]
        avg_metrics[key] = np.mean(values)
        avg_metrics[f"{key}_std"] = np.std(values)

    return {
        "folds": results,
        "average": avg_metrics,
        "total_samples": len(X),
        "positive_rate": f"{y.mean() * 100:.1f}%",
    }


def train_final_model(
    data: Dict[str, pd.DataFrame],
    params: dict,
    benchmark_df: pd.DataFrame = None,
    forward_days: int = 5,
    threshold: float = 0.03,
    ml_params: dict = None,
    save_name: str = "default",
) -> Tuple[MLPredictor, dict]:
    """Train final model on all available data and save it.

    Returns:
        Tuple of (trained model, training metrics)
    """
    X, y = prepare_dataset(data, params, benchmark_df, forward_days, threshold)

    if len(X) < 100:
        raise ValueError(f"Not enough data ({len(X)} samples). Need at least 100.")

    # Replace inf
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

    model = MLPredictor(ml_params)
    metrics = model.train(X, y)
    model.save(save_name)

    return model, metrics
