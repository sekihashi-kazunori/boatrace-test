"""コロガシの練習(2026-11-10 ご主人様「Sレースで2連チャンの練習してみるか」・紙上・集計外)。

1戦目は自信Sの本予想レースで、10,000円から転がす(2026-11-27夜 ご主人様「これからコロガシ2連ちゃんは1万で行きましょう」。それまでは1,500円)。
  1戦目: 本予想と同じ鉄板6点+穴1点を10,000円で
  当たったら、払戻を全部、次の本予想レース(自信を問わない)で2戦目。2連勝したら成功として記録し、10,000円からやり直し
  (2026-11-23 ご主人様「コロガシは必ずやりなさい！1レースやって2レース目をやらないなんてお門違い」。
   その日に本予想レースが残っていなければ、翌日の最初の本予想レースへ持ち越す)
  外れたら失敗として記録し、次の自信Sのレースで10,000円からやり直し
前のコロガシのレースの結果がまだ出ていない時は、そのレースは見送る(次の本予想レースで)。
状態は plans/korogashi/日付.json。買い目は bet_type "v_koro"(紙上・集計外)。
"""
from __future__ import annotations

import json
import os
from datetime import date

DIR = os.path.join("plans", "korogashi")
START = 10000  # 2026-11-27夜 ご主人様「これからコロガシ2連ちゃんは1万で行きましょう」(11/27までは1,500円)
GOAL = 2  # 2連チャン

# 2026-11-25 ご主人様「10000円を3回くらいコロガシすれば500万くらいになるんじゃ無い？」「やってみるか！」:
# 夢コロガシ(紙上・集計外)。1戦目は自信Sの本予想レースで10,000円、当たれば払戻を全部次の本予想レースへ、3連チャンで成功。
# 普通のコロガシと同じレース・同じ買い目で並べて記録する(状態は plans/korogashi_yume/日付.json、bet_type "v_koro3")
KORO = {"dir": DIR, "start": START, "goal": GOAL, "label": "コロガシ(練習)", "bt": "v_koro"}
YUME = {"dir": os.path.join("plans", "korogashi_yume"), "start": 10000, "goal": 3, "label": "夢コロガシ", "bt": "v_koro3"}


def _path(d: date, cfg: dict | None = None) -> str:
    return os.path.join((cfg or KORO)["dir"], f"{d}.json")


def load(d: date, cfg: dict | None = None) -> dict:
    cfg = cfg or KORO
    p = _path(d, cfg)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    st = {"bank": cfg["start"], "chain": 0, "pending": None, "log": []}
    # 2026-11-23: 前日に1戦目が当たったまま(または結果待ちのまま)終わっていたら、2戦目を今日へ持ち越す
    try:
        from datetime import timedelta
        prev_d = d - timedelta(days=1)
        if os.path.exists(_path(prev_d, cfg)):
            with open(_path(prev_d, cfg), encoding="utf-8") as f:
                prev = json.load(f)
            if prev.get("pending"):
                ok, _msg = resolve(prev_d, prev, cfg)
                if not ok:
                    return st
                save(prev_d, prev, cfg)
            if int(prev.get("chain", 0)) >= 1:
                st.update(bank=int(prev["bank"]), chain=int(prev["chain"]),
                          chain_races=list(prev.get("chain_races", [])), carried_from=str(prev_d),
                          carried_chain=int(prev["chain"]))
    except Exception as e:
        print(f"コロガシの持ち越し確認の失敗: {e}")
    return st


def save(d: date, st: dict, cfg: dict | None = None) -> None:
    cfg = cfg or KORO
    os.makedirs(cfg["dir"], exist_ok=True)
    with open(_path(d, cfg), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def resolve(d: date, st: dict, cfg: dict | None = None) -> tuple[bool, str]:
    """前のコロガシのレースを精算する。(進めてよいか, 報告文)"""
    cfg = cfg or KORO
    lab, start, goal = cfg["label"], cfg["start"], cfg["goal"]
    pend = st.get("pending")
    if not pend:
        return True, ""
    from src.tools.day_trend import result
    rd = date.fromisoformat(pend["date"]) if pend.get("date") else d
    r = result(pend["stadium_code"], int(pend["race_number"]), rd)
    if not r:
        return False, ""
    tri, pay = r
    back = sum(a * pay // 100 for c, a in pend["alloc"] if c == tri)
    name = f"{pend['name']}{pend['race_number']}R"
    if back:
        st["chain"] += 1
        st["bank"] = back
        msg = f"🎲 {lab} {name} ◎的中 {tri}({pay:,}円) → {pend['stake']:,}円が{back:,}円に！ {st['chain']}連チャン"
        if st["chain"] >= goal:
            st["log"].append({"result": "成功", "races": pend.get("chain_races", []) + [name], "final": back})
            msg += f"\n🎉 {goal}連チャン成功！ {start:,}円 → {back:,}円。次は{start:,}円からやり直し"
            st["bank"], st["chain"] = start, 0
            st["chain_races"] = []
        else:
            st["chain_races"] = pend.get("chain_races", []) + [name]
    else:
        st["log"].append({"result": "失敗", "races": pend.get("chain_races", []) + [name], "final": 0})
        msg = f"🎲 {lab} {name} ×外れ(結果{tri})。{st['chain']}連チャンで終了、次は{start:,}円から"
        st["bank"], st["chain"] = start, 0
        st["chain_races"] = []
    st["pending"] = None
    return True, msg
