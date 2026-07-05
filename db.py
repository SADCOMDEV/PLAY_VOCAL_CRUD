from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

engine: Engine | None = None

def init_engine(database_url: str) -> Engine:
    global engine
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    return engine

def get_engine() -> Engine:
    if engine is None:
        raise RuntimeError("Engine not initialized")
    return engine

def fetch_one(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).fetchone()

def fetch_all(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).fetchall()

def execute(sql: str, params: dict | None = None):
    with get_engine().begin() as conn:
        return conn.execute(text(sql), params or {})