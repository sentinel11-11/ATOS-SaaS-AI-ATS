from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ats.config import settings


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def session_factory():
    return sessionmaker(engine(), expire_on_commit=False)


def session():
    with session_factory()() as db:
        yield db
