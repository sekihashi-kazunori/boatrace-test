from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import StringIO

import requests
from metaboatrace.models.race import BettingMethod
from metaboatrace.models.stadium import StadiumTelCode

from metaboatrace.scrapers.official.website.v1707.pages.race.result_page import (
    location as result_page_location,
)
from metaboatrace.scrapers.official.website.v1707.pages.race.result_page import (
    scraping as result_page_scraping,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


@dataclass
class RaceResultData:
    race_date: date
    stadium_code: int
    race_number: int
    finish_order: str
    trifecta_payout: int


def fetch_race_result(race_date: date, stadium_code: int | str, race_number: int) -> RaceResultData:
    """ボートレース公式サイトからレース結果(三連単の着順・払戻金)を取得する。

    metaboatrace.scrapers ライブラリは「URL生成(location)」と「HTML解析(scraping)」が
    分離した設計になっており、HTTPでの取得自体は呼び出し側(ここ)の責務。
    scraping側の関数はHTMLの中身(file-like)を受け取る点に注意。
    """
    stadium_tel_code = StadiumTelCode(int(stadium_code))

    url = result_page_location.create_race_result_page_url(
        race_date, stadium_tel_code, race_number
    )

    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = res.apparent_encoding
    html = res.text

    payoffs = result_page_scraping.extract_race_payoffs(StringIO(html))

    trifecta_payoff = next(
        (p for p in payoffs if p.betting_method == BettingMethod.TRIFECTA), None
    )
    if trifecta_payoff is None:
        raise ValueError(f"3連単の払戻データが見つかりません: {stadium_code=} {race_number=}")

    finish_order = "-".join(str(n) for n in trifecta_payoff.betting_numbers)
    trifecta_payout = trifecta_payoff.amount

    return RaceResultData(
        race_date=race_date,
        stadium_code=int(stadium_code),
        race_number=race_number,
        finish_order=finish_order,
        trifecta_payout=trifecta_payout,
    )


def settle_tickets(tickets: list, result: RaceResultData) -> None:
    for ticket in tickets:
        if ticket.combination == result.finish_order:
            ticket.result = "hit"
            ticket.payout = int(result.trifecta_payout * (ticket.amount / 100))
        else:
            ticket.result = "miss"
            ticket.payout = 0
