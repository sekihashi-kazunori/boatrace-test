"""
boatraceopenapi(GitHub Pages)から出走表・直前情報・結果を取得し、
1艇1行のCSV(月ごと gzip)にまとめる。v3モデル用の拡張データ。

    python -m src.tools.fetch_openapi --start 2026-01-01 --end 2026-10-11
"""
import argparse
import gzip
import json
import os
import time
from datetime import date, timedelta

import pandas as pd
import requests

BASE = "https://boatraceopenapi.github.io"
OUT = "openapi_data"


def get(kind: str, d: date):
    url = f"{BASE}/{kind}/v2/{d.year}/{d:%Y%m%d}.json"
    for i in range(3):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"{url} 失敗 {e}")
            time.sleep(2 * (i + 1))
    return None


def rows_for_day(d: date) -> list[dict]:
    prog, prev, res = get("programs", d), get("previews", d), get("results", d)
    if not prog:
        return []
    prev_map = {(p["race_stadium_number"], p["race_number"]): p for p in (prev or {}).get("previews", [])}
    res_map = {(p["race_stadium_number"], p["race_number"]): p for p in (res or {}).get("results", [])}
    rows = []
    for race in prog.get("programs", []):
        k = (race["race_stadium_number"], race["race_number"])
        pv, rs = prev_map.get(k, {}), res_map.get(k, {})
        pv_boats = {b.get("racer_boat_number"): b for b in pv.get("boats", [])}
        rs_boats = {b.get("racer_boat_number"): b for b in rs.get("boats", [])}
        tri = None
        for pay in (rs.get("payouts", {}) or {}).get("trifecta", []) or []:
            tri = pay
            break
        for b in race.get("boats", []):
            n = b.get("racer_boat_number")
            row = {"race_date": str(d), "stadium_code": f"{k[0]:02d}", "race_number": k[1],
                   "race_grade_number": race.get("race_grade_number"), "race_distance": race.get("race_distance")}
            row.update({kk: v for kk, v in b.items() if kk != "racer_name"})
            for kk in ("race_wind", "race_wind_direction_number", "race_wave", "race_weather_number",
                       "race_temperature", "race_water_temperature"):
                row[kk] = pv.get(kk)
            for kk, v in pv_boats.get(n, {}).items():
                if kk != "racer_boat_number":
                    row["pv_" + kk] = v
            rb = rs_boats.get(n, {})
            row["finish_place"] = rb.get("racer_place_number")
            row["result_course"] = rb.get("racer_course_number")
            row["result_st"] = rb.get("racer_start_timing")
            row["trifecta_combination"] = tri.get("combination") if tri else None
            row["trifecta_payout"] = tri.get("amount") if tri else None
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    a = ap.parse_args()
    d, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    os.makedirs(OUT, exist_ok=True)
    by_month: dict[str, list] = {}
    sample_done = False
    while d <= end:
        rows = rows_for_day(d)
        print(d, len(rows))
        if rows and not sample_done:
            # 1日分の生JSONの形を確認用に保存(小さいので先頭だけ)
            with open(os.path.join(OUT, "sample_keys.json"), "w") as f:
                json.dump({k: str(v) for k, v in rows[0].items()}, f, ensure_ascii=False, indent=1)
            sample_done = True
        by_month.setdefault(f"{d:%Y-%m}", []).extend(rows)
        d += timedelta(days=1)
    for m, rows in by_month.items():
        pd.DataFrame(rows).to_csv(os.path.join(OUT, f"{m}.csv.gz"), index=False, compression="gzip")
        print("保存", m, len(rows))


if __name__ == "__main__":
    main()
