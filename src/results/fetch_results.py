from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from metaboatrace.scrapers.official.website.v1707.pages.race import result_page as race_result_page


@dataclass
class RaceResultData:
    race_date: date
    stadium_code: int
    race_number: int
    finish_order: str
    trifecta_payout: int


def fetch_race_result(race_date: date, stadium_code: int, race_number: int) -> RaceResultData:
    url = race_result_page.location.create_race_result_page_url(race_date, stadium_code, race_number)
    result = race_result_page.scraping.extract(url)

    finish_order = "-".join(str(lane) for lane in result.trifecta_finish_lanes)
    trifecta_payout = result.trifecta_payout

    return RaceResultData(
        race_date=race_date,
        stadium_code=stadium_code,
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



    


