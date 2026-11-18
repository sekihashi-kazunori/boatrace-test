"""ボートレース公式「コンピューター予想」を毎日記録する(2026-11-18 ご主人様OK「良いよ」)。

公式サイトの各レースの「予想フォーカス」(3連単)と「自信度」(5段階)を、その日の全場・全レース分まとめて
tipster/pc_YYYY-MM-DD.csv に保存する(最初に取った内容で固定)。採点は夜の答え合わせで plans/day_results と照合。
予想家の予想と同じく「当たる会場・当たる自信度」を探すための勉強用。

    python -m src.tools.pcexpect collect            # 今日の分
    python -m src.tools.pcexpect collect --date 2026-11-18
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

H = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}
URL = "https://www.boatrace.jp/owpc/pc/race/pcexpect?rno={rno}&jcd={jcd}&hd={hd}"
F = ["race_date", "stadium_code", "race_number", "confidence", "combos", "exacta", "captured_at"]


def parse(html: str) -> tuple[list[str], list[str], int | None]:
    """(3連単の買い目, 2連単の買い目, 自信度1〜5)。"6=1-2" は 6-1-2 と 1-6-2 に広げる"""
    i = html.find("予想フォーカス")
    j = html.find("この予想に対する自信度は")
    if i < 0 or j < 0:
        return [], [], None
    seg = html[i:j]
    tri, ex = [], []
    for row in re.findall(r'<div class="numberSet2_row">(.*?)</div>', seg, flags=re.S):
        nums = re.findall(r'numberSet2_number[^>]*>(\d)<', row)
        seps = re.findall(r'</span>\s*([=\-])\s*<span', row)
        if len(nums) == 3 and len(seps) == 2:
            a, b, c = nums
            out = [f"{a}-{b}-{c}"]
            if seps[0] == "=":
                out.append(f"{b}-{a}-{c}")
            if seps[1] == "=":
                out += [f"{a}-{c}-{b}"] + ([f"{b}-{c}-{a}"] if seps[0] == "=" else [])
            tri += [x for x in out if x not in tri]
        elif len(nums) == 2 and len(seps) == 1:
            a, b = nums
            ex += [f"{a}-{b}"] + ([f"{b}-{a}"] if seps[0] == "=" else [])
    m = re.search(r'state2_lv is-lv(\d)', html[j:j + 400])
    return tri, ex, int(m.group(1)) if m else None


def venues(d: date) -> list[str]:
    try:
        r = requests.get(f"https://www.boatrace.jp/owpc/pc/race/index?hd={d:%Y%m%d}", headers=H, timeout=20)
        return sorted(set(re.findall(r"jcd=(\d\d)", r.text)))
    except Exception as e:
        print("開催場の取得失敗", e)
        return []


def collect(d: date | None = None) -> int:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    d = d or now.date()
    path = os.path.join("tipster", f"pc_{d}.csv")
    if os.path.exists(path):
        return 0  # 最初に取った内容で固定
    rows = []
    for jcd in venues(d):
        for rno in range(1, 13):
            try:
                r = requests.get(URL.format(rno=rno, jcd=jcd, hd=f"{d:%Y%m%d}"), headers=H, timeout=20)
                tri, ex, lv = parse(r.text)
            except Exception as e:
                print("取得失敗", jcd, rno, e)
                tri, ex, lv = [], [], None
            if tri:
                rows.append({"race_date": str(d), "stadium_code": jcd, "race_number": rno, "confidence": lv or "",
                             "combos": " ".join(tri), "exacta": " ".join(ex),
                             "captured_at": now.isoformat(timespec="seconds")})
            time.sleep(1.5)
    if not rows:
        print("コンピューター予想が取れませんでした", d)
        return 0
    os.makedirs("tipster", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=F)
        w.writeheader()
        w.writerows(rows)
    print("公式コンピューター予想", len(rows), "R を記録", d)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["collect"])
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    collect(date.fromisoformat(a.date) if a.date else None)


if __name__ == "__main__":
    main()
