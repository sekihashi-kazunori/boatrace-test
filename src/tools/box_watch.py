"""456ボックス・1-2-全の記録(紙上・データ取り)。

2026-11-21 ご主人様「456ボックスと1-2-9を新たにデータで取る！！」(1-2-9 = 1-2-全 の4点)。
456ボックスは「強い会場」で取る。強い会場は 2026年1〜10月 約4万Rの結果で、456ボックス(6点)の回収率が上位の4場:
  江戸川 82.5%(前半84・後半80)/浜名湖 81.1%/宮島 78.3%/徳山 74.6%  (全場平均 51.7%)
  ※100%を超える場は無い。データを貯めて、会場+条件(1号艇B級・風など)で100%を超える形を探すための記録。
1-2-全(1着1号艇・2着2号艇・3着は残り4艇、4点)は全場・全レース(全場平均 74.7%)。

買い目は結果(plans/day_results/日付.csv)だけで採点できるので、bet_tickets には書かない。

    python -m src.tools.box_watch 2026-11-21          # その日
    python -m src.tools.box_watch 2026-11-21 --since  # 2026-11-21 からの累計も
"""
from __future__ import annotations

import csv
import glob
import os
import sys
from collections import defaultdict

NM = {"01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川", "06": "浜名湖", "07": "蒲郡",
      "08": "常滑", "09": "津", "10": "三国", "11": "びわこ", "12": "住之江", "13": "尼崎", "14": "鳴門",
      "15": "丸亀", "16": "児島", "17": "宮島", "18": "徳山", "19": "下関", "20": "若松", "21": "芦屋",
      "22": "福岡", "23": "唐津", "24": "大村"}
BOX_VENUES = ("03", "06", "17", "18")  # 456ボックスの強い会場
START = "2026-11-21"


def is_box(t: str) -> bool:
    return set(t.split("-")) == {"4", "5", "6"}


def is_12(t: str) -> bool:
    return t.startswith("1-2-")


REF = {}  # (場, R) -> 返還艇


def load(d: str) -> list[tuple[str, int, str, int]]:
    p = os.path.join("plans", "day_results", f"{d}.csv")
    if not os.path.exists(p):
        return []
    from src.results.refund import load as load_refund
    REF.update(load_refund(d))
    out = []
    for r in csv.DictReader(open(p, encoding="utf-8")):
        if r.get("trifecta") and r.get("payout"):
            out.append((r["stadium_code"], int(r["race_number"]), r["trifecta"], int(r["payout"])))
    return out


def _ref(jcd, rn, combos) -> int:
    """2026-11-21 返還も計算に入れる: 返還艇を含む買い目は100円戻る。"""
    boats = REF.get((jcd, rn), "")
    return 100 * sum(1 for c in combos if boats and any(b in boats for b in c.split("-")))


BOX = [f"{a}-{b}-{c}" for a in "456" for b in "456" for c in "456" if len({a, b, c}) == 3]


def score(rows):
    """{'box': {場: [R, 投資, 回収, 的中]}, '12': {...}}"""
    s = {"box": defaultdict(lambda: [0, 0, 0, 0]), "12": defaultdict(lambda: [0, 0, 0, 0])}
    for jcd, _rn, t, pay in rows:
        rf_b = _ref(jcd, _rn, BOX)
        rf_12 = _ref(jcd, _rn, [f"1-2-{c}" for c in "3456"])
        if jcd in BOX_VENUES:
            a = s["box"][jcd]
            a[0] += 1; a[1] += 600
            a[2] += rf_b
            if is_box(t):
                a[2] += pay; a[3] += 1
        a = s["12"][jcd]
        a[0] += 1; a[1] += 400
        a[2] += rf_12
        if is_12(t):
            a[2] += pay; a[3] += 1
    return s


def fmt(name, s):
    lines = []
    for key, lab in (("box", "456ボックス(強い会場・6点600円)"), ("12", "1-2-全(全場・4点400円)")):
        v = s[key]
        tot = [sum(x[i] for x in v.values()) for i in range(4)]
        rate = tot[2] / tot[1] * 100 if tot[1] else 0
        lines.append(f"【{name}】{lab}: {tot[0]}R 的中{tot[3]} 投資{tot[1]:,} 回収{tot[2]:,} 回収率{rate:.1f}%")
        for jcd, a in sorted(v.items(), key=lambda x: -x[1][2] / max(1, x[1][1])):
            lines.append(f"  {NM.get(jcd, jcd)} {a[0]}R 的中{a[3]} {a[2] / a[1] * 100:.1f}%")
    return "\n".join(lines)


def main():
    d = sys.argv[1]
    rows = load(d)
    print(fmt(d, score(rows)))
    for jcd, rn, t, pay in rows:
        if (jcd in BOX_VENUES and is_box(t)) or is_12(t):
            print(f"  的中 {NM.get(jcd, jcd)}{rn}R {t} {pay:,}円")
    if "--since" in sys.argv:
        allr = []
        for p in sorted(glob.glob(os.path.join("plans", "day_results", "*.csv"))):
            dd = os.path.basename(p)[:-4]
            if START <= dd <= d:
                allr += load(dd)
        print(fmt(f"{START}〜{d} 累計", score(allr)))


if __name__ == "__main__":
    main()
