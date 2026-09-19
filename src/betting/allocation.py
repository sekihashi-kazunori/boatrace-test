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


def _reason_for(combo: tuple[int, int, int], race_df: pd.DataFrame, category: str) -> str:
    first = race_df[race_df["lane_number"] == combo[0]].iloc[0]
    parts = [f"{combo[0]}gouTei ga jiku"]
    if (first.get("course_base_win_rate") or 0) >= 0.5:
        parts.append("in nige no shinraido ga takai")
    if (first.get("exhibition_rank") or 99) <= 2:
        parts.append("tenji time joui de ashi ga yoi")
    if (first.get("motor_2rate") or 0) >= 40:
        parts.append("motor 2renritsu ga yoi")
    if category == "大穴":
        parts.append("ninkiusu daga shisu")
    elif category == "中穴":
        parts.append("2 3chaku arasoi no")
    return " / ".join(parts)



def build_bet_plan(
    race_df: pd.DataFrame,
    total_stake: int = 1000,
    min_points: int = 6,
    max_points: int = 8,
    odds_map: dict[tuple[int, int, int], float] | None = None,
) -> list[BetPlan]:
    print(f"race_df lane_number列: {race_df['lane_number'].tolist()}")
    win_probs = dict(zip(race_df["lane_number"], race_df["predicted_win_prob"]))
    print(f"win_probs件数: {len(win_probs)} 中身={win_probs}")
    combo_probs = _harville_trifecta_probs(win_probs)

    ranked = sorted(combo_probs.items(), key=lambda x: x[1], reverse=True)

    tetsuban = ranked[:2]
    chuuketsu = ranked[2:5]

    anaba_candidates = [item for item in ranked if item[0][0] in (3, 4, 5, 6)]
    ooana = anaba_candidates[:2] if anaba_candidates else ranked[5:7]

    points = tetsuban[:2] + chuuketsu[: max(1, min(3, max_points - 4))] + ooana[:2]
    points = points[:max_points]
    while len(points) < min_points and len(ranked) > len(points):
        for item in ranked:
            if item not in points:
                points.append(item)
                break

    n_tetsuban = min(len(tetsuban), 2)
    n_ooana = min(len(ooana), 2)
    n_chuuketsu = len(points) - n_tetsuban - n_ooana

    stake_tetsuban_total = int(total_stake * 0.5)
    stake_chuuketsu_total = int(total_stake * 0.3)
    stake_ooana_total = total_stake - stake_tetsuban_total - stake_chuuketsu_total

    plans: list[BetPlan] = []
    for idx, (combo, prob) in enumerate(points):
        if idx < n_tetsuban:
            category = "鉄板"
            stake = round(stake_tetsuban_total / max(n_tetsuban, 1) / 100) * 100
        elif idx < n_tetsuban + n_chuuketsu:
            category = "中穴"
            stake = round(stake_chuuketsu_total / max(n_chuuketsu, 1) / 100) * 100
        else:
            category = "大穴"
            stake = round(stake_ooana_total / max(n_ooana, 1) / 100) * 100

        plans.append(
            BetPlan(
                combination="-".join(map(str, combo)),
                category=category,
                stake=max(stake, 100),
                predicted_prob=round(prob * 100, 2),
                odds=odds_map.get(combo) if odds_map else None,
                reason=_reason_for(combo, race_df, category),
            )
        )
    return plans
