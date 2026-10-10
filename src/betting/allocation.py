from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

import pandas as pd


@dataclass
class BetPlan:
    combination: str
    category: str
    stake: int
    predicted_prob: float
    odds: float | None = None
    reason: str = ""


def _harville_trifecta_probs(win_probs: dict[int, float]) -> dict[tuple[int, int, int], float]:
    lanes = list(win_probs.keys())
    result = {}
    for i, j, k in permutations(lanes, 3):
        p_i = win_probs[i]
        remaining_after_i = 1 - p_i
        if remaining_after_i <= 0:
            continue
        p_j_given_i = win_probs[j] / remaining_after_i
        remaining_after_ij = remaining_after_i - win_probs[j]
        if remaining_after_ij <= 0:
            continue
        p_k_given_ij = win_probs[k] / remaining_after_ij
        result[(i, j, k)] = p_i * p_j_given_i * p_k_given_ij
    return result


def _category_for_rank(prob_rank: int) -> str:
    """確率の高さの順位(0始まり)に基づく表示用カテゴリ。

    採用する買い目・金額配分は期待値(確率×オッズ)ベースだが、
    根拠文言での「本命/中穴/大穴」ラベルは確率の順位で付ける。
    (期待値が高くて選ばれても、確率自体は低い人気薄なら「大穴」
    のままにしたいため)
    """
    if prob_rank < 2:
        return "鉄板"
    if prob_rank < 5:
        return "中穴"
    return "大穴"


def _reason_for(combo: tuple[int, int, int], race_df: pd.DataFrame, category: str) -> str:
    first = race_df[race_df["lane_number"] == combo[0]].iloc[0]
    parts = [f"{combo[0]}号艇が軸"]
    if (first.get("course_base_win_rate") or 0) >= 0.5:
        parts.append("イン・逃げの信頼度が高い")
    if (first.get("exhibition_rank") or 99) <= 2:
        parts.append("展示タイム上位で足が良い")
    if (first.get("motor_2rate") or 0) >= 40:
        parts.append("モーター2連率が良い")
    if category == "大穴":
        parts.append("人気薄だが狙い目")
    elif category == "中穴":
        parts.append("2・3着争いの中穴")
    return " / ".join(parts)


def _ceil100(x: float) -> int:
    return int(-(-x // 100) * 100)


def build_bet_plan(
    race_df: pd.DataFrame,
    total_stake: int = 1000,
    odds_map: dict[tuple[int, int, int], float] | None = None,
    n_teppan: int = 4,
    chuuana_range: tuple[float, float] = (15.0, 40.0),
    ooana_range: tuple[float, float] = (40.0, 100.0),
    **_ignored,
) -> list[BetPlan]:
    """
    レースの買い目と金額配分を決める(2026-10-10 ご主人様指定ルール)。
    方針: まずは的中率を上げる。

    鉄板4点 = 予測確率の上位4点(オッズに関係なく、自信がある順)
      - オッズの低い2点: 5倍未満なら300円、5倍以上なら200円
      - 残り2点: 100円
    残りの予算で中穴(15〜40倍)・大穴(40〜100倍)を確率順に100円ずつ:
      - 残り400円 → 中穴2点・大穴2点(計8点)
      - 残り300円 → 中穴2点・大穴1点(計7点)
      - 残り200円 → 中穴1点・大穴1点(計6点)
    合計は常に1000円。オッズが取得できないレースは見送り。
    """
    win_probs = dict(zip(race_df["lane_number"], race_df["predicted_win_prob"]))
    combo_probs = _harville_trifecta_probs(win_probs)
    prob_ranked = sorted(combo_probs.items(), key=lambda x: x[1], reverse=True)

    if not odds_map:
        print("オッズ未取得のため見送り")
        return []

    ranked = [(c, p, odds_map[c]) for c, p in prob_ranked if odds_map.get(c) is not None]
    if len(ranked) < n_teppan:
        print("オッズが足りないため見送り")
        return []

    # --- 鉄板4点 ---
    chosen = ranked[:n_teppan]
    low_two = {c for c, _, _ in sorted(chosen, key=lambda x: x[2])[:2]}
    teppan = []
    for c, p, o in chosen:
        if c in low_two:
            stake = 300 if o < 5.0 else 200
        else:
            stake = 100
        teppan.append((c, p, o, stake))

    rest = total_stake - sum(t[3] for t in teppan)
    n_ana = rest // 100
    n_chuuana = (n_ana + 1) // 2   # 4→2, 3→2, 2→1
    n_ooana = n_ana - n_chuuana    # 4→2, 3→1, 2→1

    # --- 中穴・大穴(確率順に100円) ---
    picked = {c for c, _, _, _ in teppan}

    def pick_band(lo, hi, n):
        out = []
        for c, p, o in ranked:
            if len(out) >= n:
                break
            if c in picked or not (lo <= o < hi):
                continue
            out.append((c, p, o))
            picked.add(c)
        return out

    chuuana = pick_band(*chuuana_range, n_chuuana)
    ooana = pick_band(*ooana_range, n_ooana)
    # 帯の中に足りない場合は、確率順で残りの点から補う(合計1000円を守る)
    shortage = n_ana - len(chuuana) - len(ooana)
    if shortage > 0:
        extra = pick_band(10.0, 10**9, shortage)
        chuuana += extra
    if len(chuuana) + len(ooana) < n_ana:
        print("中穴・大穴の点数が足りないため見送り")
        return []

    plans: list[BetPlan] = []
    for c, p, o, st in teppan:
        plans.append(BetPlan("-".join(map(str, c)), "鉄板", st, round(p * 100, 2), o,
                             _reason_for(c, race_df, "鉄板")))
    for label, group in (("中穴", chuuana), ("大穴", ooana)):
        for c, p, o in group:
            plans.append(BetPlan("-".join(map(str, c)), label, 100, round(p * 100, 2), o,
                                 _reason_for(c, race_df, label)))
    return plans
