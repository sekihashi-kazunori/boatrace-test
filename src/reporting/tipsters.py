"""予想屋さんの予想を紙上で採点する(2026-10-22 ご主人様指示)。

予想は plans/tipsters/日付.csv に手で入れる(スクショから執事が書き起こし)。
  列: tipster, kind, stadium_code, race_number, combo, amount
  kind=3t  : 3連単の買い目(1点ずつ・amount円)
  kind=win1: 1号艇の1着予想(お金は使わず、当たったかだけ数える)
ナイター後の最終振り返りで、公式サイトの結果と照合して本日分・累積を出す。累積は plans/tipster_eval.csv。
"""
from __future__ import annotations

import csv
import os
from datetime import date

DIR = os.path.join("plans", "tipsters")
EVAL = os.path.join("plans", "tipster_eval.csv")
NAMES = {"01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川", "06": "浜名湖", "07": "蒲郡",
         "08": "常滑", "09": "津", "10": "三国", "11": "びわこ", "12": "住之江", "13": "尼崎", "14": "鳴門",
         "15": "丸亀", "16": "児島", "17": "宮島", "18": "徳山", "19": "下関", "20": "若松", "21": "芦屋",
         "22": "福岡", "23": "唐津", "24": "大村"}
FIELDS = ["race_date", "tipster", "kind", "stadium_code", "race_number", "stake", "payout", "hit", "result"]


def _results(d: date, keys) -> dict:
    from src.results.fetch_results import fetch_race_result
    out = {}
    for st, rn in sorted(keys):
        try:
            r = fetch_race_result(d, st, rn)
            out[(st, rn)] = ((r.finish_order or "").replace(" ", ""), int(r.trifecta_payout or 0))
        except Exception as e:
            print(f"結果取得失敗 {st} {rn}R: {e}")
    return out


def _woman(d: date) -> list[dict]:
    """2026-10-24〜: 競艇の女は記録ページから自動で読み取る(締切後に買い目・レース後に結果が載る)。"""
    try:
        from src.tools.kyoteiwoman import fetch, eval_rows
        return eval_rows(fetch(d))
    except Exception as e:
        print(f"競艇の女の読み取り失敗: {e}")
        return []


def evaluate(d: date) -> list[dict]:
    path = os.path.join(DIR, f"{d}.csv")
    if not os.path.exists(path):
        return _woman(d)
    with open(path, encoding="utf-8") as f:
        picks = list(csv.DictReader(f))
    for p in picks:
        p["stadium_code"] = p["stadium_code"].zfill(2)
        p["race_number"] = int(p["race_number"])
    res = _results(d, {(p["stadium_code"], p["race_number"]) for p in picks})
    races: dict = {}
    for p in picks:
        races.setdefault((p["tipster"], p["kind"], p["stadium_code"], p["race_number"]), []).append(p)
    out = []
    for (tip, kind, st, rn), ps in races.items():
        if (st, rn) not in res or not res[(st, rn)][0]:
            continue  # 中止・結果なし
        fo, pay = res[(st, rn)]
        if kind in ("win1", "win"):
            # 1着予想(win1は1号艇、winはcombo欄の艇番)
            boat = "1" if kind == "win1" else ps[0]["combo"]
            hit = fo.split("-")[0] == boat
            stake = payout = 0
        else:
            stake = sum(int(p["amount"]) for p in ps)
            hits = [p for p in ps if p["combo"] == fo]
            payout = sum(int(p["amount"]) * pay // 100 for p in hits)
            hit = bool(hits)
        out.append({"race_date": str(d), "tipster": tip, "kind": kind, "stadium_code": st, "race_number": rn,
                    "stake": stake, "payout": payout, "hit": int(hit), "result": fo})
    return out + _woman(d)


def _save(d: date, new: list[dict]) -> list[dict]:
    old = []
    if os.path.exists(EVAL):
        with open(EVAL, encoding="utf-8") as f:
            old = [r for r in csv.DictReader(f) if r["race_date"] != str(d)]
        for r in old:
            for k in ("race_number", "stake", "payout", "hit"):
                r[k] = int(r[k])
    rows = old + new
    os.makedirs(os.path.dirname(EVAL), exist_ok=True)
    with open(EVAL, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


def _yen(v: int) -> str:
    return f"{v:+,}" if v else "±0"


def tipster_summary(d: date) -> str | None:
    new = evaluate(d)
    if not new:
        return None
    rows = _save(d, new)
    md = f"{d.month}/{d.day}"
    lines = [f"**🕵️ 予想屋さん 答え合わせ {md}** ※紙上・1点100円(競艇の女は1R1,000円を配分どおり)"]
    for tip in dict.fromkeys(r["tipster"] for r in new):
        today = [r for r in new if r["tipster"] == tip]
        cum = [r for r in rows if r["tipster"] == tip]
        since = min(r["race_date"] for r in cum)
        if today[0]["kind"] in ("win1", "win"):
            lines.append(f"**{tip}**(1着予想)")
            lines.append(f"　本日: {len(today)}R中 {sum(r['hit'] for r in today)}R的中 / "
                         f"累積: {len(cum)}R中 {sum(r['hit'] for r in cum)}R的中")
            lines.append("　" + " ".join(f"{NAMES.get(r['stadium_code'], r['stadium_code'])}{r['race_number']}R"
                                        f"{'🎯' if r['hit'] else '✖'}" for r in today))
            continue
        ts, tp = sum(r["stake"] for r in today), sum(r["payout"] for r in today)
        cs, cp = sum(r["stake"] for r in cum), sum(r["payout"] for r in cum)
        th, ch = sum(r["hit"] for r in today), sum(r["hit"] for r in cum)
        lines.append(f"**{tip}**")
        lines.append(f"　本日: {len(today)}R 的中{th} │ {ts:,}→{tp:,}円 │ {_yen(tp - ts)}円"
                     + (f" ({tp / ts * 100:.0f}%)" if ts else ""))
        lines.append(f"　累積: {len(cum)}R 的中{ch} │ {cs:,}→{cp:,}円 │ {_yen(cp - cs)}円"
                     + (f" ({cp / cs * 100:.0f}%)" if cs else "") + f"　※{int(since[5:7])}/{int(since[8:10])}〜")
        rows_txt = []
        for r in sorted(today, key=lambda r: (r["stadium_code"], r["race_number"])):
            name = f"{NAMES.get(r['stadium_code'], r['stadium_code'])}{r['race_number']}R"
            rows_txt.append(f"{name} {r['result'].replace('-', '')} " + (f"🎯{r['payout']:,}円" if r["hit"] else "✖"))
        lines.append("```\n" + "\n".join(rows_txt) + "\n```")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="空なら昨日(朝の答え合わせで、夜に結果待ちだった分を埋め直す)")
    a = ap.parse_args()
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    d = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    print(tipster_summary(d))
