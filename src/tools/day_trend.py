"""その日・その場の流れ(2026-11-10 ご主人様「掘り下げた反省と予想」)。

前のレースの3連単の結果(公式の結果ページ)を plans/day_results/日付.csv にためて、
「その日その場で、1号艇が負けたレースの割合」を出す。

2026年の4.7万Rで確かめた結果(執事の読みは場のクセ・風・選手を入れたもの):
  前のレース(4R以上)で1号艇が75%以上負けている → 1号艇の1着 実際44.4% / 執事の読み48.9%(読みより4.5pt弱い。風4m以上なら5.2pt)
  前のレースで1号艇の負けが25%以下 → 実際59.8% / 読み56.7%(読みより3.2pt強い)
つまり「その日の水面が荒れているか」は、執事の読みに入っていない。本予想と荒天万舟枠の判断に使う。
"""
from __future__ import annotations

import csv
import os
import re
from datetime import date

import requests

DIR = os.path.join("plans", "day_results")
URL = "https://www.boatrace.jp/owpc/pc/race/raceresult?rno={rn}&jcd={jcd}&hd={hd}"
H = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}


PAY = {}  # (日付, 場, R) -> 3連単の払戻(100円あたり)


def _load(d: date) -> dict:
    p = os.path.join(DIR, f"{d}.csv")
    out = {}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                k = (r["stadium_code"], int(r["race_number"]))
                out[k] = r["trifecta"]
                if r.get("payout"):
                    PAY[(str(d),) + k] = int(r["payout"])
    return out


def _save(d: date, k: tuple, tri: str) -> None:
    os.makedirs(DIR, exist_ok=True)
    p = os.path.join(DIR, f"{d}.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["stadium_code", "race_number", "trifecta", "payout"])
        w.writerow([k[0], k[1], tri, PAY.get((str(d),) + k, "")])


def _fetch(stadium_code: str, rn: int, d: date) -> str | None:
    try:
        from bs4 import BeautifulSoup
        r = requests.get(URL.format(rn=rn, jcd=stadium_code, hd=f"{d:%Y%m%d}"), headers=H, timeout=15)
        t = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        m = re.search(r"3連単\s*([1-6])\s*-?\s*([1-6])\s*-?\s*([1-6])\s*¥?\s*([\d,]+)?", t)
        if not m:
            return None
        if m[4]:
            PAY[(str(d), stadium_code, rn)] = int(m[4].replace(",", ""))
        return f"{m[1]}-{m[2]}-{m[3]}"
    except Exception as e:
        print(f"結果の取得失敗 {stadium_code} {rn}R: {e}")
        return None


def result(stadium_code: str, rn: int, d: date) -> tuple[str, int] | None:
    """(3連単の組, 100円あたりの払戻)。まだ出ていなければ None。"""
    have = _load(d)
    k = (stadium_code, rn)
    tri = have.get(k)
    if tri is None or (str(d),) + k not in PAY:
        tri = _fetch(stadium_code, rn, d)
        if tri and k not in have:
            _save(d, k, tri)
    pay = PAY.get((str(d),) + k)
    return (tri, pay) if tri and pay else None


def boat1_lose_rate(stadium_code: str, race_number: int, d: date) -> tuple[int, float | None]:
    """(前のレースの数, 1号艇が負けた割合)。前のレースの結果がまだ出ていなければ取りにいく。"""
    have = _load(d)
    res = []
    for rn in range(1, race_number):
        k = (stadium_code, rn)
        tri = have.get(k)
        if tri is None:
            tri = _fetch(stadium_code, rn, d)
            if tri:
                _save(d, k, tri)
        if tri:
            res.append(tri)
    if not res:
        return 0, None
    return len(res), sum(1 for t in res if not t.startswith("1")) / len(res)
