from __future__ import annotations

import os
import pandas as pd
import requests


def format_race_prediction(race_df: pd.DataFrame, stadium_name: str, race_number: int) -> str:
    lines = [f"[{stadium_name} {race_number}R yosou]"]
    for _, row in race_df.iterrows():
        lines.append(
            f"{int(row['lane_number'])}gouTei: yosoku shoritsu {row['predicted_win_prob']*100:.1f}% "
            f"/ shisuu {row['original_index']:.1f}"
            + (" star kai" if row["original_index"] >= 120 else "")
        )
    return "\n".join(lines)


def notify_console(message: str) -> None:
    print(message)


def notify_discord(message: str) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return
    requests.post(webhook_url, json={"content": message})
