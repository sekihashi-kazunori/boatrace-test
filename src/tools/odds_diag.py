"""出走表から級別(A1/A2/B1/B2)が取れるかの診断。結果を odds_diag.txt に書く。"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.collectors.official_site import fetch_race_card, fetch_before_info, LAST_WEATHER

out = []
today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
for d, st, rn in ((today, "03", 1), (today, "02", 1), (date(2026, 10, 11), "23", 5)):
    try:
        card = fetch_race_card(st, rn, d)
        out.append(f"{d} {st} {rn}R " + " ".join(f"{r['lane']}:{r.get('racer_class')}:{r.get('national_win_rate')}" for r in card["racers"]))
    except Exception as e:
        out.append(f"{d} {st} {rn}R ERROR {e!r}")
for d, st, rn in ((date(2026, 10, 11), "23", 5), (date(2026, 10, 11), "09", 4)):
    try:
        fetch_before_info(st, rn, d)
        out.append(f"weather {d} {st} {rn}R {LAST_WEATHER.get((st, rn, str(d)))}")
    except Exception as e:
        out.append(f"weather {d} {st} {rn}R ERROR {e!r}")
open("odds_diag.txt", "w").write("\n".join(out) + "\n")
print("\n".join(out))
