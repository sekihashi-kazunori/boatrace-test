"""
v2モデルの学習(GitHub Actions の「いつものやつ」で実行)。

1着・2着・3着のそれぞれになる確率を予想する3つのモデルを学習し、
model_v2/ に保存する。2・3着専用のモデルを持つことで、
「1着は無理でも2・3着には残る艇」を読めるようにする。

検証結果(最後の10%の期間)は model_v2/metrics.json に保存する。

    python -m src.models.train_v2
"""
from __future__ import annotations

import itertools
import json
import os
import sqlite3

import lightgbm as lgb
import numpy as np
import pandas as pd

from src.features.v2 import FEATURES_V2, KEY, add_race_features, add_cumulative_priors, save_prior_tables

OUT = "model_v2"
TARGETS = {"win": 1, "second": 2, "third": 3}
PERM = np.array(list(itertools.permutations(range(6), 3)))


def load(db_path: str = "boatrace.db") -> pd.DataFrame:
    c = sqlite3.connect(db_path)
    e = pd.read_sql("""select race_date, stadium_code, race_number, lane_number, racer_id, national_win_rate,
        local_win_rate, motor_2rate, boat_2rate, exhibition_time, tilt, finish_position from race_entries""", c)
    r = pd.read_sql("select race_date, stadium_code, race_number, finish_order, trifecta_payout from race_results", c)
    for d in (e, r):
        d["stadium_code"] = d["stadium_code"].astype(str).str.zfill(2)
        d["race_date"] = d["race_date"].astype(str)
    r = r.drop_duplicates(KEY)
    g = e.groupby(KEY)
    ok = g["lane_number"].transform("size").eq(6) & g["finish_position"].transform("count").ge(3)
    e = e[ok].merge(r, on=KEY, how="inner").sort_values(KEY + ["lane_number"]).reset_index(drop=True)
    return e


def harville3(p1, p2, p3):
    i, j, k = PERM[:, 0], PERM[:, 1], PERM[:, 2]
    P = p1[:, i] * p2[:, j] / np.clip(1 - p2[:, i], 1e-9, None) * p3[:, k] / np.clip(1 - p3[:, i] - p3[:, j], 1e-9, None)
    return P / P.sum(1, keepdims=True)


def main():
    e = load()
    e = add_race_features(e)
    e = add_cumulative_priors(e)
    # 欠場・失格などで着順の無い艇も「2・3着に入らなかった艇」として残す(1レース6艇を保つ)
    dates = np.sort(e["race_date"].unique())
    split = dates[int(len(dates) * 0.9)]
    tr, va = e[e.race_date < split], e[e.race_date >= split]
    print(f"学習 {len(tr)}艇 / 検証 {len(va)}艇 (検証開始 {split})")

    params = {"objective": "binary", "metric": "binary_logloss", "learning_rate": 0.05,
              "num_leaves": 31, "min_data_in_leaf": 50, "verbose": -1}
    os.makedirs(OUT, exist_ok=True)
    probs = {}
    best = {}
    for name, pos in TARGETS.items():
        ytr = (tr.finish_position == pos).astype(int)
        yva = (va.finish_position == pos).astype(int)
        m = lgb.train(params, lgb.Dataset(tr[FEATURES_V2], ytr), num_boost_round=1000,
                      valid_sets=[lgb.Dataset(va[FEATURES_V2], yva)],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(100)])
        best[name] = m.best_iteration
        p = m.predict(va[FEATURES_V2], num_iteration=m.best_iteration).reshape(-1, 6)
        probs[name] = p / p.sum(1, keepdims=True)

    # 検証: 3連単の上位5点に正解が入った割合
    P = harville3(probs["win"], probs["second"], probs["third"])
    combo = ["-".join(str(x + 1) for x in p) for p in PERM]
    idx = {s: n for n, s in enumerate(combo)}
    fo = va.groupby(KEY, sort=False)["finish_order"].first().map(idx)
    pay = va.groupby(KEY, sort=False)["trifecta_payout"].first()
    ok = fo.notna().values
    wi = fo.values[ok].astype(int)
    Pv = P[ok]
    rank = np.argmax(np.argsort(-Pv, 1) == wi[:, None], 1)
    metrics = {
        "validation_from": str(split),
        "races": int(ok.sum()),
        "trifecta_logloss": float(-np.log(Pv[np.arange(len(wi)), wi]).mean()),
        "top5_hit": float((rank < 5).mean()),
        "top5_equal_recovery": float((pay.values[ok] * (rank < 5)).sum() / (500 * len(wi))),
        "best_iteration": best,
    }
    print(metrics)

    # 本番用は全期間で学び直す(検証で決めた回数で)
    for name, pos in TARGETS.items():
        y = (e.finish_position == pos).astype(int)
        m = lgb.train(params, lgb.Dataset(e[FEATURES_V2], y), num_boost_round=max(50, best[name]))
        m.save_model(os.path.join(OUT, f"{name}.txt"))

    raw = load()
    save_prior_tables(raw, OUT)
    with open(os.path.join(OUT, "metrics.json"), "w") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=1)
    print("保存しました:", OUT)


if __name__ == "__main__":
    main()
