"""その日の流れ(2026-11-11 ご主人様「30分間隔で全レース見て、その日の動きを確認して色々な流れを読んでほしい」)。

30分おきに、今日開催の全場の結果(公式の結果ページ)を集めて plans/day_results/日付.csv にためる(day_trend と共通)。
場ごとに「1号艇の勝ち負け・1-456-456・万舟・決まり方」を数えて、目立つ流れがあればDiscordに知らせる。
同じ内容は2回送らない(plans/day_flow/日付.txt)。

    python -m src.tools.day_flow
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from src.tools import day_trend as T

NM = {"01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川", "06": "浜名湖", "07": "蒲郡",
      "08": "常滑", "09": "津", "10": "三国", "11": "びわこ", "12": "住之江", "13": "尼崎", "14": "鳴門",
      "15": "丸亀", "16": "児島", "17": "宮島", "18": "徳山", "19": "下関", "20": "若松", "21": "芦屋",
      "22": "福岡", "23": "唐津", "24": "大村"}
H = {"User-Agent": "Mozilla/5.0 (boatrace-test research)"}
DIR = os.path.join("plans", "day_flow")


def venues(d) -> list[str]:
    try:
        r = requests.get(f"https://www.boatrace.jp/owpc/pc/race/index?hd={d:%Y%m%d}", headers=H, timeout=20)
        return sorted(set(re.findall(r"jcd=(\d\d)", r.text)))
    except Exception as e:
        print("開催場の取得失敗", e)
        return []


def collect(d) -> dict:
    """{場: [(R, 3連単, 払戻)]}。まだ取っていないレースだけ公式から取る(結果が無いRで止める)。"""
    have = T._load(d)
    out = {}
    for jcd in venues(d):
        rows = []
        for rn in range(1, 13):
            k = (jcd, rn)
            tri = have.get(k)
            if tri is None:
                tri = T._fetch(jcd, rn, d)
                time.sleep(0.5)
                if not tri:
                    break
                T._save(d, k, tri)
            rows.append((rn, tri, T.PAY.get((str(d), jcd, rn))))
        if rows:
            out[jcd] = rows
    return out


def is1456(t: str) -> bool:
    return t[0] == "1" and t[2] in "456" and t[4] in "456"


def summary(d, data: dict) -> str:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    lines, notes = [], []
    tot = sum(len(v) for v in data.values())
    for jcd, rows in sorted(data.items(), key=lambda x: -len(x[1])):
        n = len(rows)
        w1 = sum(1 for _r, t, _p in rows if t[0] == "1")
        f456 = [r for r, t, _p in rows if is1456(t)]
        man = [r for r, _t, p in rows if p and p >= 10000]
        heads = {}
        for _r, t, _p in rows:
            heads[t[0]] = heads.get(t[0], 0) + 1
        nm = NM.get(jcd, jcd)
        lines.append(f"{nm} {n}R：1号艇{w1}勝{n - w1}敗" + (f"・1-456-456 {len(f456)}本" if f456 else "")
                     + (f"・万舟{len(man)}本" if man else ""))
        if n >= 4 and (n - w1) / n >= 0.6:
            notes.append(f"⚠️ {nm}は荒れ水面：1号艇が{n}R中{n - w1}回負け(本予想は見送り・荒天万舟の狙い目)")
        if n >= 4 and w1 / n >= 0.8:
            notes.append(f"🛡️ {nm}は本命水面：1号艇が{n}R中{w1}勝")
        if len(f456) >= 2:
            notes.append(f"🔥 {nm}は1-456-456の流れ：{'R・'.join(map(str, f456))}Rで{len(f456)}本")
        if len(man) >= 3:  # 万舟は普段でも10Rに1〜2本出るので、3本から知らせる
            notes.append(f"💥 {nm}は万舟が{len(man)}本({'R・'.join(map(str, man))}R)")
        out_heads = sum(v for h, v in heads.items() if h in "456")
        if n >= 5 and out_heads / n >= 0.4:
            notes.append(f"🌊 {nm}は外の頭が多い：4〜6号艇の頭が{n}R中{out_heads}回")
    all_w1 = sum(1 for v in data.values() for _r, t, _p in v if t[0] == "1")
    all_456 = sum(1 for v in data.values() for _r, t, _p in v if is1456(t))
    head = (f"【その日の流れ {now:%H:%M}】全{tot}R 1号艇{all_w1}勝({all_w1 / max(tot, 1) * 100:.0f}%・普段は約55%)"
            f"・1-456-456 {all_456}本({all_456 / max(tot, 1) * 100:.1f}%・普段は8%)")
    return "\n".join([head] + (notes or ["目立つ流れはまだありません"]) + ["―"] + lines)


def main():
    d = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    data = collect(d)
    if not data:
        print("結果なし")
        return
    msg = summary(d, data)
    print(msg)
    os.makedirs(DIR, exist_ok=True)
    p = os.path.join(DIR, f"{d}.txt")
    key = "\n".join(l for l in msg.split("\n")[1:] if l.startswith(("⚠️", "🛡️", "🔥", "💥", "🌊")))
    last = open(p, encoding="utf-8").read() if os.path.exists(p) else None
    # 2026-11-11 ご主人様「30分おきの報告は要らない。執事が確認するだけでよい」: Discordには送らず、記録だけ残す
    if key and key != last:
        with open(p, "w", encoding="utf-8") as f:
            f.write(key)
    with open(os.path.join(DIR, f"{d}_log.txt"), "a", encoding="utf-8") as f:
        f.write(msg + "\n\n")


if __name__ == "__main__":
    main()
