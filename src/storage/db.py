from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


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


def save_entries(engine, entries: list[dict]):
    # 出走表データを保存する(現時点ではログのみ、将来的にモデル追加予定)
    print(f"[save_entries] {len(entries)} entries received")


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