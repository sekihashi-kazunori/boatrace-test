"""単勝・複勝・2連単・2連複・拡連複の確定オッズと、全券種の払戻を集める(2026-10-27 ご主人様OK)。

3連単はブレが大きく安定しないので、ブレの小さい券種で「世間のクセ」がお金になるかを調べるため。
1日分を odds_hist2/YYYY-MM-DD.csv に保存(1行=1レース)。既に保存済みの日は飛ばす。
  win_1..6       単勝オッズ
  place_1..6     複勝オッズ(下限)
  ex_a-b         2連単オッズ(30通り)
  qn_a=b         2連複オッズ(15通り)
  wd_a=b         拡連複オッズ(下限・15通り)
  payouts        全券種の払戻(openapi の results の payouts をJSONで)

    python -m src.tools.collect_odds_other --date 2026-10-26
"""
import argparse
import csv
import itertools
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests
from bs4 import BeautifulSoup

from src.tools.collect_odds_history import _races_openapi

BASE = "https://www.boatrace.jp/owpc/pc/race"
H = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}
OUT_DIR = "odds_hist2"
EX = [f"{a}-{b}" for a, b in itertools.permutations(range(1, 7), 2)]
QN = [f"{a}={b}" for a, b in itertools.combinations(range(1, 7), 2)]
COLS = ([f"win_{i}" for i in range(1, 7)] + [f"place_{i}" for i in range(1, 7)]
        + [f"ex_{k}" for k in EX] + [f"qn_{k}" for k in QN] + [f"wd_{k}" for k in QN])


def _num(s: str):
    m = re.search(r"\d+(\.\d+)?", s or "")
    return float(m.group()) if m else None


def _get(page, st, rn, d):
    r = requests.get(f"{BASE}/{page}?rno={rn}&jcd={st}&hd={d:%Y%m%d}", headers=H, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return BeautifulSoup(r.text, "html.parser")


def parse_tf(soup) -> dict:
    """単勝・複勝の表(艇番の td と oddsPoint の td)。"""
    out = {}
    for tb in soup.select("div.table1 table"):
        head = tb.get_text(" ", strip=True)[:40]
        kind = "win" if "単勝" in head else ("place" if "複勝" in head else None)
        if not kind:
            continue
        for tr in tb.select("tbody tr"):
            tds = tr.find_all("td")
            boat = next((td.get_text(strip=True) for td in tds if td.get_text(strip=True).isdigit()), None)
            od = next((td.get_text(strip=True) for td in tds if "oddsPoint" in (td.get("class") or [])), None)
            if boat and od:
                out[f"{kind}_{boat}"] = _num(od)
    return out


def parse_pairs(soup, kinds: list[str]) -> dict:
    """2連単・2連複・拡連複の表。行ごとに (相手の艇番, オッズ) の組が 1号艇の列から順に並ぶ。
    kinds: ページ内の表の順に、"ex"(順番あり) か "qn"/"wd"(順番なし)。"""
    out = {}
    tabs = [tb for tb in soup.select("div.table1 table") if tb.select("td.oddsPoint")]
    for kind, tb in zip(kinds, tabs):
        for tr in tb.select("tbody tr"):
            tds = tr.find_all("td")
            for j in range(0, len(tds) - 1, 2):
                b, o = tds[j], tds[j + 1]
                if "oddsPoint" not in (o.get("class") or []):
                    continue
                first, other = j // 2 + 1, b.get_text(strip=True)
                if not other.isdigit():
                    continue
                other = int(other)
                if kind == "ex":
                    out[f"ex_{first}-{other}"] = _num(o.get_text(strip=True))
                else:
                    a, c = sorted((first, other))
                    out[f"{kind}_{a}={c}"] = _num(o.get_text(strip=True))
    return out


def one_race(st: str, rn: int, d: date) -> dict:
    row = {}
    for page, fn in (("oddstf", parse_tf), ("odds2tf", lambda s: parse_pairs(s, ["ex", "qn"])),
                     ("oddsk", lambda s: parse_pairs(s, ["wd"]))):
        try:
            row.update(fn(_get(page, st, rn, d)))
        except Exception as e:
            print(f"{d} {st} {rn}R {page} 取得失敗: {e!r}")
    return row


def payouts(d: date) -> dict:
    from src.tools.fetch_openapi import get, as_list
    res = get("results", d) or {}
    return {(f"{x['race_stadium_number']:02d}", int(x["race_number"])): x.get("payouts") or {}
            for x in as_list(res.get("results", []))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    d = date.fromisoformat(a.date)
    path = os.path.join(OUT_DIR, f"{d}.csv")
    if os.path.exists(path):
        print(f"{d}: 保存済み")
        return
    pay = payouts(d)
    races = _races_openapi(d) or sorted(pay)  # 前日分は openapi_data にまだないので結果のほうから
    if not races:
        print(f"{d}: レース一覧なし")
        return
    with ThreadPoolExecutor(a.workers) as ex:
        rows = list(ex.map(lambda r: one_race(r[0], r[1], d), races))
    os.makedirs(OUT_DIR, exist_ok=True)
    ok = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stadium_code", "race_number"] + COLS + ["payouts"])
        for (st, rn), row in zip(races, rows):
            ok += bool(row)
            w.writerow([st, rn] + [row.get(c, "") for c in COLS] + [json.dumps(pay.get((st, rn), {}), ensure_ascii=False)])
    print(f"{d}: {ok}/{len(races)}レース取得")


if __name__ == "__main__":
    main()
