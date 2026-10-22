"""v3実験(2026-10-22 ご主人様OK・「執事版MIZUYOMI」づくり)。

MIZUYOMIさんが見ている「今節の調子」と「モーターのコース別実績」を特徴量に足すと、
執事のAIの予想が良くなるかを確かめる。データは openapi_data(1〜10月・全国約4.4万R)だけを使う。

  ・学習: 1〜7月 / 検証: 8月〜(未来のデータは使わない)
  ・比較: base(今まで相当の特徴量) と new(base + 今節の調子 + モーターのコース別実績)
  ・評価: 3連単の確率上位6点を100円ずつ(=MIZUYOMIさんと同じ6点買い)の的中率・回収率
          自信のあるレース(上位6点の確率合計が高い)だけに絞った場合も出す

結果は experiments/v3_metrics.json と experiments/v3_val.csv.gz に保存(GitHub Actionsで実行)。
    python -m src.experiments.v3
"""
from __future__ import annotations

import glob
import itertools
import json
import os

import numpy as np
import pandas as pd

KEY = ["race_date", "stadium_code", "race_number"]
PERM = np.array(list(itertools.permutations(range(6), 3)))
COMBO = ["-".join(str(x + 1) for x in p) for p in PERM]
OUT = "experiments"
K = 5.0  # 少ない件数を全体平均に寄せる強さ

BASE = [
    "lane", "cls", "age", "weight", "f_cnt", "l_cnt", "avg_st",
    "nat1", "nat2", "nat3", "loc1", "loc2", "motor2", "motor3", "boat2", "boat3",
    "ex_time", "tilt", "course", "wind", "wave", "grade",
    "nat1_rank", "nat1_diff", "motor2_rank", "motor2_diff", "ex_rank", "ex_diff", "loc1_rank",
]
NEW = [
    "ser_n", "ser_avg", "ser_win", "ser_top2",          # 今節の調子(この節でここまでの成績)
    "mc_n", "mc_win", "mc_top2", "m_n", "m_win",        # モーターのコース(枠)別実績・モーター全体
]


def load() -> pd.DataFrame:
    cols = ["race_date", "stadium_code", "race_number", "race_grade_number", "racer_boat_number", "racer_number",
            "racer_class_number", "racer_age", "racer_weight", "racer_flying_count", "racer_late_count",
            "racer_average_start_timing", "racer_national_top_1_percent", "racer_national_top_2_percent",
            "racer_national_top_3_percent", "racer_local_top_1_percent", "racer_local_top_2_percent",
            "racer_assigned_motor_number", "racer_assigned_motor_top_2_percent", "racer_assigned_motor_top_3_percent",
            "racer_assigned_boat_top_2_percent", "racer_assigned_boat_top_3_percent", "race_wind", "race_wave",
            "pv_racer_course_number", "pv_racer_exhibition_time", "pv_racer_tilt_adjustment", "finish_place",
            "trifecta_combination", "trifecta_payout"]
    df = pd.concat([pd.read_csv(f, usecols=cols) for f in sorted(glob.glob("openapi_data/2026-*.csv.gz"))])
    df = df.rename(columns={
        "racer_boat_number": "lane", "racer_number": "racer", "racer_class_number": "cls", "racer_age": "age",
        "racer_weight": "weight", "racer_flying_count": "f_cnt", "racer_late_count": "l_cnt",
        "racer_average_start_timing": "avg_st", "racer_national_top_1_percent": "nat1",
        "racer_national_top_2_percent": "nat2", "racer_national_top_3_percent": "nat3",
        "racer_local_top_1_percent": "loc1", "racer_local_top_2_percent": "loc2",
        "racer_assigned_motor_number": "motor", "racer_assigned_motor_top_2_percent": "motor2",
        "racer_assigned_motor_top_3_percent": "motor3", "racer_assigned_boat_top_2_percent": "boat2",
        "racer_assigned_boat_top_3_percent": "boat3", "race_wind": "wind", "race_wave": "wave",
        "race_grade_number": "grade", "pv_racer_course_number": "course", "pv_racer_exhibition_time": "ex_time",
        "pv_racer_tilt_adjustment": "tilt", "finish_place": "fin"})
    df = df.drop_duplicates(KEY + ["lane"])
    g = df.groupby(KEY)
    df = df[g["lane"].transform("size").eq(6)]
    df["ex_time"] = df["ex_time"].where(df["ex_time"] > 0)
    return df.sort_values(KEY + ["lane"]).reset_index(drop=True)


def add_base(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(KEY)
    for col, asc, name in (("nat1", False, "nat1"), ("motor2", False, "motor2"), ("ex_time", True, "ex"),
                           ("loc1", False, "loc1")):
        df[f"{name}_rank"] = g[col].rank(method="min", ascending=asc)
        if name != "loc1":
            df[f"{name}_diff"] = df[col] - g[col].transform("mean")
    return df


def add_series(df: pd.DataFrame) -> pd.DataFrame:
    """今節の調子: 同じ選手・同じ場で、日付が1日以内でつながっている開催を1つの節とみなし、
    このレースより前(前日まで+同じ日の前のレース)の成績を集計する。"""
    d = df[["racer", "stadium_code", "race_date", "race_number", "fin"]].copy()
    d["dt"] = pd.to_datetime(d["race_date"])
    d = d.sort_values(["racer", "stadium_code", "dt", "race_number"])
    gap = d.groupby(["racer", "stadium_code"])["dt"].diff().dt.days
    d["sid"] = (gap.isna() | (gap > 1)).cumsum()
    f = d["fin"].where(d["fin"] <= 6)
    d["_n"] = f.notna().astype(float)
    d["_pos"] = f.fillna(0)
    d["_w"] = (f == 1).astype(float)
    d["_t2"] = (f <= 2).astype(float)
    gg = d.groupby("sid")
    for c in ("_n", "_pos", "_w", "_t2"):
        d["c" + c] = gg[c].cumsum() - d[c]
    n = d["c_n"]
    d["ser_n"] = n
    d["ser_avg"] = np.where(n > 0, d["c_pos"] / n.clip(lower=1), np.nan)
    d["ser_win"] = (d["c_w"] + K * 1 / 6) / (n + K)
    d["ser_top2"] = (d["c_t2"] + K * 2 / 6) / (n + K)
    return df.join(d[["ser_n", "ser_avg", "ser_win", "ser_top2"]])


def add_motor(df: pd.DataFrame) -> pd.DataFrame:
    """モーターのコース別実績: 同じ場・同じモーター番号が、同じ枠で走った前日までの成績(直近180日)。
    モーター全体(全枠)の勝率も出す。"""
    d = df[["stadium_code", "motor", "lane", "race_date", "fin"]].copy()
    f = d["fin"].where(d["fin"] <= 6)
    d["_n"] = f.notna().astype(float)
    d["_w"] = (f == 1).astype(float)
    d["_t2"] = (f <= 2).astype(float)
    d["dt"] = pd.to_datetime(d["race_date"])
    out = {}
    lane_w = d.groupby("lane")["_w"].mean().to_dict()
    lane_t2 = d.groupby("lane")["_t2"].mean().to_dict()
    for name, by in (("mc", ["stadium_code", "motor", "lane"]), ("m", ["stadium_code", "motor"])):
        daily = d.groupby(by + ["dt"])[["_n", "_w", "_t2"]].sum().reset_index().sort_values(by + ["dt"])
        res = []
        for _, x in daily.groupby(by, sort=False):
            x = x.set_index("dt")
            r = x[["_n", "_w", "_t2"]].rolling("180D", closed="left").sum().fillna(0)
            r[by] = x[by].values
            res.append(r.reset_index())
        r = pd.concat(res)
        m = d[by + ["dt"]].merge(r, on=by + ["dt"], how="left")
        n = m["_n"].fillna(0).values
        out[f"{name}_n"] = n
        if name == "mc":
            bw = d["lane"].map(lane_w).values
            bt = d["lane"].map(lane_t2).values
            out["mc_win"] = (m["_w"].fillna(0).values + K * bw) / (n + K)
            out["mc_top2"] = (m["_t2"].fillna(0).values + K * bt) / (n + K)
        else:
            out["m_win"] = (m["_w"].fillna(0).values + K / 6) / (n + K)
    for k, v in out.items():
        df[k] = v
    return df


def harville3(p1, p2, p3):
    i, j, k = PERM[:, 0], PERM[:, 1], PERM[:, 2]
    P = p1[:, i] * p2[:, j] / np.clip(1 - p2[:, i], 1e-9, None) * p3[:, k] / np.clip(1 - p3[:, i] - p3[:, j], 1e-9, None)
    return P / P.sum(1, keepdims=True)


def fit_predict(tr, va, feats):
    import lightgbm as lgb
    params = {"objective": "binary", "metric": "binary_logloss", "learning_rate": 0.05,
              "num_leaves": 31, "min_data_in_leaf": 50, "verbose": -1, "seed": 1}
    probs, imp = {}, {}
    # 早期終了用に学習期間の最後1か月を内部検証に使う
    cut = np.sort(tr.race_date.unique())[-30]
    a, b = tr[tr.race_date < cut], tr[tr.race_date >= cut]
    for name, pos in (("win", 1), ("second", 2), ("third", 3)):
        m = lgb.train(params, lgb.Dataset(a[feats], (a.fin == pos).astype(int)), num_boost_round=1500,
                      valid_sets=[lgb.Dataset(b[feats], (b.fin == pos).astype(int))],
                      callbacks=[lgb.early_stopping(50, verbose=False)])
        m2 = lgb.train(params, lgb.Dataset(tr[feats], (tr.fin == pos).astype(int)),
                       num_boost_round=max(50, m.best_iteration))
        p = m2.predict(va[feats]).reshape(-1, 6)
        probs[name] = p / p.sum(1, keepdims=True)
        imp[name] = dict(sorted(zip(feats, m2.feature_importance("gain").round(0).tolist()),
                                key=lambda x: -x[1])[:15])
    return harville3(probs["win"], probs["second"], probs["third"]), probs["win"], imp


def evaluate(P, races: pd.DataFrame, tag: str) -> tuple[dict, pd.DataFrame]:
    idx = {s: n for n, s in enumerate(COMBO)}
    wi = races["trifecta_combination"].map(idx)
    ok = wi.notna().values & races["trifecta_payout"].notna().values
    P, r = P[ok], races[ok].reset_index(drop=True)
    wi = wi[ok].astype(int).values
    order = np.argsort(-P, 1)
    rank = np.argmax(order == wi[:, None], 1)
    cov6 = np.take_along_axis(P, order[:, :6], 1).sum(1)
    pay = r["trifecta_payout"].values
    hit6 = rank < 6
    month = r["race_date"].str[:7].values
    res = {"races": int(len(r)), "logloss": float(-np.log(P[np.arange(len(wi)), wi] + 1e-12).mean()),
           "top1_hit": float((rank < 1).mean()), "top6_hit": float(hit6.mean()),
           "top6_recovery": float((pay * hit6).sum() / (600 * len(r))),
           "by_month": {m: round(float((pay * hit6)[month == m].sum() / (600 * (month == m).sum())), 3)
                        for m in sorted(set(month))}}
    # 自信のあるレースだけ(上位6点の確率合計の上位x%)
    sel = {}
    for q in (0.5, 0.3, 0.2, 0.1):
        th = np.quantile(cov6, 1 - q)
        s = cov6 >= th
        sel[f"top{int(q * 100)}pct"] = {"races": int(s.sum()), "hit": round(float(hit6[s].mean()), 3),
                                         "recovery": round(float((pay * hit6)[s].sum() / (600 * s.sum())), 3),
                                         "by_month": {m: round(float((pay * hit6)[s & (month == m)].sum() /
                                                                     max(600 * (s & (month == m)).sum(), 1)), 3)
                                                      for m in sorted(set(month))}}
    res["confident"] = sel
    detail = r[KEY].copy()
    detail[f"{tag}_cov6"] = cov6.round(4)
    detail[f"{tag}_rank"] = rank
    detail[f"{tag}_top6"] = [",".join(COMBO[j] for j in o[:6]) for o in order]
    detail["result"] = r["trifecta_combination"].values
    detail["payout"] = pay
    return res, detail


def odds_eval(P, races: pd.DataFrame) -> dict:
    """8/1〜9/18の確定オッズ(odds_hist)がある分で、AIの確率と世間の確率(オッズ)を比べて買う方式を試す。
    ※確定オッズは締切前より穴が1〜2割低いので、実戦は少し悪くなる前提で見ること。"""
    frames = []
    for f in sorted(glob.glob("odds_hist/*.csv")):
        o = pd.read_csv(f)
        frames.append(o.assign(race_date=os.path.basename(f)[:10]))
    if not frames:
        return {}
    O = pd.concat(frames)
    O["stadium_code"] = O["stadium_code"].astype(int)
    r = races.reset_index(drop=True).copy()
    r["_i"] = np.arange(len(r))
    m = r.merge(O, on=["race_date", "stadium_code", "race_number"], how="inner")
    if m.empty:
        return {}
    odds = m[COMBO].values.astype(float)
    odds[~(odds > 0)] = np.nan
    q = (1 / odds) / np.nansum(1 / odds, 1, keepdims=True)
    Pm = P[m["_i"].values]
    pay = np.nan_to_num(m["trifecta_payout"].values.astype(float))
    win = m["trifecta_combination"].map({s: n for n, s in enumerate(COMBO)}).values
    half = np.where(m["race_date"].values < "2026-08-26", "前半", "後半")
    out = {"races": int(len(m))}
    ratio = Pm / q
    for rmin in (1.2, 1.5, 1.7, 2.0, 2.5):
        for omax in (20, 30, 50, 100):
            buy = (ratio >= rmin) & (odds <= omax)
            n = buy.sum()
            if n == 0:
                continue
            hit = buy[np.arange(len(m)), np.nan_to_num(win, nan=0).astype(int)] & ~np.isnan(win)
            ret = (pay * hit).sum()
            res = {"bets": int(n), "races_bought": int(buy.any(1).sum()), "hits": int(hit.sum()),
                   "recovery": round(float(ret / (100 * n)), 3)}
            for h in ("前半", "後半"):
                s = half == h
                nn = buy[s].sum()
                res[h] = round(float((pay * hit)[s].sum() / max(100 * nn, 1)), 3)
            out[f"ratio{rmin}_odds{omax}"] = res
    # 上位6点のうち、世間より高く見ている点だけ買う
    order = np.argsort(-Pm, 1)[:, :6]
    sel = np.zeros_like(Pm, dtype=bool)
    np.put_along_axis(sel, order, True, 1)
    for rmin in (1.0, 1.2, 1.5):
        buy = sel & (ratio >= rmin)
        n = buy.sum()
        hit = buy[np.arange(len(m)), np.nan_to_num(win, nan=0).astype(int)] & ~np.isnan(win)
        out[f"top6_ratio{rmin}"] = {"bets": int(n), "hits": int(hit.sum()),
                                    "recovery": round(float((pay * hit).sum() / max(100 * n, 1)), 3)}
    return out


def main():
    df = load()
    df = add_base(df)
    df = add_series(df)
    df = add_motor(df)
    tr = df[df.race_date < "2026-08-01"]
    va = df[df.race_date >= "2026-08-01"]
    races = va.groupby(KEY, sort=False).first().reset_index()[KEY + ["trifecta_combination", "trifecta_payout"]]
    print(f"学習 {tr.groupby(KEY).ngroups}R / 検証 {len(races)}R")
    os.makedirs(OUT, exist_ok=True)
    metrics, details = {}, []
    for tag, feats in (("base", BASE), ("new", BASE + NEW)):
        P, pwin, imp = fit_predict(tr, va, feats)
        m, d = evaluate(P, races, tag)
        try:
            m["odds"] = odds_eval(P, races)
            print(tag, "odds", json.dumps(m["odds"], ensure_ascii=False)[:2000])
        except Exception as e:
            print("odds_eval失敗", e)
        m["importance_win"] = imp["win"]
        metrics[tag] = m
        details.append(d)
        print(tag, json.dumps({k: v for k, v in m.items() if k != "importance_win"}, ensure_ascii=False))
    # 新特徴量そのものの効き(1号艇の1着率)
    b1 = va[va.lane == 1]
    q = pd.qcut(b1["ser_avg"].rank(method="first"), 4, labels=["好調", "やや好調", "やや不調", "不調"])
    metrics["b1_win_by_series_form"] = b1.groupby(q, observed=True)["fin"].apply(lambda s: round(float((s == 1).mean()), 3)).to_dict()
    q2 = pd.qcut(b1["mc_win"].rank(method="first"), 4, labels=["低", "やや低", "やや高", "高"])
    metrics["b1_win_by_motor_course"] = b1.groupby(q2, observed=True)["fin"].apply(lambda s: round(float((s == 1).mean()), 3)).to_dict()
    with open(os.path.join(OUT, "v3_metrics.json"), "w") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=1)
    out = details[0].merge(details[1].drop(columns=["result", "payout"]), on=KEY)
    out.to_csv(os.path.join(OUT, "v3_val.csv.gz"), index=False)
    print("保存:", OUT)


if __name__ == "__main__":
    main()
