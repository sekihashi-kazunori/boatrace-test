"""
過去レースの「締切時オッズ」(3連単120通り)を公式サイトから集める。

本物のオッズで「モデルの確率に対してオッズが高い(お得な)買い目」を
検証するため。1日分を odds_hist/YYYY-MM-DD.csv に保存する
(1行=1レース、列=120通りのオッズ。既に保存済みの日は飛ばす)。

    python -m src.tools.collect_odds_history --date 2026-09-01
"""
import argparse
import csv
import itertools
import os
import sqlite3
from datetime import date

from src.collectors.official_site import fetch_odds_3rentan

COMBOS = ["-".join(map(str, p)) for p in itertools.permutations(range(1, 7), 3)]
OUT_DIR = "odds_hist"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    d = date.fromisoformat(args.date)
    path = os.path.join(OUT_DIR, f"{d}.csv")
    if os.path.exists(path):
        print(f"{d}: 保存済み")
        return
    c = sqlite3.connect("boatrace.db")
    races = c.execute(
        "select distinct stadium_code, race_number from race_results where race_date=? order by 1, 2", (str(d),)
    ).fetchall()
    # GitHubから公式サイトまで1リクエスト約9秒かかるため、6本並行で取る
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        st, rn = str(item[0]).zfill(2), int(item[1])
        try:
            odds = fetch_odds_3rentan(st, rn, d)
        except Exception as e:
            print(f"{d} {st} {rn}R 取得失敗: {e!r}")
            odds = {}
        return [st, rn] + [odds.get(tuple(map(int, k.split("-"))), "") for k in COMBOS], bool(odds)

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(one, races))
    rows = [r for r, _ in results]
    ok = sum(1 for _, good in results if good)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stadium_code", "race_number"] + COMBOS)
        w.writerows(rows)
    print(f"{d}: {ok}/{len(races)}レース取得")


if __name__ == "__main__":
    main()
