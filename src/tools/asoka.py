"""あそかぶ(a-so-ka.work)の無料予想を紙上で採点する(2026-10-21 ご主人様OK)。

・朝: 予想ページを取得して 3連単1点の予想と信頼度(A〜E)を asoka/日付.csv に保存
      python -m src.tools.asoka fetch
・翌朝: 全国答え合わせ(rule_check)のあとに、前日分を openapi の結果で採点して asoka_eval.csv に追記し、
      信頼度別の成績を Discord に送る
      python -m src.tools.asoka eval --date 2026-10-21
1点100円で計算。サイトは展示・直前オッズを使っていない(朝6時の出走表と天気予報のみ)。
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

URL = "https://a-so-ka.work/boatrace/boatrace_forecast.html"
DIR = "asoka"
EVAL = "asoka_eval.csv"
NAMES = {"桐生": 1, "戸田": 2, "江戸川": 3, "平和島": 4, "多摩川": 5, "浜名湖": 6, "蒲郡": 7, "常滑": 8, "津": 9,
         "三国": 10, "びわこ": 11, "住之江": 12, "尼崎": 13, "鳴門": 14, "丸亀": 15, "児島": 16, "宮島": 17,
         "徳山": 18, "下関": 19, "若松": 20, "芦屋": 21, "福岡": 22, "唐津": 23, "大村": 24}
CONFS = "ABCDE"

_CARD = re.compile(r'<div class="card" ([^>]*)>(.*?)(?=<div class="card" |\Z)', re.S)
_ATTR = re.compile(r'data-(venue|deadline|race)="([^"]*)"')
_CONF = re.compile(r'class="conf-badge conf-([A-E])"')
_COMBO = re.compile(r'3連単 予想</div>\s*<div class="combo">(.*?)</div>', re.S)
_FRAME = re.compile(r'class="frame f\d">(\d)<')
_TITLE = re.compile(r"<title>[^<]*?(\d{4})/(\d{1,2})/(\d{1,2})")


def parse(text: str) -> tuple[str | None, list[dict]]:
    """予想ページのHTMLから (日付, [{venue, race_number, deadline, conf, combo}]) を取り出す。
    同じレースが表示タブごとに何度も出てくるので、レースごとに1件にまとめる。"""
    m = _TITLE.search(text)
    day = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
    out: dict = {}
    for attrs, body in _CARD.findall(text):
        a = dict(_ATTR.findall(attrs))
        race = html.unescape(a.get("race", ""))
        if "|" not in race or race in out:
            continue
        venue, rno = race.split("|", 1)
        cm, cf = _COMBO.search(body), _CONF.search(body)
        if not cm or not cf:
            continue
        frames = _FRAME.findall(cm.group(1))
        if len(frames) != 3 or venue not in NAMES:
            continue
        out[race] = {"venue": venue, "stadium_code": f"{NAMES[venue]:02d}", "race_number": int(rno),
                     "deadline": a.get("deadline", ""), "conf": cf.group(1), "combo": "-".join(frames)}
    return day, sorted(out.values(), key=lambda r: (r["stadium_code"], r["race_number"]))


def fetch() -> None:
    import requests
    r = requests.get(URL, timeout=60, headers={"User-Agent": "Mozilla/5.0 (boatrace-test research)"})
    r.raise_for_status()
    r.encoding = "utf-8"
    day, rows = parse(r.text)
    today = str(datetime.now(ZoneInfo("Asia/Tokyo")).date())
    if day != today:
        print(f"ページの日付が今日({today})ではない: {day} → 保存しない")
        return
    save(day, rows)


def save(day: str, rows: list[dict]) -> None:
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, f"{day}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["venue", "stadium_code", "race_number", "deadline", "conf", "combo"])
        w.writeheader()
        w.writerows(rows)
    cnt = {c: sum(r["conf"] == c for r in rows) for c in CONFS}
    print(f"{path}: {len(rows)}R 保存 {cnt}")


def evaluate(d: date) -> list[dict]:
    """前日の予想を openapi の結果で採点(1点100円)。"""
    path = os.path.join(DIR, f"{d}.csv")
    if not os.path.exists(path):
        print(f"{path} がない")
        return []
    from src.tools.fetch_openapi import rows_for_day
    res = {}
    for r in rows_for_day(d):
        if r.get("racer_boat_number") == 1 and r.get("trifecta_combination"):
            res[(r["stadium_code"], int(r["race_number"]))] = (r["trifecta_combination"], r.get("trifecta_payout") or 0)
    out = []
    with open(path, encoding="utf-8") as f:
        for p in csv.DictReader(f):
            k = (p["stadium_code"], int(p["race_number"]))
            if k not in res:
                continue  # 中止・データなし
            fo, pay = res[k]
            hit = p["combo"] == fo
            out.append({"race_date": str(d), "venue": p["venue"], "race_number": k[1], "conf": p["conf"],
                        "combo": p["combo"], "result": fo, "hit": int(hit), "ret": int(pay) if hit else 0})
    return out


def _stats(rows):
    n = len(rows)
    h = sum(r["hit"] for r in rows)
    ret = sum(r["ret"] for r in rows)
    return n, h, ret, (ret / (100 * n) * 100 if n else 0.0)


def summary(all_rows: list[dict], day: str) -> str:
    y = [r for r in all_rows if r["race_date"] == day]
    md = f"{int(day[5:7])}/{int(day[8:10])}"
    start = min(r["race_date"] for r in all_rows)
    lines = [f"**🤖 あそかぶ予想 答え合わせ {md}** ※紙上・3連単1点100円",
             "```", "信頼度   本日R 的中 回収 │ 累積R 的中  回収"]
    for c in CONFS:
        t, a = [r for r in y if r["conf"] == c], [r for r in all_rows if r["conf"] == c]
        tn, th, _, tr = _stats(t)
        an, ah, _, ar = _stats(a)
        lines.append(f"{c:<7} {tn:5d} {th:4d} {tr:4.0f}% │ {an:5d} {ah:4d} {ar:4.0f}%")
    tn, th, _, tr = _stats(y)
    an, ah, _, ar = _stats(all_rows)
    lines.append(f"{'合計':<6} {tn:5d} {th:4d} {tr:4.0f}% │ {an:5d} {ah:4d} {ar:4.0f}%")
    lines.append("```")
    hits = sorted((r for r in y if r["hit"]), key=lambda r: -r["ret"])
    if hits:
        lines.append("🎯 " + " / ".join(f"{r['venue']}{r['race_number']}R({r['conf']}) {r['combo']} {r['ret']:,}円"
                                       for r in hits[:8]))
    lines.append(f"累積は{int(start[5:7])}/{int(start[8:10])}〜。比較: 1-2-3を全レース1点買いは過去80%")
    return "\n".join(lines)


def run_eval(d: date, notify: bool = True) -> str | None:
    new = evaluate(d)
    if not new:
        return None
    old = []
    if os.path.exists(EVAL):
        with open(EVAL, encoding="utf-8") as f:
            old = [dict(r, race_number=int(r["race_number"]), hit=int(r["hit"]), ret=int(r["ret"]))
                   for r in csv.DictReader(f) if r["race_date"] != str(d)]
    rows = old + new
    with open(EVAL, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(new[0].keys()))
        w.writeheader()
        w.writerows(rows)
    msg = summary(rows, str(d))
    print(msg)
    if notify:
        try:
            from src.notify.notifier import notify_discord
            notify_discord(msg)
        except Exception as e:
            print(f"通知失敗: {e}")
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "eval", "parse"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--file", default=None, help="parse: 保存済みHTMLから取り出す")
    a = ap.parse_args()
    if a.cmd == "fetch":
        fetch()
    elif a.cmd == "parse":
        day, rows = parse(open(a.file, encoding="utf-8").read())
        save(day, rows)
    else:
        d = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
        run_eval(d)


if __name__ == "__main__":
    main()
