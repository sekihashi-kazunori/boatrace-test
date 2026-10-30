"""1号艇の単勝・複勝 検証枠の採点(毎朝・紙上・2026-10-30〜)。

前日に final_pipeline が記録した締切前オッズ(plans/tanfuku/日付.csv)と、openapi の払戻で採点する。
  A: 展示1コースの1号艇・単勝3.0倍以上 → 単勝100円
  B: 展示1コースの1号艇・複勝(下限)1.4倍以上 → 複勝100円
  (参考) 展示1コースの1号艇すべての単勝・複勝
累積は tanfuku_eval.csv。

    python -m src.tools.tanfuku_check --date 2026-10-30
"""
from __future__ import annotations

import argparse
import csv
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EVAL = "tanfuku_eval.csv"
START = "2026-10-30"
SLOTS = [("A", "1号艇 単勝3.0倍以上の単勝", "win", lambda r: r["win_1"] >= 3.0),
         ("B", "1号艇 複勝1.4倍以上の複勝", "place", lambda r: r["place_1"] >= 1.4),
         ("all_win", "(参考)1号艇の単勝 全部", "win", lambda r: True),
         ("all_place", "(参考)1号艇の複勝 全部", "place", lambda r: True)]
FIELDS = ["race_date", "slot", "stadium_code", "race_number", "odds", "payout"]


def _payouts(d: date) -> dict:
    from src.tools.fetch_openapi import get, as_list
    res = get("results", d) or {}
    out = {}
    for x in as_list(res.get("results", [])):
        p = x.get("payouts") or {}
        k = (f"{int(x['race_stadium_number']):02d}", int(x["race_number"]))
        out[k] = {kind: sum(int(y.get("payout") or 0) for y in p.get(kind, []) if str(y.get("combination")) == "1")
                  for kind in ("win", "place")}
        out[k]["has"] = bool(p.get("win"))
    return out


def evaluate(d: date) -> list[dict]:
    path = os.path.join("plans", "tanfuku", f"{d}.csv")
    if not os.path.exists(path):
        return []
    pay = _payouts(d)
    seen, out = set(), []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r["stadium_code"].zfill(2), int(r["race_number"]))
            if k in seen or r.get("course1") not in ("1", "1.0"):
                continue
            seen.add(k)
            if k not in pay or not pay[k]["has"]:
                continue  # 中止など
            row = {"win_1": float(r["win_1"]), "place_1": float(r["place_1"])}
            for slot, _t, kind, cond in SLOTS:
                if cond(row):
                    out.append({"race_date": str(d), "slot": slot, "stadium_code": k[0], "race_number": k[1],
                                "odds": row["win_1" if kind == "win" else "place_1"], "payout": pay[k][kind]})
    return out


def summary(rows: list[dict], d: date) -> str:
    md = f"{d.month}/{d.day}"
    lines = [f"【1号艇の単勝・複勝 検証枠 {md}】※紙上・1点100円・展示で1コースの1号艇だけ"]
    for slot, title, _k, _c in SLOTS:
        t = [r for r in rows if r["slot"] == slot and r["race_date"] == str(d)]
        a = [r for r in rows if r["slot"] == slot]

        def one(x):
            n, h, py = len(x), sum(1 for r in x if int(r["payout"]) > 0), sum(int(r["payout"]) for r in x)
            return f"{n}R 的中{h} │ {100 * n:,}→{py:,}円 │ {py - 100 * n:+,}円 ({py / (100 * n) * 100:.0f}%)" if n else "対象なし"
        lines += [f"**{title}**", f"　本日: {one(t)}", f"　累積: {one(a)}　※{START[5:7].lstrip('0')}/{START[8:]}〜"]
    lines.append("※確定オッズの過去7か月では A 116%・B 113%(毎月100%超え)。締切前オッズでも出るかを確かめ中")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    new = evaluate(d)
    old = []
    if os.path.exists(EVAL):
        with open(EVAL, encoding="utf-8") as f:
            old = [r for r in csv.DictReader(f) if r["race_date"] != str(d)]
    rows = old + new
    if not rows:
        print("記録なし")
        return
    with open(EVAL, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    msg = summary(rows, d)
    print(msg)
    try:
        from src.notify.notifier import notify_discord
        notify_discord(msg)
    except Exception as e:
        print(f"通知失敗: {e}")


if __name__ == "__main__":
    main()
