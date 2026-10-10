"""
特徴量 v2 (2026-10-11〜)

過去9か月(約4万レース)のバックテストで、v1(9項目・1着モデルのみ)より
3連単の予測精度が上がった特徴量セット。

- レース内の順位・平均との差(展示タイム、勝率、モーター)
- 選手の過去成績(1着率・3着内率、全体/枠番別)と、場×枠の過去成績
  ※ 学習時は「その日より前」のデータだけで計算(未来の情報を使わない)
  ※ 本番では全履歴から計算した表(model_v2/priors_*.csv)を使う
- スタート展示(ST)は本番で取得できていないため使わない(学習と本番のズレ防止)
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

KEY = ["race_date", "stadium_code", "race_number"]
SHRINK_K = 5.0

FEATURES_V2 = [
    "lane_number", "national_win_rate", "local_win_rate", "motor_2rate", "boat_2rate",
    "exhibition_time", "exhibition_time_rank", "tilt",
    "national_win_rate_rank", "local_win_rate_rank", "motor_2rate_rank",
    "exhibition_time_diff", "national_win_rate_diff", "local_win_rate_diff", "motor_2rate_diff",
    "rc_is1", "rc_is3", "rc_n", "rl_is1", "rl_is3", "rl_n", "sl_is1", "sl_is3",
]

PRIOR_GROUPS = {
    "rc": ["racer_id"],
    "rl": ["racer_id", "lane_number"],
    "sl": ["stadium_code", "lane_number"],
}


def add_race_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "course_base_win_rate" in df.columns and "local_win_rate" not in df.columns:
        df["local_win_rate"] = df["course_base_win_rate"]
    df["stadium_code"] = df["stadium_code"].astype(str).str.zfill(2)
    df["racer_id"] = df["racer_id"].astype(str)
    for col in ("national_win_rate", "local_win_rate", "motor_2rate", "boat_2rate", "exhibition_time", "tilt"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.loc[df["exhibition_time"] <= 0, "exhibition_time"] = np.nan
    g = df.groupby(KEY)
    for col, asc in (("exhibition_time", True), ("national_win_rate", False),
                     ("local_win_rate", False), ("motor_2rate", False)):
        df[f"{col}_rank"] = g[col].rank(method="min", ascending=asc)
        df[f"{col}_diff"] = df[col] - g[col].transform("mean")
    return df


def _base(df_hist: pd.DataFrame, name: str, col: str):
    if "lane_number" in PRIOR_GROUPS[name]:
        return {str(int(k)): float(v) for k, v in df_hist.groupby("lane_number")[col].mean().items()}
    return float(df_hist[col].mean())


def add_cumulative_priors(df: pd.DataFrame) -> pd.DataFrame:
    """学習用: 各行について「その日より前」の成績だけで事前成績を計算する。"""
    df = df.copy()
    fin = df["finish_position"].where(df["finish_position"] <= 6)
    df["_is1"] = (fin == 1).astype(float)
    df["_is3"] = (fin <= 3).astype(float)
    df["_has"] = fin.notna().astype(float)
    for name, by in PRIOR_GROUPS.items():
        daily = df.groupby(by + ["race_date"])[["_is1", "_is3", "_has"]].sum().reset_index()
        daily = daily.sort_values(by + ["race_date"])
        gg = daily.groupby(by)
        for c in ("_is1", "_is3", "_has"):
            daily["cum" + c] = gg[c].cumsum() - daily[c]
        m = df[by + ["race_date"]].merge(daily[by + ["race_date", "cum_is1", "cum_is3", "cum_has"]],
                                         on=by + ["race_date"], how="left")
        for c in ("is1", "is3"):
            base = _base(df, name, "_" + c)
            b = df["lane_number"].astype(int).astype(str).map(base).values if isinstance(base, dict) else base
            df[f"{name}_{c}"] = (m[f"cum_{c}"].values + SHRINK_K * b) / (m["cum_has"].values + SHRINK_K)
        df[f"{name}_n"] = m["cum_has"].values
    return df.drop(columns=["_is1", "_is3", "_has"])


def save_prior_tables(df: pd.DataFrame, out_dir: str) -> None:
    """本番用: 全履歴から事前成績の表を作って保存する。"""
    os.makedirs(out_dir, exist_ok=True)
    fin = df["finish_position"].where(df["finish_position"] <= 6)
    h = df.assign(_is1=(fin == 1).astype(float), _is3=(fin <= 3).astype(float), _has=fin.notna().astype(float))
    bases = {}
    for name, by in PRIOR_GROUPS.items():
        t = h.groupby(by)[["_is1", "_is3", "_has"]].sum().reset_index()
        t.to_csv(os.path.join(out_dir, f"priors_{name}.csv"), index=False)
        bases[name] = {c: _base(h, name, "_" + c) for c in ("is1", "is3")}
    with open(os.path.join(out_dir, "priors_base.json"), "w") as f:
        json.dump(bases, f)


def add_table_priors(df: pd.DataFrame, model_dir: str) -> pd.DataFrame:
    """本番用: 保存済みの表から事前成績を付ける。表に無い選手は全体平均寄りの値になる。"""
    df = df.copy()
    with open(os.path.join(model_dir, "priors_base.json")) as f:
        bases = json.load(f)
    for name, by in PRIOR_GROUPS.items():
        t = pd.read_csv(os.path.join(model_dir, f"priors_{name}.csv"), dtype={"racer_id": str, "stadium_code": str})
        if "stadium_code" in t.columns:
            t["stadium_code"] = t["stadium_code"].str.zfill(2)
        m = df[by].merge(t, on=by, how="left")
        n = m["_has"].fillna(0).values
        for c in ("is1", "is3"):
            base = bases[name][c]
            b = df["lane_number"].astype(int).astype(str).map(base).astype(float).values if isinstance(base, dict) else base
            df[f"{name}_{c}"] = (m["_" + c].fillna(0).values + SHRINK_K * b) / (n + SHRINK_K)
        df[f"{name}_n"] = n
    return df
