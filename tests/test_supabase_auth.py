"""Tests for Supabase Auth (GoTrue) integration and JWT validation."""
from __future__ import annotations

import time
import uuid
import jwt
import pytest

from src.core.auth import ensure_uuid, verify_supabase_token
from src.infrastructure.db.models import Course, UserCourse


TEST_JWT_SECRET = "supabase-test-jwt-secret-that-is-at-least-32-bytes-long!"


def create_test_token(
    user_id: uuid.UUID,
    email: str = "test@supabase.io",
    username: str = "supatester",
    secret: str = TEST_JWT_SECRET,
    expired: bool = False,
    extra_claims: dict | None = None,
) -> str:
    """Helper to generate mock Supabase GoTrue JWT tokens."""
    exp = int(time.time()) - 3600 if expired else int(time.time()) + 3600
    claims = {
        "sub": str(user_id),
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": exp,
        "iat": int(time.time()),
        "user_metadata": {"username": username},
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, secret, algorithm="HS256")


def test_ensure_uuid():
    """ensure_uuid correctly parses valid UUID strings and objects, and rejects invalid ones."""
    uid = uuid.uuid4()
    assert ensure_uuid(uid) == uid
    assert ensure_uuid(str(uid)) == uid

    with pytest.raises(ValueError):
        ensure_uuid("invalid-uuid-string")

    with pytest.raises(ValueError):
        ensure_uuid(None)


def test_verify_supabase_token_valid(monkeypatch):
    """verify_supabase_token decodes and validates a valid token signed with SUPABASE_JWT_SECRET."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", TEST_JWT_SECRET)
    user_id = uuid.uuid4()
    token = create_test_token(user_id, email="alice@test.com", username="alice")

    payload = verify_supabase_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["email"] == "alice@test.com"
    assert payload["user_metadata"]["username"] == "alice"


def test_verify_supabase_token_expired(monkeypatch):
    """Expired token raises 401 HTTPException."""
    from fastapi import HTTPException
    monkeypatch.setenv("SUPABASE_JWT_SECRET", TEST_JWT_SECRET)
    user_id = uuid.uuid4()
    token = create_test_token(user_id, expired=True)

    with pytest.raises(HTTPException) as exc_info:
        verify_supabase_token(token)
    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


def test_verify_supabase_token_invalid_signature(monkeypatch):
    """Token signed with wrong secret raises 401 HTTPException."""
    from fastapi import HTTPException
    monkeypatch.setenv("SUPABASE_JWT_SECRET", TEST_JWT_SECRET)
    user_id = uuid.uuid4()
    token = create_test_token(user_id, secret="wrong-secret-key-that-does-not-match!!")

    with pytest.raises(HTTPException) as exc_info:
        verify_supabase_token(token)
    assert exc_info.value.status_code == 401


def test_user_courses_endpoints_require_auth(client):
    """Accessing /api/v1/user-courses without auth returns 401."""
    resp = client.get("/api/v1/user-courses/")
    assert resp.status_code == 401


def test_user_courses_endpoints_with_bearer_token(client, session, sample_data, monkeypatch):
    """Accessing /api/v1/user-courses with valid Supabase Bearer token manages enrollments."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", TEST_JWT_SECRET)
    user_id = uuid.uuid4()
    token = create_test_token(user_id)
    auth_headers = {"Authorization": f"Bearer {token}"}

    course_id = sample_data["course"].id

    # 1. Initially user has no courses
    res_list = client.get("/api/v1/user-courses/", headers=auth_headers)
    assert res_list.status_code == 200
    assert res_list.json() == []

    # 2. Enroll user in course CS101
    res_add = client.post(
        "/api/v1/user-courses/",
        json={"course_id": course_id, "is_default": True},
        headers=auth_headers,
    )
    assert res_add.status_code == 201
    data = res_add.json()
    assert data["course_id"] == course_id
    assert data["user_id"] == str(user_id)
    assert data["is_default"] is True
    assert data["course_code"] == "CS101"

    # 3. Create a second course to test switching default and multiple courses
    scale_id = sample_data["scale"].id
    course2 = Course(
        code="MATH200",
        name="Advanced Mathematics",
        grading_scale_id=scale_id,
    )
    session.add(course2)
    session.commit()

    res_add2 = client.post(
        "/api/v1/user-courses/",
        json={"course_id": course2.id, "is_default": False},
        headers=auth_headers,
    )
    assert res_add2.status_code == 201

    # 4. List user courses
    res_list2 = client.get("/api/v1/user-courses/", headers=auth_headers)
    assert res_list2.status_code == 200
    enrolled = res_list2.json()
    assert len(enrolled) == 2

    # 5. Switch default course to course2
    res_default = client.patch(
        f"/api/v1/user-courses/{course2.id}/default",
        headers=auth_headers,
    )
    assert res_default.status_code == 200
    assert res_default.json()["is_default"] is True

    # Verify course 1 is no longer default
    res_list3 = client.get("/api/v1/user-courses/", headers=auth_headers)
    c1 = next(c for c in res_list3.json() if c["course_id"] == course_id)
    c2 = next(c for c in res_list3.json() if c["course_id"] == course2.id)
    assert c1["is_default"] is False
    assert c2["is_default"] is True

    # 6. Delete enrollment
    res_del = client.delete(
        f"/api/v1/user-courses/{course2.id}",
        headers=auth_headers,
    )
    assert res_del.status_code == 204

    # Course 1 should automatically be restored as default
    res_list4 = client.get("/api/v1/user-courses/", headers=auth_headers)
    assert len(res_list4.json()) == 1
    assert res_list4.json()[0]["course_id"] == course_id
    assert res_list4.json()[0]["is_default"] is True


def test_subjects_endpoint_with_user_filtering(client, session, sample_data, monkeypatch):
    """Subjects endpoint filters by user's enrolled courses when user_only is set."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", TEST_JWT_SECRET)
    user_id = uuid.uuid4()
    token = create_test_token(user_id)
    auth_headers = {"Authorization": f"Bearer {token}"}

    # User is not enrolled in CS101, so user_only query returns empty
    res = client.get("/api/v1/subjects/?user_only=true", headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == []

    # Enroll user in CS101
    uc = UserCourse(user_id=user_id, course_id=sample_data["course"].id, is_default=True)
    session.add(uc)
    session.commit()

    # Now subjects belonging to CS101 appear
    res2 = client.get("/api/v1/subjects/?user_only=true", headers=auth_headers)
    assert res2.status_code == 200
    subjects = res2.json()
    assert len(subjects) == 1
    assert subjects[0]["subject_code"] == "COMP1001"


def test_sync_session_route(client, monkeypatch):
    """POST /auth/session accepts validated Supabase client access token and sets session."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", TEST_JWT_SECRET)
    user_id = uuid.uuid4()
    token = create_test_token(user_id, email="sync@example.com", username="syncuser")

    res = client.post(
        "/auth/session",
        json={"access_token": token},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["user_id"] == str(user_id)


def test_legacy_custom_auth_routes_removed(client):
    """Verify that legacy custom auth POST routes (/login, /signup, /profile/change-password) are removed."""
    # POST /login without Supabase client returns 405 Method Not Allowed
    res_login = client.post("/login", data={"username": "user", "password": "pw"})
    assert res_login.status_code == 405

    # POST /signup without Supabase client returns 405 Method Not Allowed
    res_signup = client.post("/signup", data={"username": "user", "password": "pw"})
    assert res_signup.status_code == 405

    # POST /profile/change-password returns 404 or 405
    res_change_pw = client.post("/profile/change-password", data={"current_password": "pw", "new_password": "pw2"})
    assert res_change_pw.status_code in (404, 405)
