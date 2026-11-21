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
    # 2026-11-21夜: レースごとの収支を出すと長くなるので、Discordの2,000字制限を超えたら行の区切りで分けて送る
    for part in _split(message):
        resp = requests.post(webhook_url, json={"content": part})
        print(f"Discord response: {resp.status_code} {resp.text}")


def _split(message: str, limit: int = 1900) -> list[str]:
    if len(message) <= limit:
        return [message]
    parts, cur, in_code = [], "", False
    for ln in message.split("\n"):
        add = ln + "\n"
        if len(cur) + len(add) + 4 > limit and cur:
            parts.append(cur + ("```" if in_code else ""))
            cur = "```\n" if in_code else ""
        cur += add
        if ln.strip().startswith("```"):
            in_code = not in_code
    if cur.strip():
        parts.append(cur)
    return parts

