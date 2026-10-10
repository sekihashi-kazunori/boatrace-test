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
    n_teppan: int = 5,
    ana_range: tuple[float, float] = (30.0, 100.0),
    **_ignored,
) -> list[BetPlan]:
    """
    レースの買い目と金額配分を決める(2026-10-10深夜 ご主人様指定ルール)。

    10/10ナイターの的中6本がすべて鉄板(4〜17倍)、中穴・大穴は0本だったため、
    鉄板を5点に絞って厚く張り、穴は1点だけにする。

    鉄板5点 = 予測確率の上位5点(オッズに関係なく自信がある順)に計900円
      金額はオッズの逆数に比例(低いオッズほど厚く)させ、100円単位・最低100円。
      → どの鉄板が当たっても払戻がだいたい同じになる。
    穴1点 = オッズ30〜100倍の中で予測確率が最も高い点に100円
      (見つからなければ、その100円も鉄板に回す)
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

    teppan_src = ranked[:n_teppan]
    teppan_set = {c for c, _, _ in teppan_src}
    ana = next(((c, p, o) for c, p, o in ranked if c not in teppan_set and ana_range[0] <= o <= ana_range[1]), None)

    teppan_budget = total_stake - (100 if ana else 0)

    # オッズの逆数に比例配分 → 100円単位に丸め(最低100円)
    inv = [1.0 / o for _, _, o in teppan_src]
    raw = [teppan_budget * w / sum(inv) for w in inv]
    stakes = [max(100, int(r / 100 + 0.5) * 100) for r in raw]
    # 丸めのズレを合わせる: 多すぎたら高オッズ側から削り、足りなければ低オッズ側に足す
    order_low = sorted(range(len(stakes)), key=lambda i: teppan_src[i][2])
    while sum(stakes) > teppan_budget:
        for i in reversed(order_low):
            if stakes[i] > 100:
                stakes[i] -= 100
                break
        else:
            break
    while sum(stakes) < teppan_budget:
        stakes[order_low[0]] += 100

    plans: list[BetPlan] = []
    for (c, p, o), st in zip(teppan_src, stakes):
        plans.append(BetPlan("-".join(map(str, c)), "鉄板", st, round(p * 100, 2), o,
                             _reason_for(c, race_df, "鉄板")))
    if ana:
        c, p, o = ana
        plans.append(BetPlan("-".join(map(str, c)), "穴", 100, round(p * 100, 2), o,
                             _reason_for(c, race_df, "大穴")))
    return plans
