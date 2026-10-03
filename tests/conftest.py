"""Pytest fixtures and configuration."""
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from src.app.main import app
from src.infrastructure.db.models import Course, GradeScale, Semester, Subject, User, UserCourse
from src.presentation.api.deps import get_session


@pytest.fixture(name="db_engine")
def db_engine_fixture():
    """Create an isolated in-memory SQLite engine with StaticPool."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture(name="session")
def session_fixture(db_engine):
    """Yield a database session for test operations."""
    with Session(db_engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(db_engine):
    """FastAPI TestClient with overridden get_session dependency."""
    def override_get_session():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(name="sample_data")
def sample_data_fixture(session):
    """Seed baseline User, GradeScale, Course, Semester, Subject, and UserCourse."""
    test_user_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    user = User(
        id=test_user_id,
        username="testuser",
        email="test@example.com",
    )
    session.add(user)

    scale = GradeScale(
        id=1,
        scale_name="Standard",
        grade="HD",
        label="High Distinction",
        min_mark=85.0,
        max_mark=100.0,
        gpa_point=7.0,
        band_type="both",
    )
    session.add(scale)
    session.commit()

    course = Course(
        id=1,
        code="CS101",
        name="Computer Science",
        grading_scale_id=scale.id,
    )
    session.add(course)
    session.commit()

    user_course = UserCourse(
        user_id=test_user_id,
        course_id=course.id,
        is_default=True,
    )
    session.add(user_course)
    session.commit()

    semester = Semester(
        id=1,
        name="Semester 1",
        year=2026,
        course_id=course.id,
    )
    session.add(semester)
    session.commit()

    subject = Subject(
        id=1,
        subject_code="COMP1001",
        subject_name="Intro to Computer Science",
        semester_id=semester.id,
        credit_points=6,
        has_exam=False,
    )
    session.add(subject)
    session.commit()

    return {
        "user": user,
        "scale": scale,
        "course": course,
        "semester": semester,
        "subject": subject,
        "user_course": user_course,
    }
