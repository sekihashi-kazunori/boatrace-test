"""
ボートレース公式サイトからデータを取得するモジュール
"""
import re
from datetime import date
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.boatrace.jp"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_today_stadiums(target_date: Optional[date] = None) -> list[dict]:
    """
    指定日(省略時は今日)に開催中のボートレース場一覧を取得する。

    Returns:
        [{"stadium_code": "01", "stadium_name": "桐生", "race_count": 12}, ...]
    """
    if target_date is None:
        target_date = date.today()
    hd = target_date.strftime("%Y%m%d")

    url = f"{BASE_URL}/owpc/pc/race/index?hd={hd}"
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")
    stadiums = []

    # 開催場一覧のテーブル行をパース
    for link in soup.select("a[href*='raceindex'][href*='jcd=']"):

        href = link.get("href", "")
        m = re.search(r"jcd=(\d+)", href)
        if not m:
            continue
        stadium_code = m.group(1)
        stadium_name = link.get_text(strip=True)
        if not stadium_name:
            continue

        stadiums.append({
            "stadium_code": stadium_code,
            "stadium_name": stadium_name,
        })

    # 重複除去(同じ場が複数リンクに出ることがあるため)
    seen = set()
    unique_stadiums = []
    for s in stadiums:
        if s["stadium_code"] not in seen:
            seen.add(s["stadium_code"])
            unique_stadiums.append(s)

    return unique_stadiums


def fetch_race_card(stadium_code: str, race_number: int, target_date: Optional[date] = None) -> dict:
    """
    指定場・指定レースの出走表を取得する。

    Args:
        stadium_code: 場コード(例 "01" = 桐生)
        race_number: レース番号(1〜12)
        target_date: 対象日(省略時は今日)

    Returns:
        {
            "stadium_code": "01",
            "race_number": 1,
            "racers": [
                {"lane": 1, "name": "選手名", "number": "1234", ...},
                ...
            ]
        }
    """
    if target_date is None:
        target_date = date.today()
    hd = target_date.strftime("%Y%m%d")

    url = (
        f"{BASE_URL}/owpc/pc/race/racelist"
        f"?rno={race_number}&jcd={stadium_code}&hd={hd}"
    )
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")
    print(f"取得url={url} タイトル={soup.title.get_text() if soup.title else 'なし'} 本文長={len(res.text)}")
    print("is-fs12 の有無:", "is-fs12" in res.text)
    idx = res.text.find("is-fs12")
    print(res.text[idx-200:idx+2000] if idx != -1 else "racerという文字列が見つかりません")


    racers = []

            # 出走表テーブルの各艇(1〜6号艇)をパース
        for row in soup.select("tbody.is-fs12 tr"):
            cells = row.find_all("td")

            boat_color_cell = next(
                (c for c in cells if any(
                    cls.startswith("is-boatColor") for cls in c.get("class", [])
                )),
                None
            )
            if boat_color_cell is None:
                continue

            if len(cells) < 2:
                continue
            try:
                lane = int(boat_color_cell.get_text(strip=True))
            except ValueError:
                continue

            name_tag = cells[1].find("a")
            name = name_tag.get_text(strip=True) if name_tag else None


        number_tag = cells[1].find("div", class_="is-fs11")
        racer_number = None
        if number_tag:
            m = re.search(r"\d{4}", number_tag.get_text())
            if m:
                    lineh2 = row.find_all("td", class_="is-lineH2")
                    national_win_rate = None
                    local_win_rate = None
                    motor_2rate = None
                    boat_2rate = None
                    if len(lineh2) >= 5:
                        try:
                            national_win_rate = float(lineh2[1].get_text(separator="|").split("|")[0])
                        except (ValueError, IndexError):
                            pass
                        try:
                            local_win_rate = float(lineh2[2].get_text(separator="|").split("|")[0])
                        except (ValueError, IndexError):
                            pass
                        try:
                            motor_2rate = float(lineh2[3].get_text(separator="|").split("|")[1])
                        except (ValueError, IndexError):
                            pass
                        try:
                            boat_2rate = float(lineh2[4].get_text(separator="|").split("|")[1])
                        except (ValueError, IndexError):
                            pass

                    racers.append({
                        "lane": lane,
                        "name": name,
                        "number": racer_number,
                        "national_win_rate": national_win_rate,
                        "local_win_rate": local_win_rate,
                        "motor_2rate": motor_2rate,
                        "boat_2rate": boat_2rate,
                    })





    return {
        "stadium_code": stadium_code,
        "race_number": race_number,
        "date": hd,
        "racers": racers,
    }
