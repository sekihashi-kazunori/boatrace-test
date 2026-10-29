"""
全会場ルールの毎朝の答え合わせ(紙上・2026-10-13〜)。

前日の全レース(全国約150〜180R)の出走表・天候・結果を openapi から取り、
「会場 × 1号艇の級 × レース番号帯」ごとに、
  ・1-456-456(600円)
  ・江戸川式(1-2-3/1-3-2 各200円 + 1-456-456 各100円 = 1,000円)
を買っていたらどうなったかを計算し、rule_eval.csv に追記する。
本番で買うレースが少なくても、候補ルールの検証は毎日全国分進む。

    python -m src.tools.rule_check --date 2026-10-12
"""
import argparse
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from src.tools.fetch_openapi import rows_for_day
from src.notify.notifier import notify_console, notify_discord

NAMES = {1: "桐生", 2: "戸田", 3: "江戸川", 4: "平和島", 5: "多摩川", 6: "浜名湖", 7: "蒲郡", 8: "常滑", 9: "津",
         10: "三国", 11: "びわこ", 12: "住之江", 13: "尼崎", 14: "鳴門", 15: "丸亀", 16: "児島", 17: "宮島",
         18: "徳山", 19: "下関", 20: "若松", 21: "芦屋", 22: "福岡", 23: "唐津", 24: "大村"}
ONE456 = {f"1-{a}-{b}" for a in (4, 5, 6) for b in (4, 5, 6) if a != b}
PATH = "rule_eval.csv"
START = "2026-10-13"


def evaluate(d: date) -> pd.DataFrame:
    rows = rows_for_day(d)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    b1 = df[df.racer_boat_number == 1]
    out = []
    for _, r in b1.iterrows():
        fo, pay = r.get("trifecta_combination"), r.get("trifecta_payout")
        if not fo or pd.isna(pay):
            continue
        cls = "B" if (r.get("racer_class_number") or 0) >= 3 else "A"
        rn = int(r.race_number)
        grp = "1-4R" if rn <= 4 else ("5-8R" if rn <= 8 else "9-12R")
        hit456 = fo in ONE456
        ret456 = pay if hit456 else 0
        retE = (pay * 2 if fo in ("1-2-3", "1-3-2") else 0) + ret456
        out.append({"race_date": str(d), "stadium": NAMES.get(int(r.stadium_code), r.stadium_code),
                    "race_number": rn, "group": grp, "b1_class": cls,
                    "wind": r.get("race_wind"), "wave": r.get("race_wave"), "result": fo, "payout": pay,
                    "hit456": int(hit456), "ret456": ret456, "hitE": int(fo in ONE456 or fo in ("1-2-3", "1-3-2")),
                    "retE": retE})
    return pd.DataFrame(out)


def summary(all_df: pd.DataFrame) -> str:
    """2026-10-14 ご主人様指示で分かりやすく作り直し:
    何レース買って何レース当たったか、当たったのはどのレースか、を先に出す。"""
    x = all_df[(all_df.race_date >= START) & (all_df.b1_class == "B")].copy()
    if x.empty:
        return ""
    day = x.race_date.max()
    y = x[x.race_date == day]
    md = f"{int(day[5:7])}/{int(day[8:10])}"

    def total(df):
        n, h = len(df), int(df.hit456.sum())
        stake, pay = 600 * n, int(df.ret456.sum())
        rate = pay / stake * 100 if stake else 0
        return n, h, stake, pay, rate

    n, h, stake, pay, rate = total(y)
    lines = [
        f"【全国答え合わせ {md}】※紙上",
        "ルール: 1号艇がB級のレースだけ 1-456-456(6点×100円=600円)を買ったら?",
        "",
        f"▶ {md} の結果",
        f"{n}Rに賭けて {h}R的中🎯(的中率{h / n * 100:.0f}%)" if n else "対象レースなし",
        f"購入{stake:,}円 → 払戻{pay:,}円 / 回収率{rate:.0f}% ({pay - stake:+,}円)",
    ]
    hits = y[y.hit456 == 1].sort_values("payout", ascending=False)
    if len(hits):
        lines.append("")
        lines.append("🎯 的中したレース")
        for _, r in hits.iterrows():
            lines.append(f"・{r.stadium}{int(r.race_number)}R {r.result} → {int(r.payout):,}円")

    n, h, stake, pay, rate = total(x)
    lines += ["", f"▶ 累積({START[5:7].lstrip('0')}/{START[8:10].lstrip('0')}〜)",
              f"{n}Rに賭けて {h}R的中🎯(的中率{h / n * 100:.0f}%)",
              f"購入{stake:,}円 → 払戻{pay:,}円 / 回収率{rate:.0f}% ({pay - stake:+,}円)"]

    g = x.groupby("stadium").agg(R=("ret456", "size"), hit=("hit456", "sum"), ret=("ret456", "sum"))
    g["rate"] = g.ret / (600 * g.R) * 100
    g = g[g.hit > 0].sort_values(["rate", "R"], ascending=False)
    if len(g):
        lines += ["", "🏆 当たっている会場(累積・回収率順)"]
        for st, row in g.head(8).iterrows():
            lines.append(f"・{st}: {int(row.R)}R中 {int(row.hit)}R的中 / 回収率{row.rate:.0f}%")
    lines += ["", "※1日数Rなので、1回当たれば100%超え・外れれば0%。何週間か貯まってから判断します"]
    return "\n".join(lines)


import itertools
# 2026-10-30 ご主人様指示: 全レースで 1-456-456・123BOX・456BOX を買っていたら、を毎朝出す
PATTERNS = {
    "1-456-456": ONE456,
    "123BOX": {"-".join(map(str, p)) for p in itertools.permutations((1, 2, 3))},
    "456BOX": {"-".join(map(str, p)) for p in itertools.permutations((4, 5, 6))},
}
LONGRUN = {"1-456-456": 66, "123BOX": 72, "456BOX": 52}  # 2024/6〜2026/10 の13.4万R・全レース


def patterns_summary(all_df: pd.DataFrame) -> str:
    x = all_df[all_df.race_date >= START].copy()
    if x.empty:
        return ""
    day = x.race_date.max()
    md = f"{int(day[5:7])}/{int(day[8:10])}"
    lines = [f"【全レース 3つの買い方 {md}】※紙上・各6点×100円=600円を全レース"]

    def one(df, S):
        h = df[df.result.isin(S)]
        st, py = 600 * len(df), int(h.payout.sum())
        return len(df), len(h), st, py, (py / st * 100 if st else 0), h

    for name, S in PATTERNS.items():
        n, h, st, py, rate, hits = one(x[x.race_date == day], S)
        cn, ch, cst, cpy, crate, _ = one(x, S)
        icon = "✅" if rate >= 100 else "🔻"
        lines += ["", f"{icon} **{name}**",
                  f"　本日: {n}R 的中{h} │ {st:,}→{py:,}円 │ {py - st:+,}円 ({rate:.0f}%)",
                  f"　累積: {cn}R 的中{ch} │ {cst:,}→{cpy:,}円 │ {cpy - cst:+,}円 ({crate:.0f}%)　※{START[5:7].lstrip('0')}/{START[8:10].lstrip('0')}〜",
                  f"　(参考)2年半の回収率 {LONGRUN[name]}%"]
        top = hits.sort_values("payout", ascending=False).head(5)
        if len(top):
            lines.append("　🎯 " + " / ".join(f"{r.stadium}{int(r.race_number)}R {r.result} {int(r.payout):,}円" for r in top.itertuples()))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    new = evaluate(d)
    print(f"{d}: {len(new)}R")
    old = pd.read_csv(PATH) if os.path.exists(PATH) else pd.DataFrame()
    if not old.empty:
        old = old[old.race_date != str(d)]
    all_df = pd.concat([old, new], ignore_index=True)
    all_df.to_csv(PATH, index=False)
    # 2026-10-30: 前日の精算(モーニング・デイ・ナイター)がちゃんと済んだかを毎朝自己点検して報告
    chk = []
    for k, ja in (("morning", "モーニング"), ("day", "デイ"), ("nighter", "ナイター＋最終振り返り")):
        ok = os.path.exists(os.path.join("plans", f"{d}_settled_{k}"))
        chk.append(f"{'✅' if ok else '❌'}{ja}")
    health = f"【執事の自己点検 {d.month}/{d.day}】精算と報告: " + " / ".join(chk)
    if "❌" in health:
        health += "\n⚠️ 抜けがあります。執事に「昨日の精算をやり直して」と伝えてください"
    msgs = [health, summary(all_df), patterns_summary(all_df)]
    for msg in msgs:
        if not msg:
            continue
        notify_console(msg)
        try:
            notify_discord(msg)
        except Exception as e:
            print(f"通知失敗: {e}")


if __name__ == "__main__":
    main()
