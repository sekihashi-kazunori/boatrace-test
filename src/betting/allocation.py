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


def build_bet_plan(
    race_df: pd.DataFrame,
    total_stake: int = 1000,
    min_points: int = 3,
    max_points: int = 5,
    odds_map: dict[tuple[int, int, int], float] | None = None,
    min_odds: float = 10.0,
    min_ev: float = 1.0,
    max_odds: float = 100.0,
) -> list[BetPlan]:
    """
    レースの買い目と金額配分を決める。

    設計方針(2026-10-10改訂):
    実績を見ると、的中しても配当が4〜9倍の本命ばかりで、しかも
    そこに300円など厚めに張っていたため、当たってもトリガミになり
    回収率が伸びなかった(10/09: 的中3本とも16〜19倍・回収率約30%)。
    そこで以下の3ルールに変更する。

    1. オッズ範囲: min_odds(10倍)未満と max_odds(100倍)超は買わない
       (超大穴はモデルの確率誤差がそのまま期待値を膨らませるため除外)
    2. 期待値下限: 予測確率×オッズ が min_ev(1.0)未満の買い目は買わない
    3. 点数: 条件を満たす買い目から期待値上位 min_points〜max_points 点
       (3〜5点)だけ買う。3点に満たないレースは「見送り」(空リスト)

    金額配分は「どれが当たっても払戻がほぼ同じになる」ように
    オッズの逆数に比例させる(高配当ほど少額)。オッズ10倍以上・
    最大5点なので、どれが当たっても購入額の2倍以上が戻り、
    トリガミが起きない。

    オッズが取得できなかったレースは期待値を判断できないため見送る。
    """
    win_probs = dict(zip(race_df["lane_number"], race_df["predicted_win_prob"]))
    combo_probs = _harville_trifecta_probs(win_probs)

    # 根拠文言用のカテゴリは確率順位で決める(採用順=期待値順とは別)。
    prob_ranked = sorted(combo_probs.items(), key=lambda x: x[1], reverse=True)
    prob_rank_of = {combo: rank for rank, (combo, _) in enumerate(prob_ranked)}

    if not odds_map:
        print("オッズ未取得のため見送り")
        return []

    candidates = []
    for combo, prob in combo_probs.items():
        odds = odds_map.get(combo)
        if odds is None or odds < min_odds or odds > max_odds:
            continue
        ev = prob * odds
        if ev < min_ev:
            continue
        candidates.append((combo, prob, odds, ev))
    candidates.sort(key=lambda x: x[3], reverse=True)
    print(f"条件(オッズ{min_odds}〜{max_odds}倍・期待値{min_ev}以上)を満たす買い目: {len(candidates)}点")

    if len(candidates) < min_points:
        print(f"条件を満たす買い目が{min_points}点未満のため見送り")
        return []

    selected = candidates[:max_points]

    # 払戻均等化: stake ∝ 1/odds
    inv = [1.0 / odds for _, _, odds, _ in selected]
    inv_sum = sum(inv)
    raw_stakes = [total_stake * w / inv_sum for w in inv]

    # 100円単位に丸め、最低100円を保証
    stakes = [max(100, int(s / 100 + 0.5) * 100) for s in raw_stakes]

    # 丸め誤差をtotal_stakeに合わせる(最もオッズの低い=最大配分の点で調整)
    diff = total_stake - sum(stakes)
    if diff != 0:
        max_idx = stakes.index(max(stakes))
        stakes[max_idx] = max(100, stakes[max_idx] + diff)

    plans: list[BetPlan] = []
    for (combo, prob, odds, _ev), stake in zip(selected, stakes):
        category = _category_for_rank(prob_rank_of.get(combo, 99))
        plans.append(
            BetPlan(
                combination="-".join(map(str, combo)),
                category=category,
                stake=stake,
                predicted_prob=round(prob * 100, 2),
                odds=odds,
                reason=_reason_for(combo, race_df, category),
            )
        )

    return plans
