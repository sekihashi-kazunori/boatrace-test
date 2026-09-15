from __future__ import annotations

import pandas as pd
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from src.storage.db import RaceEntry

# LightGBM(train.py / predict.py)に渡す特徴量カラム一覧
FEATURE_COLUMNS = [
    "lane_number",
    "national_win_rate",
    "course_base_win_rate",
    "motor_2rate",
    "boat_2rate",
    "exhibition_time",
    "exhibition_rank",
    "tilt",
    "start_timing",
]


def build_feature_dataframe(engine: Engine, race_date=None) -> pd.DataFrame:
    """
    src.storage.db の RaceEntry テーブルから特徴量DataFrameを構築する。

    - race_date を指定すると、その日のレースだけに絞り込む(predict.py用)
    - race_date を省略すると、全期間のデータを返す(train.py用)
    """
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        q = session.query(RaceEntry)
        if race_date is not None:
            q = q.filter(RaceEntry.race_date == race_date)
        # RaceEntry モデルの定義に沿ってDataFrame化(テーブル名は問わない)
        df = pd.read_sql(q.statement, con=engine)
    finally:
        session.close()

    if df.empty:
        return df

    # --- 列名の整合(predict.py / train.py が期待する名前に合わせる) ---
    df = df.rename(columns={"local_win_rate": "course_base_win_rate"})

    # --- 展示タイム順位(1〜6位、速いほど1位) ---
    df["exhibition_rank"] = (
        df.groupby(["race_date", "stadium_code", "race_number"])["exhibition_time"]
        .rank(method="min", ascending=True)
    )

    # --- 学習用ターゲット(1着なら1、それ以外は0。未確定レースはNaNのまま) ---
    df["target_win"] = df["finish_position"].apply(
        lambda x: 1 if x == 1 else (0 if pd.notna(x) else pd.NA)
    )

    return df


