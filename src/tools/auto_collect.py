"""オートレースの結果を集める(2026-11-06 ご主人様OK「オートレースもやってみて」・研究用・私的利用)。

autorace.jp の レース結果API(POST /race_info/RaceResult)を 1.2秒に1回だけ読む。
1レースで 着順・ハンデ・試走タイム・競走タイム・ST と 全券種の払戻(的中票数つき)が取れる。
保存: auto/YYYY-MM.csv (1行=1車)、払戻は auto/refund_YYYY-MM.csv (1行=1レース・JSON)。
auto/_days.txt に済んだ日を書くので、途中で止まっても続きから再開できる。

    python -m src.tools.auto_collect --start 2024-11-01 --end 2026-11-05 --max-minutes 300
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import date, timedelta

import requests

OUT = "auto"
URL = "https://autorace.jp/race_info/RaceResult"
H = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}
PLACES = {2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
FIELDS = ["race_date", "place", "race_no", "car_no", "order", "player_code", "player_name", "handicap",
          "trial_time", "race_time", "st", "accident", "retire", "foul"]


def _post(place: int, d: date, rn: int) -> dict | None:
    for i in range(3):
        try:
            r = requests.post(URL, headers=H, data={"placeCode": str(place), "raceDate": str(d), "raceNo": str(rn)}, timeout=30)
            if r.status_code == 200:
                j = r.json()
                return j.get("body") if j.get("result") == "Success" else {}
            print("status", r.status_code, place, d, rn)
            if r.status_code in (403, 429):
                time.sleep(60 * (i + 1))
        except Exception as e:
            print("NG", place, d, rn, e)
            time.sleep(10)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-11-01")
    ap.add_argument("--end", default="2026-11-05")
    ap.add_argument("--max-minutes", type=float, default=300)
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    dp = os.path.join(OUT, "_days.txt")
    done = set(open(dp).read().split()) if os.path.exists(dp) else set()
    d, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    n_new = 0
    while d <= end:
        if time.time() - t0 > a.max_minutes * 60:
            print("時間切れ。続きは次回")
            break
        if str(d) in done:
            d += timedelta(days=1)
            continue
        rows, refs, blocked = [], [], False
        for place in PLACES:
            for rn in range(1, 13):
                b = _post(place, d, rn)
                time.sleep(1.2)
                if b is None:
                    blocked = True
                    break
                res = b.get("raceResult") or []
                if not res:
                    break  # その日その場は開催なし(1Rが空)、またはレース数の終わり
                for x in res:
                    rows.append({"race_date": str(d), "place": place, "race_no": rn, "car_no": x.get("carNo"),
                                 "order": x.get("order"), "player_code": x.get("playerCode"),
                                 "player_name": (x.get("playerName") or "").replace("　", " "),
                                 "handicap": x.get("handicap"), "trial_time": x.get("traialTime"),
                                 "race_time": x.get("raceTime"), "st": x.get("st"), "accident": x.get("accidentName") or "",
                                 "retire": x.get("retireFlag"), "foul": x.get("foulCode") or ""})
                refs.append({"race_date": str(d), "place": place, "race_no": rn,
                             "refund": json.dumps(b.get("refundInfo") or {}, ensure_ascii=False)})
            if blocked:
                break
        if blocked:
            print("止められた/通信失敗。ここでやめる", d)
            break
        if rows:
            for name, data, fields in ((f"{d:%Y-%m}.csv", rows, FIELDS),
                                       (f"refund_{d:%Y-%m}.csv", refs, ["race_date", "place", "race_no", "refund"])):
                path = os.path.join(OUT, name)
                new = not os.path.exists(path)
                with open(path, "a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fields)
                    if new:
                        w.writeheader()
                    w.writerows(data)
            n_new += len(refs)
            print(d, len(refs), "R")
        with open(dp, "a") as f:
            f.write(str(d) + "\n")
        d += timedelta(days=1)
    print("新しく保存", n_new, "R")


if __name__ == "__main__":
    main()
