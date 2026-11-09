"""場ごと・コースごとの直近1年の成績(2026-11-09 ご主人様「直近データが1番大切」)。

venue_daily.csv に 日付×場×コース の 出走数・1着・2着内・3着内 をためる。
執事の本予想はここから「その場のそのコースの直近365日の1着率・2連率・3連率」を材料にする。

    python -m src.tools.venue_stats build            # openapi_data から作り直す
    python -m src.tools.venue_stats update --date 2026-11-08   # 1日分を足す(毎朝)
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PATH = "venue_daily.csv"
F = ["race_date", "stadium_code", "course", "n", "win", "top2", "top3"]
WINDOW = 365


def build() -> None:
    import pandas as pd
    fs = sorted(glob.glob("openapi_data/*.csv.gz"))
    d = pd.concat([pd.read_csv(f, dtype={"stadium_code": str},
                               usecols=["race_date", "stadium_code", "result_course", "finish_place"]) for f in fs])
    d["fin"] = pd.to_numeric(d.finish_place, errors="coerce")
    d = d[d.result_course.between(1, 6)]
    d["stadium_code"] = d.stadium_code.str.zfill(2)
    g = d.groupby(["race_date", "stadium_code", d.result_course.astype(int).rename("course")]).agg(
        n=("fin", "size"), win=("fin", lambda s: int((s == 1).sum())), top2=("fin", lambda s: int((s <= 2).sum())),
        top3=("fin", lambda s: int((s <= 3).sum()))).reset_index()
    g.to_csv(PATH, index=False)
    print(len(g), "行", g.race_date.min(), g.race_date.max())


def update(d: date) -> None:
    from src.tools.fetch_openapi import get, as_list
    res = get("results", d) or {}
    cnt = {}
    for x in as_list(res.get("results", [])):
        for b in x.get("boats") or []:
            c, p = b.get("racer_course_number"), b.get("racer_place_number")
            if not c or not (1 <= int(c) <= 6):
                continue
            k = (f"{int(x['race_stadium_number']):02d}", int(c))
            v = cnt.setdefault(k, [0, 0, 0, 0])
            v[0] += 1
            if p:
                v[1] += int(p) == 1
                v[2] += int(p) <= 2
                v[3] += int(p) <= 3
    if not cnt:
        print("結果なし", d)
        return
    rows = []
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["race_date"] != str(d)]
    rows += [{"race_date": str(d), "stadium_code": s, "course": c, "n": v[0], "win": v[1], "top2": v[2], "top3": v[3]}
             for (s, c), v in sorted(cnt.items())]
    with open(PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=F)
        w.writeheader()
        w.writerows(rows)
    print("場のクセ 更新", d, len(cnt), "行")


def table(asof: date | str | None = None) -> dict:
    """{(場コード"02", コース1): (1着率, 2連率, 3連率)} 直近365日(asof の前日まで)。"""
    if not os.path.exists(PATH):
        return {}
    asof = str(asof or datetime.now(ZoneInfo("Asia/Tokyo")).date())
    lo = str(date.fromisoformat(asof) - timedelta(days=WINDOW))
    acc = {}
    with open(PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not (lo <= r["race_date"] < asof):
                continue
            k = (r["stadium_code"], int(r["course"]))
            v = acc.setdefault(k, [0, 0, 0, 0])
            for i, c in enumerate(("n", "win", "top2", "top3")):
                v[i] += int(r[c])
    return {k: (v[1] / v[0], v[2] / v[0], v[3] / v[0]) for k, v in acc.items() if v[0] >= 50}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["build", "update"])
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    if a.action == "build":
        build()
    else:
        d = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
        update(d)


if __name__ == "__main__":
    main()
