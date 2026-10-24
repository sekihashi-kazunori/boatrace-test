"""競艇の女(kyotei-muryou.deyo1245.workers.dev/record)の予想を紙上で採点する(2026-10-24 ご主人様OK)。

記録ページには、締切後に買い目と配分(%)、レース後に結果と「配分どおり1,000円→◯円」が載る。
外れも含めて全部自動で記録しているサイトなので、そのまま読み取って予想屋さん答え合わせに並べる。
  ・1レース1,000円 × 配分% で計算(サイトと同じ)
  ・穴枠は成績が違うので「競艇の女(穴枠)」として分けて数える

    python -m src.tools.kyoteiwoman --file probe/.../01_record.html   # 保存済みHTMLから確認
"""
from __future__ import annotations

import argparse
import html as H
import re
from datetime import date

URL = "https://kyotei-muryou.deyo1245.workers.dev/record"
CODES = {"桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05", "浜名湖": "06", "蒲郡": "07",
         "常滑": "08", "津": "09", "三国": "10", "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14",
         "丸亀": "15", "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20", "芦屋": "21",
         "福岡": "22", "唐津": "23", "大村": "24"}
NAME = "競艇の女"

_TR = re.compile(r'<tr class="d(?:even|odd)">(.*?)</tr>', re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_YEAR = re.compile(r"<title>[^<]*?(\d{4})年(\d{1,2})月")
_PICK = re.compile(r"(\d-\d-\d)\s*<span class=\"pc\">(\d+)%</span>")


def _text(s: str) -> str:
    return H.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def parse(text: str) -> list[dict]:
    m = _YEAR.search(text)
    year = int(m.group(1)) if m else date.today().year
    out = []
    for tr in _TR.findall(text):
        td = _TD.findall(tr)
        if len(td) < 5:
            continue
        mo, dd = map(int, _text(td[0]).split("/"))
        race = td[1]
        mr = re.match(r"\s*([^\d<]+?)(\d+)R", race)
        if not mr or mr.group(1) not in CODES:
            continue
        tag = "穴枠" if 'class="ana"' in race else ("無料" if 'class="free"' in race else "")
        picks = [(c, int(p)) for c, p in _PICK.findall(td[2])]
        status = _text(td[3])
        res = _text(td[4])
        order = re.search(r"\d-\d-\d", res)
        pay = re.search(r"→\s*([\d,]+)円", res)
        out.append({"race_date": str(date(year, mo, dd)), "stadium_code": CODES[mr.group(1)],
                    "race_number": int(mr.group(2)), "tag": tag, "picks": picks, "status": status,
                    "order": order.group() if order else "",
                    "payout": int(pay.group(1).replace(",", "")) if pay else 0})
    return out


def fetch(d: date) -> list[dict]:
    import requests
    hdr = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}
    url = URL if d.strftime("%Y-%m") == date.today().strftime("%Y-%m") else f"{URL}?m={d:%Y-%m}"
    r = requests.get(url, headers=hdr, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    rows = [x for x in parse(r.text) if x["race_date"] == str(d)]
    if not rows and url == URL:  # 月初に前月の分を見るとき
        r = requests.get(f"{URL}?m={d:%Y-%m}", headers=hdr, timeout=30)
        r.encoding = "utf-8"
        rows = [x for x in parse(r.text) if x["race_date"] == str(d)]
    return rows


def eval_rows(rows: list[dict]) -> list[dict]:
    """tipster_eval.csv の形にする。結果が出ていないレース・中止は入れない。"""
    out = []
    for x in rows:
        if x["status"] not in ("的中", "ハズレ") or not x["picks"]:
            continue
        out.append({"race_date": x["race_date"], "tipster": NAME + ("(穴枠)" if x["tag"] == "穴枠" else ""),
                    "kind": "3t", "stadium_code": x["stadium_code"], "race_number": x["race_number"],
                    "stake": 1000, "payout": x["payout"] if x["status"] == "的中" else 0,
                    "hit": int(x["status"] == "的中"), "result": x["order"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    a = ap.parse_args()
    rows = eval_rows(parse(open(a.file, encoding="utf-8").read()))
    st, py = sum(r["stake"] for r in rows), sum(r["payout"] for r in rows)
    print(f"{len(rows)}R 的中{sum(r['hit'] for r in rows)} {st:,}→{py:,}円 ({py / st * 100:.0f}%)")


if __name__ == "__main__":
    main()
