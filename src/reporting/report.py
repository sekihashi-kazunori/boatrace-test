from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.storage.db import BetTicket


def _main_only(tickets):
    return [t for t in tickets if not t.bet_type]


def _summarize(tickets: list[BetTicket]) -> dict:
    # 結果確定済み(hit/miss)の買い目だけを集計する。未確定のまま残った
    # 過去データ(払戻0扱い)が累積に混ざり、回収率が実態より極端に
    # 低く出ていたため。
    tickets = [t for t in _main_only(tickets) if t.result is not None]
    total_stake = sum(t.amount for t in tickets)
    total_payout = sum((t.payout or 0) for t in tickets)

    races = {}
    for t in tickets:
        key = (t.race_date, t.session, t.stadium_code, t.race_number)
        races.setdefault(key, []).append(t)
    race_count = len(races)
    hit_race_count = sum(1 for ts in races.values() if any(t.result == "hit" for t in ts))

    return {
        "race_count": race_count,
        "hit_race_count": hit_race_count,
        "hit_rate": round(hit_race_count / race_count * 100, 1) if race_count else 0.0,
        "total_stake": total_stake,
        "total_payout": total_payout,
        "profit": total_payout - total_stake,
        "recovery_rate": round(total_payout / total_stake * 100, 1) if total_stake else 0.0,
    }


def session_report(engine, race_date: date, session_name: str) -> dict:
    with Session(engine) as db:
        stmt = select(BetTicket).where(
            BetTicket.race_date == str(race_date), BetTicket.session == session_name
        )
        tickets = list(db.scalars(stmt).all())
    summary = _summarize(tickets)
    summary["session"] = session_name
    summary["race_date"] = str(race_date)
    return summary


def daily_report(engine, race_date: date) -> dict:
    with Session(engine) as db:
        stmt = select(BetTicket).where(BetTicket.race_date == str(race_date))
        tickets = list(db.scalars(stmt).all())
    summary = _summarize(tickets)
    summary["race_date"] = str(race_date)
    return summary


# 累積収支の集計開始日。新システム(展示後予想・鉄板4点ルール)の本格稼働日から
# 数え直す(2026-10-10 ご主人様指示でリセット)。
CUMULATIVE_START = date(2026, 10, 11)


def cumulative_report(engine, up_to_date: date) -> dict:
    with Session(engine) as db:
        stmt = select(BetTicket).where(
            BetTicket.race_date >= str(CUMULATIVE_START),
            BetTicket.race_date <= str(up_to_date),
        )
        tickets = list(db.scalars(stmt).all())
    summary = _summarize(tickets)
    summary["up_to_date"] = str(up_to_date)
    summary["since"] = str(CUMULATIVE_START)
    return summary


def format_report(summary: dict, title: str) -> str:
    if "since" in summary:
        title = f"{title}（{summary['since']}〜）"
    lines = [f"【{title}】"]
    lines.append(f"対象レース数: {summary['race_count']}R / 的中: {summary['hit_race_count']}R")
    lines.append(f"的中率: {summary['hit_rate']}%")
    lines.append(f"購入: {summary['total_stake']:,}円 / 払戻: {summary['total_payout']:,}円")
    profit_sign = "+" if summary["profit"] >= 0 else ""
    lines.append(f"収支: {profit_sign}{summary['profit']:,}円 / 回収率: {summary['recovery_rate']}%")
    return "\n".join(lines)



def experiment_report(engine, up_to_date: date, bet_type="exp", race_max: int | None = None) -> dict:
    """紙上検証枠(実験枠 'exp' / 江戸川枠 'edo' / 江戸川456ボックス 'edo456')の累積。複数指定可。"""
    types = [bet_type] if isinstance(bet_type, str) else list(bet_type)
    with Session(engine) as db:
        stmt = select(BetTicket).where(BetTicket.bet_type.in_(types), BetTicket.race_date <= str(up_to_date))
        tickets = [t for t in db.scalars(stmt).all() if t.result is not None
                   and (race_max is None or t.race_number <= race_max)]
    n = len(tickets)
    hits = [t for t in tickets if t.result == "hit"]
    stake = sum(t.amount for t in tickets)
    payout = sum(t.payout or 0 for t in hits)
    races = {(t.race_date, t.stadium_code, t.race_number) for t in tickets}
    hit_races = {(t.race_date, t.stadium_code, t.race_number) for t in hits}
    return {"bets": n, "hits": len(hits), "stake": stake, "payout": payout,
            "races": len(races), "hit_races": len(hit_races),
            "recovery_rate": round(payout / stake * 100, 1) if stake else 0.0}


def format_experiment(r: dict, title: str = "実験枠") -> str:
    if title.startswith("江戸川"):
        return (f"【{title} 累積】※紙上検証\n"
                f"{r['races']}R / 的中{r['hit_races']}R / 購入{r['stake']:,}円 / 払戻{r['payout']:,}円 / 回収率{r['recovery_rate']}%")
    return (f"【実験枠 累積】※本番とは別の紙上検証\n"
            f"{r['bets']}点 / 的中{r['hits']}点 / 購入{r['stake']:,}円 / 払戻{r['payout']:,}円 / 回収率{r['recovery_rate']}%")
