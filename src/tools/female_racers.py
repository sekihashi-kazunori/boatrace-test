"""女子選手の名簿(2026-11-10 ご主人様「レディースは的中しやすい。本予想に組み込め」)。

公式データに「レディース戦」の印が無いので、レース名にヴィーナス・レディース・プリンセス・クイーン・女子などが
付いたレースに出た選手を女子選手として models/female_racers.json にためる(毎朝、前日の出走表から足す)。
女子選手が4人以上いるレースをレディース戦とみなす。

2026年(答えを見ていないレース)で、レディース戦の執事の本予想(6点)は
  自信S・A: 的中67.3%(その他61.1%) / 上位10%まで広げても62.3%(その他の自信S・Aと同じくらい)
    python -m src.tools.female_racers update --date 2026-11-09
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PATH = os.path.join("models", "female_racers.json")
PAT = re.compile(r"ヴィーナス|ビーナス|レディース|レディ|女子|プリンセス|クイーン|女王")
_S = None


def load() -> set:
    global _S
    if _S is None:
        try:
            with open(PATH, encoding="utf-8") as f:
                _S = {int(x) for x in json.load(f)}
        except Exception:
            _S = set()
    return _S


def is_ladies(numbers) -> bool:
    s = load()
    return sum(1 for n in numbers if n is not None and int(n) in s) >= 4


def update(d: date) -> None:
    from src.tools.fetch_openapi import get, as_list
    res = get("programs", d) or {}
    s = set(load())
    before = len(s)
    for p in as_list(res.get("programs", [])):
        if PAT.search(p.get("race_title") or "") or PAT.search(p.get("race_subtitle") or ""):
            s |= {int(b["racer_number"]) for b in p.get("boats") or [] if b.get("racer_number")}
    if len(s) > before:
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(s), f)
    print(f"女子選手の名簿 {before}→{len(s)}人")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["update"])
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    update(d)


if __name__ == "__main__":
    main()
