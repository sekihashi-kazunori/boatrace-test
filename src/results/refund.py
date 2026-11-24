"""返還の扱い(2026-11-21 ご主人様「返還も計算に入れるんだよ」)。

- 欠場などで返還になった艇を含む買い目は、外れではなく「返還」= 掛け金がそのまま戻る。
- レース中止・開催中止のレースの買い目は、全部「返還」。
公式の結果ページの「返還」欄(numberSet1 の中の艇番)から返還艇を読む。
返還艇は plans/day_refund/日付.csv(stadium_code, race_number, boats)にためる。予想家の採点もこれを使う。
"""
from __future__ import annotations

import csv
import os
import re

DIR = os.path.join("plans", "day_refund")
CANCEL_WORDS = ("レース中止", "開催中止", "中止になりました")


def parse_refund_boats(html: str) -> str:
    """結果ページのHTMLから返還艇を '3' や '36' の形で返す(無ければ '')。"""
    i = html.find("<th>返還</th>")
    if i < 0:
        return ""
    j = html.find("決まり手", i)
    seg = html[i + len("<th>返還</th>"): j if j > 0 else i + 3000]
    nums = re.findall(r'numberSet1_number[^>]*>\s*([1-6])\s*<', seg)
    if not nums:  # 書き方が違った時の予備: タグを外して残る艇番
        nums = re.findall(r"[1-6]", re.sub(r"<[^>]+>", " ", re.sub(r'class="[^"]*"', "", seg)))
    return "".join(sorted(set(nums)))


def is_cancelled_page(html: str) -> bool:
    return any(w in html for w in CANCEL_WORDS) and "3連単" not in re.sub(r"<[^>]+>", "", html)


def is_refund(combination: str, boats: str) -> bool:
    # 2026-11-24: 3連複(1=3=4 の形)も返還艇を含めば返還にする(尼崎6Rで外れ扱いになっていた)
    return bool(boats) and any(b in boats for b in re.split(r"[-=]", combination) if b)


def save(d, stadium_code: str, race_number: int, boats: str) -> None:
    if not boats:
        return
    have = load(d)
    if have.get((stadium_code, int(race_number))) == boats:
        return
    os.makedirs(DIR, exist_ok=True)
    p = os.path.join(DIR, f"{d}.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["stadium_code", "race_number", "boats"])
        w.writerow([stadium_code, int(race_number), boats])


def load(d) -> dict:
    p = os.path.join(DIR, f"{d}.csv")
    out = {}
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding="utf-8")):
            out[(r["stadium_code"], int(r["race_number"]))] = r["boats"]
    return out
