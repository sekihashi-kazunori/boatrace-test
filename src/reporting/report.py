from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.storage.db import BetTicket


def _summarize(tickets: list[BetTicket]) -> dict:
    total_stake = sum(t.amount for t in tickets)
    total_payout = sum((t.payout or 0) for t in tickets)

    races = {}
    for t in tickets:
        key = (t.race_date, t.stadium_code, t.race_number)
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
            BetTicket.race_date == race_date, BetTicket.session == session_name
        )
        tickets = list(db.scalars(stmt).all())
    summary = _summarize(tickets)
    summary["session"] = session_name
    summary["race_date"] = str(race_date)
    return summary


def daily_report(engine, race_date: date) -> dict:
    with Session(engine) as db:
        stmt = select(BetTicket).where(BetTicket.race_date == race_date)
        tickets = list(db.scalars(stmt).all())
    summary = _summarize(tickets)
    summary["race_date"] = str(race_date)
    return summary


def cumulative_report(engine, up_to_date: date) -> dict:
    with Session(engine) as db:
        stmt = select(BetTicket).where(BetTicket.race_date <= up_to_date)
        tickets = list(db.scalars(stmt).all())
    summary = _summarize(tickets)
    summary["up_to_date"] = str(up_to_date)
    return summary


def format_report(summary: dict, title: str) -> str:
    lines = [f"[{title}]"]
    lines.append(f"taisho race su: {summary['race_count']}R / tekichu: {summary['hit_race_count']}R")
    lines.append(f"tekichuritsu: {summary['hit_rate']}%")
    lines.append(f"kounyu: {summary['total_stake']:,}en / haraimodoshi: {summary['total_payout']:,}en")
    profit_sign = "+" if summary["profit"] >= 0 else ""
    lines.append(f"shuushi: {profit_sign}{summary['profit']:,}en / kaishuritsu: {summary['recovery_rate']}%")
    return "\n".join(lines)
