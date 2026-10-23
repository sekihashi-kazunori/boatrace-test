from __future__ import annotations

import argparse
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from src.storage.db import get_state_engine, BetTicket, save_race_result, RaceResult
from src.results.fetch_results import fetch_race_result, settle_tickets
from src.reporting.report import session_report, daily_report, session_breakdown, cumulative_report, format_report, CUMULATIVE_START, experiment_report, format_experiment
from src.notify.notifier import notify_console, notify_discord


KINDS = (("rule", "江戸川・津"), ("main", "本予想"))


def race_review(engine, target_date: date, session_name: str, kind: str = "rule") -> str | None:
    rows = race_rows(engine, target_date, session_name, kind)
    if not rows:
        return None
    lines = ["【レース振り返り】"]
    for r in rows:
        if r["status"] == "wait":
            lines.append(f"{r['name']} 結果待ち")
        elif r["status"] == "hit":
            lines.append(f"{r['name']} {r['order']} 的中🎯 {r['payout']:,}円")
        else:
            lines.append(f"{r['name']} {r['order']} ハズレ")
    return "\n".join(lines)


def race_rows(engine, target_date: date, session_name: str, kind: str = "rule") -> list[dict]:
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
        return []

    from src.reporting.report import _main_only
    tickets = _main_only(tickets, kind)
    if not tickets:
        return []
    races: dict = {}
    for t in tickets:
        races.setdefault((str(t.stadium_code).zfill(2), t.race_number), []).append(t)

    deadlines = {}
    for path in (plan_path(target_date, session_name), os.path.join("plans", f"{target_date}_rules.json")):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for r in json.load(f):
                    deadlines.setdefault((r["stadium_code"], r["race_number"]), r.get("deadline") or "")

    out = []
    for key in sorted(races, key=lambda k: (deadlines.get(k, "99:99"), k)):
        ts = races[key]
        status = "wait" if any(t.result is None for t in ts) else (
            "hit" if any(t.result == "hit" for t in ts) else "miss")
        out.append({"name": f"{STADIUM_NAMES.get(key[0], key[0])}{key[1]}R",
                     "order": (results.get(key) or "").replace("-", ""),
                     "stake": sum(t.amount for t in ts),
                     "payout": sum(t.payout or 0 for t in ts), "status": status,
                     # 2026-10-22: 当たった買い目の100円あたり払戻(30倍以上なら大穴として目立たせる)
                     "hit_per100": max((int((t.payout or 0) * 100 / t.amount) for t in ts
                                        if t.result == "hit" and t.amount), default=0)})
    return out


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

    # 2026-10-14 ご主人様指示: セッションごとの報告は本予想だけ。
    # 江戸川・津と検証枠は、ナイター後の最終収支報告でまとめて出す。
    # 2026-10-17 ご主人様指示: 見やすく・分析しやすい表示に作り直し
    from src.reporting.discord_format import session_message
    summary = session_report(engine, target_date, session_name, "main")
    report = session_message(session_name_ja, f"{target_date.month}/{target_date.day}", summary,
                             race_rows(engine, target_date, session_name, "main"))
    notify_console(report)
    notify_discord(report)

    if is_last_session_of_day:
        # 2026-10-17 ご主人様指示: 最終振り返りはモーニング・デイ・ナイター・合計の収支を出す
        msgs = [session_breakdown(engine, target_date, "main")]  # 当日・累積・日別推移を1通で
        msgs.append(verify_summary(engine, target_date))
        # 2026-10-22 ご主人様指示: 予想屋さん(スクショから書き起こした分)も今日の収支に出す
        try:
            from src.reporting.tipsters import tipster_summary
            t = tipster_summary(target_date)
            if t:
                msgs.append(t)
        except Exception as e:
            print(f"予想屋さんの採点失敗: {e}")
        for msg in msgs:
            notify_console(msg)
            notify_discord(msg)
            time.sleep(1.2)


# 本予想への昇格ライン(2026-10-14 ご主人様と決定): 10/14以降で 30R以上・的中3R以上・回収率115%以上
PROMOTE_SINCE = date(2026, 10, 14)
PROMOTE_MIN_RACES, PROMOTE_MIN_HITS, PROMOTE_MIN_RATE = 30, 3, 115.0


def verify_summary(engine, target_date: date) -> str:
    """検証枠(紙上)の10/14以降の累積を1通にまとめ、昇格ラインを満たした枠に印を付ける。"""
    from src.pipeline.final_pipeline import VERIFY_RULES
    from src.reporting.report import cumulative_report as _cum
    from src.reporting.discord_format import verify_message
    S = PROMOTE_SINCE

    # 2026-10-20 ご主人様指示: 本日分と累積を並べて出す
    def item(title, *args):
        r = experiment_report(engine, target_date, *args, since=S)
        today = experiment_report(engine, target_date, *args, since=target_date)
        ok = (r["races"] >= PROMOTE_MIN_RACES and r["hit_races"] >= PROMOTE_MIN_HITS
              and r["recovery_rate"] >= PROMOTE_MIN_RATE)
        return (title, r, ok, today)

    items = [
        item("江戸川 B級1〜4R 1-456-456", "edo", 4, "B"),
        # 津の2枠は 2026-10-17 停止(万舟頼みで実力なし)
    ]
    for bt, *_rest, title in VERIFY_RULES:
        items.append(item(title, bt))
    from src.pipeline.final_pipeline import WIND_RULES, WEATHER_RULES
    from src.pipeline.final_pipeline import MANSHU_RULES
    from src.pipeline.final_pipeline import OUTER_RULES
    for bt, title in MANSHU_RULES + OUTER_RULES:
        items.append(item(title, bt))
    for bt, _st, _w, _c, title in WIND_RULES + WEATHER_RULES:
        items.append(item(title, bt))
    items.append(item("実験枠(モデル期待値)", "exp"))
    rule = _cum(engine, target_date, "rule")
    extra = (f"(参考)江戸川・津 旧本番ルール: {rule['race_count']}R 的中{rule['hit_race_count']}R "
             f"回収率{rule['recovery_rate']:.0f}%")
    return verify_message(f"{S.month}/{S.day}", f"{target_date.month}/{target_date.day}",
                          f"昇格ライン: {PROMOTE_MIN_RACES}R以上・的中{PROMOTE_MIN_HITS}R以上・回収率{PROMOTE_MIN_RATE:.0f}%以上",
                          items, extra)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["morning", "day", "nighter"], required=True)
    parser.add_argument("--last-of-day", action="store_true")
    parser.add_argument("--date", default=None, help="精算する日(YYYY-MM-DD)。省略時は日本時間の今日")
    args = parser.parse_args()
    run_settle(args.session, target_date=date.fromisoformat(args.date) if args.date else None,
               is_last_session_of_day=args.last_of_day)
