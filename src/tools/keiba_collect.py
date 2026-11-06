"""中央競馬(JRA)の確定単勝オッズと着順を集める(2026-11-06 ご主人様OK・研究用)。

netkeiba の レース一覧(日付ごと) → レース結果ページ を、1.2秒に1回のゆっくりした間隔で読む。
保存: keiba/YYYY-MM.csv (1行=1頭)。保存済みのレースは飛ばすので、途中で止まっても続きから再開できる。

    python -m src.tools.keiba_collect --start 2024-11-01 --end 2026-10-31 --max-minutes 330
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import time
from datetime import date, timedelta

import requests

OUT = "keiba"
H = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}
FIELDS = ["race_id", "race_date", "place", "race_number", "surface", "distance", "turn", "weather", "going",
          "race_class", "n_horses", "rank", "waku", "umaban", "sex_age", "kinryo", "jockey", "win_odds",
          "popularity", "weight", "win_pay", "place_pay"]
PLACE = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京", "06": "中山", "07": "中京",
         "08": "京都", "09": "阪神", "10": "小倉"}


def _get(url: str) -> str | None:
    for i in range(3):
        try:
            r = requests.get(url, headers=H, timeout=30)
            if r.status_code == 200:
                return r.content.decode("euc-jp", "replace")
            if r.status_code in (403, 429):
                print("止められた", r.status_code, url)
                time.sleep(60 * (i + 1))
        except Exception as e:
            print("NG", url, e)
            time.sleep(10)
    return None


def _txt(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_race(rid: str, t: str) -> list[dict]:
    m = re.search(r"<table[^>]*race_table_01.*?</table>", t, re.S)
    if not m:
        return []
    intro = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t[t.find("data_intro"):t.find("data_intro") + 1500])))
    mc = re.search(r"(芝|ダ|障)(右|左|直線)?[^\d]*(\d{3,4})m", intro)
    mw = re.search(r"天候 : (\S+)", intro)
    mg = re.search(r"(?:芝|ダート) : (\S+)", intro)
    md = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", intro)
    mk = re.search(r"\d+回\S+?\d+日目\s*(\S+)", intro)
    rows = []
    for tr in re.findall(r"<tr.*?</tr>", m.group(0), re.S)[1:]:
        c = [_txt(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(c) < 19:
            continue
        rows.append({"rank": c[0], "waku": c[1], "umaban": c[2], "sex_age": c[4], "kinryo": c[5],
                     "jockey": c[6], "win_odds": c[16], "popularity": c[17], "weight": c[18]})
    # 払戻(単勝・複勝)
    win, plc = {}, {}
    p = re.search(r"pay_table_01.*?</table>", t, re.S)
    if p:
        for tr in re.findall(r"<tr>(.*?)</tr>", p.group(0), re.S):
            th = re.search(r"<th[^>]*>(.*?)</th>", tr, re.S)
            td = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if not th or len(td) < 2:
                continue
            kind = _txt(th.group(1))
            nums = [_txt(x) for x in re.split(r"<br\s*/?>", td[0])]
            pays = [_txt(x).replace(",", "") for x in re.split(r"<br\s*/?>", td[1])]
            for n, y in zip(nums, pays):
                if kind == "単勝":
                    win[n] = y
                elif kind == "複勝":
                    plc[n] = y
    common = {"race_id": rid, "race_date": (f"{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}" if md else ""),
              "place": rid[4:6], "race_number": int(rid[10:12]),
              "surface": mc.group(1) if mc else "", "distance": mc.group(3) if mc else "",
              "turn": (mc.group(2) or "") if mc else "", "weather": mw.group(1) if mw else "",
              "going": mg.group(1) if mg else "", "race_class": mk.group(1) if mk else "", "n_horses": len(rows)}
    for r in rows:
        r.update(common)
        r["win_pay"] = win.get(r["umaban"], "0")
        r["place_pay"] = plc.get(r["umaban"], "0")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-11-01")
    ap.add_argument("--end", default="2026-10-31")
    ap.add_argument("--max-minutes", type=float, default=330)
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    done_days = set()
    if os.path.exists(os.path.join(OUT, "_days.txt")):
        done_days = set(open(os.path.join(OUT, "_days.txt")).read().split())
    d, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    n_new = 0
    while d <= end:
        if time.time() - t0 > a.max_minutes * 60:
            print("時間切れ。続きは次回")
            break
        ds = d.strftime("%Y%m%d")
        if ds in done_days:
            d += timedelta(days=1)
            continue
        t = _get(f"https://db.netkeiba.com/race/list/{ds}/")
        time.sleep(1.2)
        if t is None:
            break  # 止められたら無理に続けない
        ids = sorted(set(re.findall(r"/race/(\d{12})/", t)))
        ids = [i for i in ids if i[4:6] in PLACE and i[:4] == ds[:4]]
        day_rows, ok = [], True
        for rid in ids:
            rt = _get(f"https://db.netkeiba.com/race/{rid}/")
            time.sleep(1.2)
            if rt is None:
                ok = False
                break
            rows = parse_race(rid, rt)
            day_rows += [r for r in rows if r["race_date"] == str(d)]
        if not ok:
            break
        if day_rows:
            path = os.path.join(OUT, f"{d:%Y-%m}.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerows(day_rows)
            n_new += len({r["race_id"] for r in day_rows})
            print(d, len({r['race_id'] for r in day_rows}), "R")
        with open(os.path.join(OUT, "_days.txt"), "a") as f:
            f.write(ds + "\n")
        d += timedelta(days=1)
    print("新しく保存", n_new, "R")


if __name__ == "__main__":
    main()
