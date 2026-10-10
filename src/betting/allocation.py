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
    n_teppan: int = 6,
    chuuana_range: tuple[float, float] = (35.0, 70.0),
    ooana_range: tuple[float, float] = (70.0, 150.0),
    **_ignored,
) -> list[BetPlan]:
    """
    レースの買い目と金額配分を決める(2026-10-10夜 ご主人様指定ルール)。
    方針: まずは的中率を上げる。

    鉄板と中穴のオッズが重なっていた(鉄板に16〜30倍が混ざり、中穴15〜40倍と
    ほぼ同じ)ため、鉄板を厚くし、中穴・大穴は1点ずつ本当に離れた配当を狙う。

    鉄板 = 予測確率の上位から(オッズに関係なく自信がある順)
      - オッズの低い2点: 5倍未満なら300円、5倍以上なら200円
      - 残り: 100円
    中穴1点 = オッズ35〜70倍(50倍前後)の中で予測確率が最も高い点に100円
    大穴1点 = オッズ70〜150倍(100倍前後)の中で予測確率が最も高い点に100円

    合計は常に1000円。鉄板の点数で調整する:
      - 5倍未満なし → 鉄板6点(200,200,100×4)+中穴+大穴 = 8点
      - 5倍未満1点 → 鉄板5点(300,200,100×3)+中穴+大穴 = 7点
      - 5倍未満2点 → 鉄板4点(300,300,100×2)+中穴+大穴 = 6点
    オッズが取得できないレースは見送り。
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

    # --- 中穴・大穴 各1点(鉄板候補とは重ならないよう、鉄板候補の外から選ぶ) ---
    teppan_pool = {c for c, _, _ in ranked[:n_teppan]}

    def pick_one(lo, hi, exclude):
        for c, p, o in ranked:
            if c not in exclude and lo <= o < hi:
                return (c, p, o)
        return None

    chuuana = pick_one(*chuuana_range, teppan_pool)
    ooana = pick_one(*ooana_range, teppan_pool | ({chuuana[0]} if chuuana else set()))
    ana = [x for x in ((chuuana, "中穴"), (ooana, "大穴")) if x[0] is not None]
    ana_budget = 100 * len(ana)

    # --- 鉄板: 予算に収まるまで点数を減らす(6→5→4) ---
    teppan_budget = total_stake - ana_budget
    teppan = []
    for n in range(n_teppan, 1, -1):
        chosen = ranked[:n]
        low_two = sorted(chosen, key=lambda x: x[2])[:2]
        stake_of = {c: (300 if o < 5.0 else 200) for c, _, o in low_two}
        stakes = [stake_of.get(c, 100) for c, _, _ in chosen]
        if sum(stakes) <= teppan_budget:
            teppan = [(c, p, o, st) for (c, p, o), st in zip(chosen, stakes)]
            break

    if not teppan:
        print("鉄板を組めないため見送り")
        return []

    # 中穴・大穴が帯の中に見つからなかった等で余りが出たら、確率1位の鉄板に上乗せ
    rest = total_stake - ana_budget - sum(t[3] for t in teppan)
    if rest > 0:
        c, p, o, st = teppan[0]
        teppan[0] = (c, p, o, st + rest)

    plans: list[BetPlan] = []
    for c, p, o, st in teppan:
        plans.append(BetPlan("-".join(map(str, c)), "鉄板", st, round(p * 100, 2), o,
                             _reason_for(c, race_df, "鉄板")))
    for (c, p, o), label in ana:
        plans.append(BetPlan("-".join(map(str, c)), label, 100, round(p * 100, 2), o,
                             _reason_for(c, race_df, label)))
    return plans
