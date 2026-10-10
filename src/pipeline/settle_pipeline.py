from __future__ import annotations

import argparse
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from src.storage.db import get_state_engine, BetTicket, save_race_result, RaceResult
from src.results.fetch_results import fetch_race_result, settle_tickets
from src.reporting.report import session_report, daily_report, cumulative_report, format_report, CUMULATIVE_START
from src.notify.notifier import notify_console, notify_discord


def run_settle(session_name: str, target_date: date | None = None, is_last_session_of_day: bool = False) -> None:
    # date.today()はサーバー(GitHub Actions)のUTC時刻を使ってしまい、
    # 日本時間とズレて前日/翌日の日付になることがあるため、
    # 明示的に日本時間(JST)の「今日」を使う。
    target_date = target_date or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    engine = get_state_engine()

    session_name_ja = {"morning": "モーニング", "day": "デイ", "nighter": "ナイター"}.get(session_name, session_name)

    with OrmSession(engine) as db:
        # セッションに関係なく、その日の未確定の買い目をすべて精算する
        # (展示後予想にしたことで、セッションの精算時刻より後に締切の
        # レースもあるため。取りこぼしは次の精算で拾われる)
        stmt = select(BetTicket).where(
            BetTicket.race_date == str(target_date),
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
                print(f"結果取得失敗 {stadium_code=} {race_number=}: {e}")
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
    notify_console(format_report(summary, f"{session_name_ja} 収支報告"))
    notify_discord(format_report(summary, f"{session_name_ja} 収支報告"))

    if is_last_session_of_day:
        daily = daily_report(engine, target_date)
        notify_console(format_report(daily, "本日総収支"))
        notify_discord(format_report(daily, "本日総収支"))

        if target_date < CUMULATIVE_START:
            msg = f"【累積収支】{CUMULATIVE_START.month}/{CUMULATIVE_START.day}から新システムで集計開始します"
        else:
            msg = format_report(cumulative_report(engine, target_date), "累積収支")
        notify_console(msg)
        notify_discord(msg)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["morning", "day", "nighter"], required=True)
    parser.add_argument("--last-of-day", action="store_true")
    args = parser.parse_args()
    run_settle(args.session, is_last_session_of_day=args.last_of_day)
