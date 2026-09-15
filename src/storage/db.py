from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


# ============================================================
# ここから下のコードを src/storage/db.py に追加してください。
# 追加場所: class BetTicket の直前(9行目の上あたり)がおすすめです。
# ============================================================


class RaceResult(Base):
    __tablename__ = "race_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    race_date = Column(String)
    stadium_code = Column(String)
    race_number = Column(Integer)
    finish_order = Column(String)
    trifecta_payout = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
    
class RaceEntry(Base):
    __tablename__ = "race_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    race_date = Column(String)
    stadium_code = Column(String)
    race_number = Column(Integer)
    lane_number = Column(Integer)
    racer_id = Column(String, nullable=True)
    national_win_rate = Column(Float, nullable=True)
    local_win_rate = Column(Float, nullable=True)
    motor_2rate = Column(Float, nullable=True)
    boat_2rate = Column(Float, nullable=True)
    exhibition_time = Column(Float, nullable=True)
    tilt = Column(Float, nullable=True)
    start_timing = Column(Float, nullable=True)
    win_odds = Column(Float, nullable=True)
    finish_position = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ============================================================
# 既存の save_entries 関数を、以下の内容で置き換えてください。
# (今は print するだけで実際にDB保存していないため)
# ============================================================


def save_entries(engine, entries: list) -> None:
    # entries は src/pipeline/session_pipeline.py の to_race_entries() が返す
    # RaceEntry オブジェクトのリストがそのまま渡ってくる想定。
    session = get_session(engine)
    try:
        for entry in entries:
            entry.race_date = str(entry.race_date)
            session.add(entry)
        session.commit()
        print(f"[save_entries] {len(entries)} entries saved")
    finally:
        session.close()

class BetTicket(Base):
    __tablename__ = "bet_tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session = Column(String)
    stadium_code = Column(String)
    race_number = Column(Integer)
    bet_type = Column(String)
    combination = Column(String)
    amount = Column(Integer)
    odds = Column(Float, nullable=True)
    result = Column(String, nullable=True)
    payout = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def get_engine(db_path: str = "boatrace.db"):
    return create_engine(f"sqlite:///{db_path}")


def init_db(engine):
    Base.metadata.create_all(engine)


def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()
def save_bet_tickets(engine, tickets: list[dict]):
    session = get_session(engine)
    try:
        for t in tickets:
            ticket = BetTicket(
                session=t.get("session"),
                stadium_code=t.get("stadium_code"),
                race_number=t.get("race_number"),
                bet_type=t.get("bet_type"),
                combination=t.get("combination"),
                amount=t.get("amount"),
                odds=t.get("odds"),
            )
            session.add(ticket)
        session.commit()
    finally:
        session.close()

def save_race_result(engine, result) -> None:
    session = get_session(engine)
    try:
        row = RaceResult(
            race_date=str(result.race_date),
            stadium_code=str(result.stadium_code),
            race_number=result.race_number,
            finish_order=result.finish_order,
            trifecta_payout=result.trifecta_payout,
        )
        session.add(row)
        session.commit()
    finally:
        session.close()
