"""予想家「アンダーグラウンド盛田」(note.com/ryuya415) の無料予想を毎日記録して採点する(2026-11-08 ご主人様指示)。

collect: noteの公開APIから記事を読み、初めて見た記事だけ tipster/morita_picks.csv に追記する
         (最初に見た時の本文を残すので、後から書き換えられても採点は最初の予想で行う)。見張りが30分おきに実行。
score:   前日の予想を openapi の結果で採点(1点100円)。tipster/morita_eval.csv に保存し、Discordに報告。

    python -m src.tools.morita collect
    python -m src.tools.morita score --date 2026-11-08
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

OUT = "tipster"
PICKS = os.path.join(OUT, "morita_picks.csv")
EVAL = os.path.join(OUT, "morita_eval.csv")
API = "https://note.com/api/v2/creators/ryuya415/contents?kind=note&page={}"
H = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}
NAMES = {"桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05", "浜名湖": "06", "蒲郡": "07",
         "常滑": "08", "津": "09", "三国": "10", "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14",
         "丸亀": "15", "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20", "芦屋": "21",
         "福岡": "22", "唐津": "23", "大村": "24"}
PF = ["key", "publish_at", "captured_at", "name", "body"]
EF = ["race_date", "stadium_code", "race_number", "points", "result", "payout", "hit", "name"]


def collect() -> int:
    os.makedirs(OUT, exist_ok=True)
    seen = set()
    if os.path.exists(PICKS):
        with open(PICKS, encoding="utf-8") as f:
            seen = {r["key"] for r in csv.DictReader(f)}
    now = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
    new = []
    for page in range(1, 6):
        try:
            j = requests.get(API.format(page), headers=H, timeout=30).json()
        except Exception as e:
            print("盛田さんの記事取得失敗", e)
            break
        d = j.get("data") or {}
        c = d.get("contents") or []
        stop = False
        for x in c:
            if x.get("key") in seen:
                stop = True  # 新しい順に並ぶので、見たことのある記事が出たらそれ以降も見たもの
                continue
            new.append({"key": x.get("key"), "publish_at": x.get("publishAt"), "captured_at": now,
                        "name": x.get("name") or "", "body": x.get("body") or ""})
        if stop or d.get("isLastPage") or not c:
            break
        time.sleep(1)
    if new:
        exists = os.path.exists(PICKS)
        with open(PICKS, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=PF)
            if not exists:
                w.writeheader()
            w.writerows(reversed(new))
    print(f"盛田さんの新しい予想 {len(new)}件")
    return len(new)


def expand(tok: str) -> set[str]:
    """「1-3=全」「34-145-流し」「1-5-246」などを3連単の組に広げる。"""
    tok = tok.replace("＝", "=").replace("流し", "全").strip()
    a6 = "123456"
    out = set()
    if "=" in tok:
        left, t = tok.split("=", 1)
        f, s = left.split("-", 1)
        pairs = ((f, s), (s, f))
    else:
        f, s, t = tok.split("-")
        pairs = ((f, s),)
    for p, q in pairs:
        for i in p:
            for j in (a6 if q == "全" else q):
                for k in (a6 if t == "全" else t):
                    if len({i, j, k}) == 3:
                        out.add(f"{i}-{j}-{k}")
    return out


TOK = re.compile(r"[1-6]+-[1-6]+[=＝][1-6全]+|[1-6]+-[1-6全]+-(?:[1-6]+|全|流し)")
RACE = re.compile(r"(\d{1,2})/(\d{1,2}).*?(" + "|".join(sorted(NAMES, key=len, reverse=True)) + r")\s*(\d{1,2})R")


def parse(name: str, body: str, year: int):
    m = RACE.search(name)
    if not m:
        return None
    combos = set()
    for t in TOK.findall(body):
        try:
            combos |= expand(t)
        except ValueError:
            pass
    return f"{year}-{int(m[1]):02d}-{int(m[2]):02d}", NAMES[m[3]], int(m[4]), combos


def _results(d: date) -> dict:
    from src.tools.fetch_openapi import get, as_list
    res = get("results", d) or {}
    out = {}
    for x in as_list(res.get("results", [])):
        t = (x.get("payouts") or {}).get("trifecta") or []
        if t:
            out[(f"{int(x['race_stadium_number']):02d}", int(x["race_number"]))] = (t[0]["combination"], int(t[0]["payout"] or 0))
    return out


def score(d: date) -> str:
    if not os.path.exists(PICKS):
        return ""
    with open(PICKS, encoding="utf-8") as f:
        picks = list(csv.DictReader(f))
    res = _results(d)
    rows, done = [], set()
    for p in picks:
        r = parse(p["name"], p["body"], d.year)
        if not r or r[0] != str(d) or not r[3]:
            continue
        k = (r[1], r[2])
        if k in done or k not in res:
            continue
        done.add(k)
        combo, pay = res[k]
        hit = combo in r[3]
        rows.append({"race_date": str(d), "stadium_code": r[1], "race_number": r[2], "points": len(r[3]),
                     "result": combo, "payout": pay if hit else 0, "hit": int(hit), "name": p["name"]})
    old = []
    if os.path.exists(EVAL):
        with open(EVAL, encoding="utf-8") as f:
            old = [r for r in csv.DictReader(f) if r["race_date"] != str(d)]
    allr = old + rows
    os.makedirs(OUT, exist_ok=True)
    with open(EVAL, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EF)
        w.writeheader()
        w.writerows(allr)

    def one(x):
        n, h = len(x), sum(int(r["hit"]) for r in x)
        st = sum(int(r["points"]) for r in x) * 100
        py = sum(int(r["payout"]) for r in x)
        if not n:
            return "対象なし"
        return f"{n}R 的中{h}({h / n * 100:.0f}%) │ 平均{st / n / 100:.0f}点 │ {st:,}→{py:,}円 ({py / st * 100:.0f}%)"
    lines = [f"【予想家チェック：アンダーグラウンド盛田 {d.month}/{d.day}】※無料予想を1点100円で買ったら",
             f"　本日: {one(rows)}", f"　累積: {one(allr)}"]
    for r in rows:
        lines.append(f"　{'◎' if r['hit'] else '×'} {r['name'][:24]} {r['points']}点 結果{r['result']}"
                     + (f" {r['payout']:,}円" if r["hit"] else ""))
    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…"
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["collect", "score"])
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    if a.action == "collect":
        collect()
        return
    d = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    msg = score(d)
    print(msg)
    if msg:
        try:
            from src.notify.notifier import notify_discord
            notify_discord(msg)
        except Exception as e:
            print(f"通知失敗: {e}")


if __name__ == "__main__":
    main()
