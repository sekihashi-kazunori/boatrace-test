from __future__ import annotations

import lightgbm as lgb
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

from src.features.build_features import FEATURE_COLUMNS


def train_win_probability_model(df: pd.DataFrame) -> lgb.Booster:
    df = df.dropna(subset=FEATURE_COLUMNS + ["target_win"])
    race_group = df["race_date"].astype(str) + "_" + df["stadium_code"].astype(str) + "_" + df["race_number"].astype(str)

    splitter = GroupShuffleSplit(test_size=0.2, n_splits=1, random_state=42)
    train_idx, valid_idx = next(splitter.split(df, groups=race_group))

    train_set = lgb.Dataset(df.iloc[train_idx][FEATURE_COLUMNS], label=df.iloc[train_idx]["target_win"])
    valid_set = lgb.Dataset(df.iloc[valid_idx][FEATURE_COLUMNS], label=df.iloc[valid_idx]["target_win"])

    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "verbose": -1,
    }

    model = lgb.train(
        params,
        train_set,
        num_boost_round=500,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
    )
    return model


def save_model(model: lgb.Booster, path: str = "model.txt") -> None:
    model.save_model(path)
