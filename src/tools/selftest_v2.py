"""本番と同じ予想経路(v2モデル→買い目)を過去レースで動かす動作確認。Discordには送らない。"""
import sqlite3

import pandas as pd

from src.models.predict import predict_win_probabilities, add_original_index
from src.betting.allocation import build_bet_plan, _harville3

c = sqlite3.connect("boatrace.db")
lines = []
races = pd.read_sql("select distinct stadium_code, race_number from race_entries where race_date='2026-09-18' limit 3", c)
for st, rn in races.values:
    df = pd.read_sql(f"select * from race_entries where race_date='2026-09-18' and stadium_code='{st}' and race_number={rn}", c)
    df = df.rename(columns={"local_win_rate": "course_base_win_rate"})
    df["exhibition_rank"] = df["exhibition_time"].rank()
    out = add_original_index(predict_win_probabilities(None, df))
    cp = _harville3(out)
    odds = {k: round(0.75 / v, 1) for k, v in cp.items()}
    plans = build_bet_plan(out, odds_map=odds)
    lines.append(f"{st} {rn}R p1={out.sort_values('lane_number')['p1'].round(3).tolist()} "
                 + " / ".join(f"{p.category}{p.combination} {p.stake}円" for p in plans))
open("selftest_v2.txt", "w").write("\n".join(lines) + "\n")
print("\n".join(lines))
