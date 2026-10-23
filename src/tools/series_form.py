"""今節の調子(2026-10-23 ご主人様OK・万舟検証枠B用)。

毎朝の全国答え合わせ(rule_check)のあとに、前日の全レースの「選手番号・場・着順」を
racer_hist.csv に追記する。本番では、同じ場で前日までつながっている開催(=今節)の
平均着順を選手ごとに出す。

    python -m src.tools.series_form --date 2026-10-22          # 1日分追記
    python -m src.tools.series_form --start 2026-10-01 --end 2026-10-22   # まとめて
"""
from __future__ import annotations

import argparse
import csv
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PATH = "racer_hist.csv"
FIELDS = ["race_date", "stadium_code", "race_number", "racer", "fin"]


def update(d: date) -> int:
    from src.tools.fetch_openapi import rows_for_day
    rows = rows_for_day(d)
    new = []
    for r in rows:
        fin = r.get("finish_place")
        try:
            fin = int(fin)
        except (TypeError, ValueError):
            continue
        if not 1 <= fin <= 6 or not r.get("racer_number"):
            continue
        new.append({"race_date": str(d), "stadium_code": str(r["stadium_code"]).zfill(2),
                    "race_number": int(r["race_number"]), "racer": str(int(r["racer_number"])), "fin": fin})
    old = []
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            old = [x for x in csv.DictReader(f) if x["race_date"] != str(d)]
    cutoff = str(d - timedelta(days=30))
    old = [x for x in old if x["race_date"] >= cutoff]
    with open(PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(old + new)
    print(f"{d}: {len(new)}件 追記")
    return len(new)


_CACHE: dict = {}


def series_avg(stadium_code: str, today: date) -> dict[str, tuple[int, float]]:
    """{選手番号: (今節の出走数, 平均着順)}。同じ場で、昨日から1日以内の間隔でつながる日を今節とみなす。"""
    key = (stadium_code, str(today))
    if key in _CACHE:
        return _CACHE[key]
    out: dict = {}
    if os.path.exists(PATH):
        by: dict = {}
        with open(PATH, encoding="utf-8") as f:
            for x in csv.DictReader(f):
                if x["stadium_code"] == stadium_code and x["race_date"] < str(today):
                    by.setdefault(x["racer"], []).append((x["race_date"], int(x["fin"])))
        for racer, xs in by.items():
            days = sorted({d for d, _ in xs}, reverse=True)
            prev = today
            keep = set()
            for d in days:
                dd = date.fromisoformat(d)
                if (prev - dd).days > 1:
                    break
                keep.add(d)
                prev = dd
            fins = [f for d, f in xs if d in keep]
            if fins:
                out[racer] = (len(fins), sum(fins) / len(fins))
    _CACHE[key] = out
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    a = ap.parse_args()
    if a.start:
        d, e = date.fromisoformat(a.start), date.fromisoformat(a.end)
        while d <= e:
            update(d)
            d += timedelta(days=1)
    else:
        d = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
        update(d)


if __name__ == "__main__":
    main()
