"""2026年の中央競馬G1(J・G1を含む)の結果と全券種の払戻を残す(紙上の検証・研究用)。

2026-11-28 ご主人様「執事君は2026のG1のデータは全て取っておいて!」
(有馬記念に向けた紙上検証の材料。ジャパンカップ 11/29 の紙上の買い目は keiba_paper/ にある)

netkeiba の レース一覧(日付ごと) → G1 のレース結果ページ を 1.2秒に1回のゆっくりした間隔で読む。
G1 の見分け方: レース一覧のレース名に (G1)/(GI)/(JG1) が付いているもの。一覧で分からない日は 9〜12R の結果ページを開いて名前で確かめる。
保存(同じレースは上書きしないで飛ばす。途中で止まっても続きから再開できる):
  keiba/g1/2026_results.csv  1行=1頭(レース名・着順・馬名・騎手・調教師・単勝オッズ・人気・通過・上り など)
  keiba/g1/2026_payouts.csv  1行=1つの払戻(単勝・複勝・枠連・馬連・ワイド・馬単・3連複・3連単)

    python -m src.tools.keiba_g1 --start 2026-01-01 --end 2026-12-31
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import time
from datetime import date, timedelta

from src.tools.keiba_collect import PLACE, _get

OUT = os.path.join("keiba", "g1")
RES = os.path.join(OUT, "2026_results.csv")
PAY = os.path.join(OUT, "2026_payouts.csv")
RES_F = ["race_id", "race_date", "place", "place_name", "race_number", "race_name", "surface", "distance", "turn",
         "weather", "going", "n_horses", "rank", "waku", "umaban", "horse", "sex_age", "kinryo", "jockey", "time",
         "margin", "passing", "last3f", "win_odds", "popularity", "weight", "trainer"]
PAY_F = ["race_id", "race_date", "race_name", "bet_type", "combination", "payout", "popularity"]
G1_RE = re.compile(r"[(（]\s*(?:J\.?・?)?G\s*(?:1|I|Ⅰ)\s*[)）]")


def _txt(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def is_g1(name: str) -> bool:
    return bool(G1_RE.search(name))


def g1_ids_from_list(t: str) -> tuple[list[str], bool]:
    """レース一覧から G1 の race_id を返す。(ids, 一覧にレース名が読めたか)"""
    ids, named = [], False
    for rid, inner in re.findall(r'href="/race/(\d{12})/"[^>]*>(.*?)</a>', t, re.S):
        name = _txt(inner)
        if name and not name.isdigit():
            named = True
        if is_g1(name):
            ids.append(rid)
    for rid, title in re.findall(r'href="/race/(\d{12})/"[^>]*title="([^"]+)"', t):
        named = True
        if is_g1(html.unescape(title)):
            ids.append(rid)
    return sorted(set(ids)), named


def parse(rid: str, t: str) -> tuple[list[dict], list[dict]]:
    i = t.find("data_intro")
    intro_html = t[i:i + 3000] if i >= 0 else ""
    mh = re.search(r"<h1[^>]*>(.*?)</h1>", intro_html, re.S)
    name = _txt(mh.group(1)) if mh else ""
    intro = _txt(intro_html)
    mc = re.search(r"(芝|ダ|障)(右|左|直線)?[^\d]*(\d{3,4})m", intro)
    mw = re.search(r"天候 : (\S+)", intro)
    mg = re.search(r"(?:芝|ダート) : (\S+)", intro)
    md = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", intro)
    common = {"race_id": rid, "race_date": f"{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}" if md else "",
              "place": rid[4:6], "place_name": PLACE.get(rid[4:6], ""), "race_number": int(rid[10:12]),
              "race_name": name, "surface": mc.group(1) if mc else "", "distance": mc.group(3) if mc else "",
              "turn": (mc.group(2) or "") if mc else "", "weather": mw.group(1) if mw else "",
              "going": mg.group(1) if mg else ""}
    rows = []
    m = re.search(r"<table[^>]*race_table_01.*?</table>", t, re.S)
    if m:
        trs = re.findall(r"<tr.*?</tr>", m.group(0), re.S)
        head = [_txt(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", trs[0], re.S)] if trs else []

        def col(c, key, default=None):
            for j, h in enumerate(head):
                if key in h and j < len(c):
                    return c[j]
            return c[default] if default is not None and default < len(c) else ""
        for tr in trs[1:]:
            c = [_txt(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(c) < 10:
                continue
            rows.append({"rank": c[0], "waku": c[1], "umaban": c[2], "horse": col(c, "馬名", 3), "sex_age": col(c, "性齢", 4),
                         "kinryo": col(c, "斤量", 5), "jockey": col(c, "騎手", 6), "time": col(c, "タイム", 7),
                         "margin": col(c, "着差", 8), "passing": col(c, "通過"), "last3f": col(c, "上り"),
                         "win_odds": col(c, "単勝", 16), "popularity": col(c, "人気", 17), "weight": col(c, "馬体重", 18),
                         "trainer": col(c, "調教師")})
    for r in rows:
        r.update(common)
        r["n_horses"] = len(rows)
    pays = []
    for tb in re.findall(r"<table[^>]*pay_table_01.*?</table>", t, re.S):
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S):
            th = re.search(r"<th[^>]*>(.*?)</th>", tr, re.S)
            td = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if not th or len(td) < 2:
                continue
            kind = _txt(th.group(1))
            sp = lambda x: [_txt(y) for y in re.split(r"<br\s*/?>", x)]
            nums, ys = sp(td[0]), sp(td[1])
            pops = sp(td[2]) if len(td) > 2 else [""] * len(nums)
            for n, y, p in zip(nums, ys, pops + [""] * len(nums)):
                if n:
                    pays.append({"race_id": rid, "race_date": common["race_date"], "race_name": name, "bet_type": kind,
                                 "combination": n.replace(" ", ""), "payout": y.replace(",", "").replace("円", ""),
                                 "popularity": p})
    return rows, pays


def _done() -> set[str]:
    if not os.path.exists(RES):
        return set()
    with open(RES, encoding="utf-8") as f:
        return {r["race_id"] for r in csv.DictReader(f)}


def _append(path: str, fields: list[str], rows: list[dict]) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=str(date.today()))
    ap.add_argument("--max-minutes", type=float, default=300)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    t0, done, n_new = time.time(), _done(), 0
    d, end = date.fromisoformat(a.start), min(date.fromisoformat(a.end), date.today())
    while d <= end:
        if time.time() - t0 > a.max_minutes * 60:
            print("時間切れ。続きは次回")
            break
        ds = d.strftime("%Y%m%d")
        t = _get(f"https://db.netkeiba.com/race/list/{ds}/")
        time.sleep(1.2)
        if t is None:
            print("一覧が読めないので止める", ds)
            break
        all_ids = sorted({i for i in re.findall(r"/race/(\d{12})/", t) if i[4:6] in PLACE and i[:4] == ds[:4]})
        if not all_ids:
            d += timedelta(days=1)
            continue
        g1, named = g1_ids_from_list(t)
        cand = g1 if named else [i for i in all_ids if 9 <= int(i[10:12]) <= 12]
        for rid in cand:
            if rid in done:
                continue
            rt = _get(f"https://db.netkeiba.com/race/{rid}/")
            time.sleep(1.2)
            if rt is None:
                print("結果ページが読めない", rid)
                continue
            rows, pays = parse(rid, rt)
            if not rows or not is_g1(rows[0]["race_name"]) or rows[0]["race_date"] != str(d):
                continue
            _append(RES, RES_F, rows)
            _append(PAY, PAY_F, pays)
            done.add(rid)
            n_new += 1
            print(d, rows[0]["place_name"], f"{rows[0]['race_number']}R", rows[0]["race_name"], len(rows), "頭",
                  "払戻", len(pays), "件")
        d += timedelta(days=1)
    print("新しく保存した G1:", n_new, "R / 合計", len(done), "R")


if __name__ == "__main__":
    main()
