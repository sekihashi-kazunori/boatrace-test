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


def race_review(engine, target_date: date, session_name: str) -> str | None:
    """セッションの各レースの振り返り(結果の3連単と、的中なら払戻額)。

    例:
      丸亀1R 136 ハズレ
      浜名湖3R 165 的中🎯 12,600円
    """
    import json
    import os
    from src.pipeline.session_pipeline import STADIUM_NAMES, plan_path

    with OrmSession(engine) as db:
        tickets = list(db.scalars(select(BetTicket).where(
            BetTicket.race_date == str(target_date), BetTicket.session == session_name)).all())
        results = {
            (str(r.stadium_code).zfill(2), r.race_number): r.finish_order
            for r in db.scalars(select(RaceResult).where(RaceResult.race_date == str(target_date)))
        }
    if not tickets:
        return None

    races: dict = {}
    for t in tickets:
        races.setdefault((str(t.stadium_code).zfill(2), t.race_number), []).append(t)

    deadlines = {}
    path = plan_path(target_date, session_name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for r in json.load(f):
                deadlines[(r["stadium_code"], r["race_number"])] = r.get("deadline") or ""

    lines = ["【レース振り返り】"]
    for key in sorted(races, key=lambda k: (deadlines.get(k, "99:99"), k)):
        ts = races[key]
        name = f"{STADIUM_NAMES.get(key[0], key[0])}{key[1]}R"
        order = (results.get(key) or "").replace("-", "")
        if any(t.result is None for t in ts):
            lines.append(f"{name} 結果待ち")
        elif any(t.result == "hit" for t in ts):
            payout = sum(t.payout or 0 for t in ts)
            lines.append(f"{name} {order} 的中🎯 {payout:,}円")
        else:
            lines.append(f"{name} {order} ハズレ")
    return "\n".join(lines)


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
    review = race_review(engine, target_date, session_name)
    report = format_report(summary, f"{session_name_ja} 収支報告")
    if review:
        report += "\n" + review
    notify_console(report)
    notify_discord(report)

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
