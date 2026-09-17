from __future__ import annotations

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
    if model is None:
        raw_scores = heuristic_win_score(race_df).values
    else:
        raw_scores = model.predict(race_df[FEATURE_COLUMNS])
    exp_scores = pd.Series(raw_scores).apply(lambda x: 2.718281828 ** x)
    probabilities = exp_scores / exp_scores.sum()

    result = race_df.copy()
    result["predicted_win_prob"] = probabilities.values
    return result


def add_original_index(race_df: pd.DataFrame) -> pd.DataFrame:
    df = race_df.copy()
    implied_prob = 1 / df["win_odds"].fillna(10)
    market_prob = implied_prob / implied_prob.sum()

    df["market_prob"] = market_prob
    df["original_index"] = (df["predicted_win_prob"] / df["market_prob"] * 100).round(1)
    return df.sort_values("original_index", ascending=False)
