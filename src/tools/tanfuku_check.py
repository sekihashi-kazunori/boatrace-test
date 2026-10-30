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


def _fun_rows(d_from: str, d_to: str) -> dict:
    """楽しむ枠(3連単の本予想・当たりやすい123BOX・1-456-456)の日ごとの購入・払戻(state.db)。"""
    import sqlite3
    out = {}
    if not os.path.exists("state.db"):
        return out
    c = sqlite3.connect("state.db")
    for d, st, py in c.execute(
            "select race_date, sum(amount), sum(coalesce(payout,0)) from bet_tickets "
            "where bet_type in ('gen','v_123hi','v_456hi') and result is not null and race_date between ? and ? "
            "group by race_date", (d_from, d_to)):
        out[d] = (st or 0, py or 0)
    return out


def portfolio(rows: list[dict], d: date) -> str:
    """2026-10-31 ご主人様OK: 稼ぐ枠(単複)7割＋楽しむ枠(3連単・ボックス)3割で、1日1万円を配ったら。
    それぞれの回収率を、その日の使った割合で平均する。"""
    days = sorted({r["race_date"] for r in rows if r["slot"] in ("A", "B")})
    fun = _fun_rows(START, str(d))

    def day_rate(dd):
        x = [r for r in rows if r["race_date"] == dd and r["slot"] in ("A", "B")]
        st = 100 * len(x)
        t = sum(int(r["payout"]) for r in x) / st if st else None
        f = fun.get(dd)
        u = f[1] / f[0] if f and f[0] else None
        return t, u
    lines = ["", "【組み合わせ(1日1万円で配ったら)】稼ぐ枠=1号艇単複A+B / 楽しむ枠=3連単本予想+当たりやすい123BOX・1-456-456"]
    for wt, wf in ((10, 0), (7, 3), (5, 5)):
        tot_in = tot_out = 0
        today = None
        for dd in days:
            t, u = day_rate(dd)
            if t is None and u is None:
                continue
            t = t if t is not None else 1.0
            u = u if u is not None else 1.0
            out = 1000 * wt * t + 1000 * wf * u
            tot_in += 10000
            tot_out += out
            if dd == str(d):
                today = out
        if not tot_in:
            continue
        lab = f"単複{wt}割" + (f"＋楽しむ{wf}割" if wf else "だけ")
        lines.append(f"・{lab}: 本日 {('%+d円' % (today - 10000)) if today is not None else '-'}"
                     f" / 累積 {tot_out - tot_in:+,.0f}円 ({tot_out / tot_in * 100:.0f}%・{len(days)}日)")
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
    try:
        msg += portfolio(rows, d)
    except Exception as e:
        print(f"組み合わせ計算の失敗: {e}")
    print(msg)
    try:
        from src.notify.notifier import notify_discord
        notify_discord(msg)
    except Exception as e:
        print(f"通知失敗: {e}")


if __name__ == "__main__":
    main()
