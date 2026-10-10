from __future__ import annotations

import os

import lightgbm as lgb
import pandas as pd

from src.features.build_features import FEATURE_COLUMNS


def heuristic_win_score(race_df: pd.DataFrame) -> pd.Series:
    score = (
        race_df["course_base_win_rate"].fillna(0.1) * 0.5
        + (race_df["national_win_rate"].fillna(5.0) / 10) * 0.25
        + (race_df["motor_2rate"].fillna(30.0) / 100) * 0.15
        + ((7 - race_df["exhibition_rank"].fillna(3.5)) / 6) * 0.10
    )
    return score


def predict_win_probabilities(model: lgb.Booster | None, race_df: pd.DataFrame) -> pd.DataFrame:
    # v2モデル(1着・2着・3着の3モデル)があればそちらを使う
    v2 = _load_v2()
    if v2 is not None:
        return _predict_v2(v2, race_df)

    if model is None:
        raw_scores = heuristic_win_score(race_df).values
        exp_scores = pd.Series(raw_scores).apply(lambda x: 2.718281828 ** x)
        probabilities = exp_scores / exp_scores.sum()
    else:
        # モデルの出力はすでに確率(0〜1)。以前はここで exp() を掛けてから
        # 正規化していたため、60%の本命と10%の艇がほぼ同じ確率に
        # つぶれていた(2026-10-11発覚)。そのまま合計1に正規化する。
        raw = pd.Series(model.predict(race_df[FEATURE_COLUMNS]))
        probabilities = raw / raw.sum()

    result = race_df.copy()
    result["predicted_win_prob"] = probabilities.values
    return result


V2_DIR = "model_v2"
_V2_CACHE: dict = {}


def _load_v2():
    if "models" in _V2_CACHE:
        return _V2_CACHE["models"]
    models = None
    if all(os.path.exists(os.path.join(V2_DIR, f"{n}.txt")) for n in ("win", "second", "third")):
        models = {n: lgb.Booster(model_file=os.path.join(V2_DIR, f"{n}.txt")) for n in ("win", "second", "third")}
        print("v2モデル(1着・2着・3着)を使用")
    _V2_CACHE["models"] = models
    return models


def _predict_v2(models, race_df: pd.DataFrame) -> pd.DataFrame:
    from src.features.v2 import FEATURES_V2, add_race_features, add_table_priors

    df = add_race_features(race_df.sort_values("lane_number"))
    df = add_table_priors(df, V2_DIR)
    out = race_df.sort_values("lane_number").copy()
    for name, col in (("win", "p1"), ("second", "p2"), ("third", "p3")):
        p = pd.Series(models[name].predict(df[FEATURES_V2]), index=out.index)
        out[col] = p / p.sum()
    out["predicted_win_prob"] = out["p1"]
    return out


def add_original_index(race_df: pd.DataFrame) -> pd.DataFrame:
    df = race_df.copy()
    implied_prob = 1 / df["win_odds"].fillna(10)
    market_prob = implied_prob / implied_prob.sum()

    df["market_prob"] = market_prob
    df["original_index"] = (df["predicted_win_prob"] / df["market_prob"] * 100).round(1)
    return df.sort_values("original_index", ascending=False)
