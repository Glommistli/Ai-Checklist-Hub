import os
os.environ["DATABASE_URL"] = "sqlite:///./data/test.db"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base, get_db
from app.main import app

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=__import__("sqlalchemy").pool.StaticPool)
TestingSession = sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def database():
    Base.metadata.create_all(engine)
    with TestingSession() as db:
        from app.models import Checklist
        db.add(Checklist(name="Inbox")); db.commit()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    with TestingSession() as session: yield session


@pytest.fixture
def client():
    def override():
        with TestingSession() as session: yield session
    app.dependency_overrides[get_db] = override
    with TestClient(app) as c: yield c
    app.dependency_overrides.clear()

