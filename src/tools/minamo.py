"""予想家「みなも」(note.com/minamo_kyotei・Threads minamo_kyotei) の無料の先出し予想を毎日記録する
(2026-11-14 ご主人様「みなもさんに入り込める？」)。

毎朝の無料記事「全◯場◯レースの予想を先出し（3連単2点）」を noteの公開APIで読み、
tipster/minamo_YYYY-MM-DD.csv に 場・R・締切・◎○▲・買い目2点 を保存する(最初に見た内容で固定)。
採点は夜の答え合わせで plans/day_results と照合する(1点100円)。

    python -m src.tools.minamo collect
"""
from __future__ import annotations

import csv
import html
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

LIST = "https://note.com/api/v2/creators/minamo_kyotei/contents?kind=note&page=1"
NOTE = "https://note.com/api/v3/notes/{}"
H = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}
NAMES = {"桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05", "浜名湖": "06", "蒲郡": "07",
         "常滑": "08", "津": "09", "三国": "10", "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14",
         "丸亀": "15", "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20", "芦屋": "21",
         "福岡": "22", "唐津": "23", "大村": "24"}
F = ["race_date", "stadium_code", "race_number", "deadline", "marks", "combos", "captured_at"]


def text_of(body: str) -> str:
    t = re.sub(r"<br ?/?>", "\n", body or "")
    t = re.sub(r"</p>|</h\d>|</li>", "\n", t)
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def parse(t: str, d: str, now: str) -> list[dict]:
    rows, code = [], None
    for line in t.splitlines():
        m = re.match(r"■\s*(\S+?)（", line.strip())
        if m:
            code = NAMES.get(m.group(1))
            continue
        m = re.match(r"\s*(\d{1,2})R（(\d{1,2}:\d\d)）\s*(◎\d○\d▲\d)\s+(\d-\d-\d)\s*/\s*(\d-\d-\d)", line)
        if m and code:
            rows.append({"race_date": d, "stadium_code": code, "race_number": int(m.group(1)),
                         "deadline": m.group(2), "marks": m.group(3), "combos": f"{m.group(4)} {m.group(5)}",
                         "captured_at": now})
    return rows


def collect() -> int:
    now_dt = datetime.now(ZoneInfo("Asia/Tokyo"))
    d = f"{now_dt:%Y-%m-%d}"
    path = os.path.join("tipster", f"minamo_all_{d}.csv")
    if os.path.exists(path):
        return 0  # 最初に見た内容で固定
    try:
        c = (requests.get(LIST, headers=H, timeout=30).json().get("data") or {}).get("contents") or []
    except Exception as e:
        print("みなもさんの記事一覧の取得失敗", e)
        return 0
    want = f"{now_dt.month}月{now_dt.day}日"
    key = next((x["key"] for x in c if not x.get("price") and "先出し" in (x.get("name") or "")
                and want in (x.get("name") or "")), None)
    if not key:
        print("みなもさんの今日の無料先出し記事はまだ")
        return 0
    try:
        body = (requests.get(NOTE.format(key), headers=H, timeout=30).json().get("data") or {}).get("body") or ""
    except Exception as e:
        print("みなもさんの記事の取得失敗", e)
        return 0
    rows = parse(text_of(body), d, now_dt.isoformat(timespec="seconds"))
    if not rows:
        return 0
    os.makedirs("tipster", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=F)
        w.writeheader()
        w.writerows(rows)
    print("みなもさん 先出し", len(rows), "R を記録")
    return len(rows)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["collect"]:
        collect()
