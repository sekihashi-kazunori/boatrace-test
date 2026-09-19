"""
LightGBMモデルの学習スクリプト。

src.storage.db に蓄積されたレースデータ(過去データ収集で貯めたもの)から
特徴量を作り、「1着になったかどうか」を予測する二値分類モデルを学習して
model.txt として保存する。

既存の src/models/predict.py は変更していない。predict.py は
model.predict(...) の出力をそのまま exp() してレース内で正規化する作りなので、
ここでは objective="binary" で学習し、そのまま整合するようにしている。

使い方:
    python -m src.models.train --db-path boatrace.db --output model.txt
"""
import argparse

import lightgbm as lgb
import pandas as pd

from src.features.build_features import build_feature_dataframe, FEATURE_COLUMNS
from src.storage.db import get_engine


def load_training_data(engine) -> pd.DataFrame:
    df = build_feature_dataframe(engine, race_date=None)
    if df.empty:
        raise SystemExit("学習データが1件もありません。先にデータ収集を実行してください。")

    # 結果未確定(target_winがNaN)のレースは学習に使えないので除外
    df = df[df["target_win"].notna()].copy()
    df["target_win"] = df["target_win"].astype(int)
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=str, default="boatrace.db")
    parser.add_argument("--output", type=str, default="model.txt")
    parser.add_argument("--test-size", type=float, default=0.2, help="検証用に取り分ける割合")
    args = parser.parse_args()

    engine = get_engine(args.db_path)
    df = load_training_data(engine)

    print(f"学習に使えるデータ: {len(df)}件 (1着になった割合の平均 {df['target_win'].mean():.3%})")

    # 日付順に分割する(ランダム分割だと未来のレースの情報が学習に混ざってしまうため、
    # 古い方を学習用、新しい方を検証用にする)
    df_sorted = df.sort_values("race_date")
    split_idx = int(len(df_sorted) * (1 - args.test_size))
    train_df = df_sorted.iloc[:split_idx]
    valid_df = df_sorted.iloc[split_idx:]

    print(f"学習用: {len(train_df)}件 / 検証用: {len(valid_df)}件")

    train_set = lgb.Dataset(train_df[FEATURE_COLUMNS], label=train_df["target_win"])
    valid_set = lgb.Dataset(valid_df[FEATURE_COLUMNS], label=valid_df["target_win"], reference=train_set)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "verbose": -1,
    }

    model = lgb.train(
        params,
        train_set,
        num_boost_round=500,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(stopping_rounds=30), lgb.log_evaluation(50)],
    )

    model.save_model(args.output)
    print(f"モデルを保存しました: {args.output} (best_iteration={model.best_iteration})")

    # --- 簡易バックテスト: 検証データで「モデルが最有力とした艇」が実際に1着だった割合 ---
    valid_df = valid_df.copy()
    valid_df["pred"] = model.predict(valid_df[FEATURE_COLUMNS], num_iteration=model.best_iteration)
    top_pick = (
        valid_df.sort_values("pred", ascending=False)
        .groupby(["race_date", "stadium_code", "race_number"])
        .head(1)
    )
    hit_rate = top_pick["target_win"].mean()
    print(f"検証データでの単勝的中率(モデル1番評価艇が実際に1着だった割合): {hit_rate:.3%} ({len(top_pick)}レース)")


if __name__ == "__main__":
    main()
