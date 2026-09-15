from __future__ import annotations

import argparse
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from src.storage.db import get_engine, BetTicket, save_race_result, RaceResult
from src.results.fetch_results import fetch_race_result, settle_tickets
from src.reporting.report import session_report, daily_report, cumulative_report, format_report
from src.notify.notifier import notify_console, notify_discord


def run_settle(session_name: str, target_date: date | None = None, db_path: str = "boatrace.db", is_last_session_of_day: bool = False) -> None:
    target_date = target_date or date.today()
    engine = get_engine(db_path)

    with OrmSession(engine) as db:
        stmt = select(BetTicket).where(
            BetTicket.race_date == target_date,
            BetTicket.session == session_name,
            BetTicket.result.is_(None),

        )
        pending_tickets = list(db.scalars(stmt).all())

        races = {}
        for t in pending_tickets:
            races.setdefault((t.stadium_code, t.race_number), []).append(t)

        for (stadium_code, race_number), tickets in races.items():
            try:
                result = fetch_race_result(target_date, stadium_code, race_number)
            except Exception as e:
                print(f"kekka shutoku shippai {stadium_code=} {race_number=}: {e}")
                continue
            settle_tickets(tickets, result)
            save_race_result(engine, RaceResult(
                race_date=target_date,
                stadium_code=stadium_code,
                race_number=race_number,
                finish_order=result.finish_order,
                trifecta_payout=result.trifecta_payout,
            ))
        db.commit()

    summary = session_report(engine, target_date, session_name)
    notify_console(format_report(summary, f"{session_name} shuushi houkoku"))
    notify_discord(format_report(summary, f"{session_name} shuushi houkoku"))

    if is_last_session_of_day:
        daily = daily_report(engine, target_date)
        notify_console(format_report(daily, "honjitsu souzuushi"))
        notify_discord(format_report(daily, "honjitsu souzuushi"))

        cumulative = cumulative_report(engine, target_date)
        notify_console(format_report(cumulative, "ruiseki shuushi"))
        notify_discord(format_report(cumulative, "ruiseki shuushi"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["morning", "day", "nighter"], required=True)
    parser.add_argument("--last-of-day", action="store_true")
    args = parser.parse_args()
    run_settle(args.session, is_last_session_of_day=args.last_of_day)
