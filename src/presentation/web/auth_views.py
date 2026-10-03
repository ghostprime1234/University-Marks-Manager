"""Authentication routes and course management using Supabase Auth (GoTrue)."""
from __future__ import annotations

import os
import uuid
from typing import Any, List, Optional, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import selectinload
from sqlmodel import Session, col, func, select

from src.core.auth import ensure_uuid, verify_supabase_token
from src.core.services.course_manager import CourseManager
from src.infrastructure.db.models import Course, GradeScale, University, UserCourse
from src.presentation.api.deps import get_session
from src.presentation.web.template_helpers import _render

router = APIRouter()


class SessionSyncPayload(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None


@router.api_route("/login", methods=["GET", "HEAD"], response_class=HTMLResponse)
def login_page(request: Request):
    """Render the login page powered by Supabase client SDK."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)

    error = request.query_params.get("error")
    return _render(request, "login.html", {
        "error": error,
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_anon_key": os.getenv("SUPABASE_ANON_KEY", ""),
    })


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    """Render the registration page powered by Supabase client SDK."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)

    error = request.query_params.get("error")
    return _render(request, "signup.html", {
        "error": error,
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_anon_key": os.getenv("SUPABASE_ANON_KEY", ""),
    })


@router.post("/auth/session")
def sync_supabase_session(
    request: Request,
    payload: SessionSyncPayload,
    session: Session = Depends(get_session),
):
    """Sync validated Supabase client token with server-side session."""
    token_data = verify_supabase_token(payload.access_token)
    user_id = ensure_uuid(token_data["sub"])
    email = token_data.get("email", "")
    metadata = token_data.get("user_metadata", {})
    username = metadata.get("username") or (email.split("@")[0] if email else str(user_id)[:8])

    request.session["user_id"] = str(user_id)
    request.session["email"] = email
    request.session["username"] = username
    request.session["access_token"] = payload.access_token

    # Find active or default course for user
    user_course = session.exec(
        select(UserCourse).where(
            UserCourse.user_id == user_id,
            UserCourse.is_default == True,
        )
    ).first()
    if not user_course:
        user_course = session.exec(
            select(UserCourse).where(UserCourse.user_id == user_id)
        ).first()

    if user_course:
        request.session["current_course_id"] = user_course.course_id
        course = session.get(Course, user_course.course_id)
        if course:
            request.session["current_course_name"] = course.name
            request.session["current_course_code"] = course.code

    return {"status": "ok", "user_id": str(user_id)}


@router.get("/logout")
def logout(request: Request):
    """Handle user logout: clear session and cookies."""
    request.session.clear()
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("sb_access_token", path="/")
    response.delete_cookie("access_token", path="/")
    return response


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, session: Session = Depends(get_session)):
    """Render profile page with user's enrolled courses and preferences."""
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        return RedirectResponse(url="/login", status_code=303)

    try:
        user_id = ensure_uuid(user_id_val)
    except Exception:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    statement = (
        select(UserCourse)
        .where(UserCourse.user_id == user_id)
        .options(selectinload(UserCourse.course))
    )
    user_courses = session.exec(statement).all()

    assigned_course_ids = [uc.course_id for uc in user_courses]
    if assigned_course_ids:
        available_courses = session.exec(
            select(Course).where(col(Course.id).not_in(assigned_course_ids))
        ).all()
    else:
        available_courses = session.exec(select(Course)).all()

    # Grading scales logic
    all_scales = session.exec(select(GradeScale)).all()
    seen_scale_names = set()
    grading_scales = []
    for scale in all_scales:
        if scale.scale_name not in seen_scale_names:
            seen_scale_names.add(scale.scale_name)
            grading_scales.append(scale)

    universities = session.exec(select(University)).all()

    # Build GPA scale selector options
    gpa_scale_rows = session.exec(
        select(GradeScale.scale_name, func.max(GradeScale.gpa_point))
        .where(GradeScale.band_type == "both")
        .group_by(GradeScale.scale_name)
        .order_by(func.max(GradeScale.gpa_point))
    ).all()
    gpa_scale_options = [
        (int(row[1]), row[0])
        for row in gpa_scale_rows
        if row[1] is not None
    ]

    # Per-scale data for registered scale cards
    _band_rows = session.exec(
        select(GradeScale.scale_name, GradeScale.grade, GradeScale.gpa_point)
        .where(GradeScale.band_type == "both")
        .order_by(GradeScale.scale_name, GradeScale.gpa_point.desc())
    ).all()
    _scale_map: dict[str, dict] = {}
    for _sname, _grade, _gpa in _band_rows:
        if _sname not in _scale_map:
            _scale_map[_sname] = {"scale_name": _sname, "max_gpa": int(_gpa), "bands": []}
        _scale_map[_sname]["bands"].append(_grade)
    gpa_scale_cards = sorted(_scale_map.values(), key=lambda x: x["max_gpa"])

    email = request.session.get("email", "")
    username = request.session.get("username") or (email.split("@")[0] if email else "User")
    user_data = {
        "id": str(user_id),
        "username": username,
        "email": email,
    }

    return _render(request, "profile.html", {
        "user": user_data,
        "courses": user_courses,
        "available_courses": available_courses,
        "grading_scales": grading_scales,
        "universities": universities,
        "gpa_scale_options": gpa_scale_options,
        "gpa_scale_cards": gpa_scale_cards,
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_anon_key": os.getenv("SUPABASE_ANON_KEY", ""),
    })


@router.get("/courses/create", response_class=HTMLResponse)
def course_create_page(request: Request, session: Session = Depends(get_session)):
    """Render a full-page form for creating a new course/program."""
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        return RedirectResponse(url="/login", status_code=303)

    universities = session.exec(select(University)).all()

    all_scales = session.exec(select(GradeScale)).all()
    seen_scale_names = set()
    grading_scales = []
    for scale in all_scales:
        if scale.scale_name not in seen_scale_names:
            seen_scale_names.add(scale.scale_name)
            grading_scales.append(scale)

    return _render(request, "course_create.html", {
        "universities": universities,
        "grading_scales": grading_scales,
    })


@router.post("/courses/create")
def course_create_submit(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    grading_scale_id: str = Form(...),
    university_id: str = Form(None),
    new_university_name: str = Form(None),
    custom_grading_scale: Optional[str] = Form(None),
    custom_grades: Optional[List[str]] = Form(None),
    custom_labels: Optional[List[str]] = Form(None),
    custom_min_marks: Optional[List[float]] = Form(None),
    custom_gpa_points: Optional[List[float]] = Form(None),
    custom_band_types: Optional[List[str]] = Form(None),
    session: Session = Depends(get_session),
):
    """Handle course creation form submission including custom grading bands."""
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        return RedirectResponse(url="/login", status_code=303)
    user_id = ensure_uuid(user_id_val)

    gs_id: Optional[int] = None
    if grading_scale_id and grading_scale_id != "other":
        try:
            gs_id = int(str(grading_scale_id))
        except Exception:
            return RedirectResponse(url="/courses/create?error=Invalid grading scale selected", status_code=303)
    elif grading_scale_id == "other":
        if not custom_grading_scale or not custom_grades or not custom_labels or not custom_min_marks or not custom_gpa_points:
            return RedirectResponse(url="/courses/create?error=Missing custom grading scale data", status_code=303)

        scale_name = custom_grading_scale.strip()
        if not scale_name:
            return RedirectResponse(url="/courses/create?error=Custom scale name is required", status_code=303)

        scale_ids: List[int] = []
        for i in range(len(custom_grades)):
            grade = (custom_grades[i] or "").strip()
            label = (custom_labels[i] or "").strip()
            if not grade or not label:
                continue
            try:
                min_mark = float(custom_min_marks[i]) if custom_min_marks[i] is not None else 0.0
            except Exception:
                min_mark = 0.0
            try:
                gpa_point = float(custom_gpa_points[i]) if custom_gpa_points[i] is not None else 0.0
            except Exception:
                gpa_point = 0.0
            band_type = (
                custom_band_types[i]
                if custom_band_types is not None and i < len(custom_band_types)
                else "both"
            )

            existing = session.exec(
                select(GradeScale).where(
                    GradeScale.scale_name == scale_name,
                    GradeScale.grade == grade,
                    GradeScale.band_type == band_type,
                )
            ).first()
            if existing:
                scale_ids.append(existing.id)  # type: ignore[arg-type]
            else:
                gs = GradeScale(
                    scale_name=scale_name,
                    grade=grade,
                    label=label,
                    min_mark=min_mark,
                    gpa_point=gpa_point,
                    band_type=band_type,
                )
                session.add(gs)
                session.commit()
                session.refresh(gs)
                scale_ids.append(gs.id)  # type: ignore[arg-type]

        if not scale_ids:
            return RedirectResponse(url="/courses/create?error=At least one grading band is required", status_code=303)

        gs_id = scale_ids[0]

    uni_id = None
    if university_id and university_id != "add_new":
        try:
            uni_id = int(university_id)
        except Exception:
            uni_id = None

    if gs_id is None:
        return RedirectResponse(url="/courses/create?error=Invalid grading scale selected", status_code=303)

    cm = CourseManager(session)
    course = cm.create_course(
        name=name,
        code=code,
        grading_scale_id=gs_id,
        university_id=uni_id,
        new_university_name=new_university_name if university_id == "add_new" else None,
    )

    user_courses = session.exec(
        select(UserCourse).where(UserCourse.user_id == user_id)
    ).all()
    is_default = len(user_courses) == 0

    user_course = UserCourse(
        user_id=user_id,
        course_id=course.id,  # type: ignore[arg-type]
        is_default=is_default,
    )
    session.add(user_course)
    session.commit()

    if is_default:
        request.session["current_course_id"] = course.id
        request.session["current_course_name"] = course.name
        request.session["current_course_code"] = course.code

    return RedirectResponse(url=f"/courses/{course.id}", status_code=303)


@router.post("/profile/add-course")
def add_course_to_user(
    request: Request,
    course_id: int = Form(...),
    session: Session = Depends(get_session),
):
    """Add a course to the current authenticated user's profile."""
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        return RedirectResponse(url="/login", status_code=303)
    user_id = ensure_uuid(user_id_val)

    existing = session.exec(
        select(UserCourse).where(
            UserCourse.user_id == user_id,
            UserCourse.course_id == course_id,
        )
    ).first()
    if existing:
        return RedirectResponse(url="/profile?error=Course already assigned", status_code=303)

    user_courses = session.exec(
        select(UserCourse).where(UserCourse.user_id == user_id)
    ).all()
    is_default = len(user_courses) == 0

    user_course = UserCourse(
        user_id=user_id,
        course_id=course_id,
        is_default=is_default,
    )
    session.add(user_course)
    session.commit()

    if is_default:
        course = session.get(Course, course_id)
        if course:
            request.session["current_course_id"] = course.id
            request.session["current_course_name"] = course.name
            request.session["current_course_code"] = course.code

    return RedirectResponse(url="/profile", status_code=303)


@router.post("/profile/create-course")
def create_course_for_user(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    grading_scale_id: str = Form(...),
    university_id: str = Form(None),
    new_university_name: str = Form(None),
    custom_grading_scale: Optional[str] = Form(None),
    custom_grades: Optional[List[str]] = Form(None),
    custom_labels: Optional[List[str]] = Form(None),
    custom_min_marks: Optional[List[float]] = Form(None),
    custom_gpa_points: Optional[List[float]] = Form(None),
    custom_band_types: Optional[List[str]] = Form(None),
    session: Session = Depends(get_session),
):
    """Create a brand new course and attach it to the current user."""
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        return RedirectResponse(url="/login", status_code=303)
    user_id = ensure_uuid(user_id_val)

    gs_id: Optional[int] = None
    if grading_scale_id and grading_scale_id != "other":
        try:
            gs_id = int(str(grading_scale_id))
        except Exception:
            return RedirectResponse(url="/profile?error=Invalid grading scale selected", status_code=303)
    elif grading_scale_id == "other":
        if not custom_grading_scale or not custom_grades or not custom_labels or not custom_min_marks or not custom_gpa_points:
            return RedirectResponse(url="/profile?error=Missing custom grading scale data", status_code=303)

        scale_name = custom_grading_scale.strip()
        if not scale_name:
            return RedirectResponse(url="/profile?error=Custom scale name is required", status_code=303)

        scale_ids: List[int] = []
        for i in range(len(custom_grades)):
            grade = (custom_grades[i] or "").strip()
            label = (custom_labels[i] or "").strip()
            if not grade or not label:
                continue
            try:
                min_mark = float(custom_min_marks[i]) if custom_min_marks[i] is not None else 0.0
            except Exception:
                min_mark = 0.0
            try:
                gpa_point = float(custom_gpa_points[i]) if custom_gpa_points[i] is not None else 0.0
            except Exception:
                gpa_point = 0.0
            band_type = (
                custom_band_types[i]
                if custom_band_types is not None and i < len(custom_band_types)
                else "both"
            )

            existing = session.exec(
                select(GradeScale).where(
                    GradeScale.scale_name == scale_name,
                    GradeScale.grade == grade,
                    GradeScale.band_type == band_type,
                )
            ).first()
            if existing:
                scale_ids.append(existing.id)  # type: ignore[arg-type]
            else:
                gs = GradeScale(
                    scale_name=scale_name,
                    grade=grade,
                    label=label,
                    min_mark=min_mark,
                    gpa_point=gpa_point,
                    band_type=band_type,
                )
                session.add(gs)
                session.commit()
                session.refresh(gs)
                scale_ids.append(gs.id)  # type: ignore[arg-type]

        if not scale_ids:
            return RedirectResponse(url="/profile?error=Failed to create custom grading scale", status_code=303)

        gs_id = scale_ids[0]

    uni_id = None
    if university_id and university_id != "add_new":
        try:
            uni_id = int(university_id)
        except Exception:
            uni_id = None

    if gs_id is None:
        return RedirectResponse(url="/profile?error=Invalid grading scale selected", status_code=303)

    cm = CourseManager(session)
    course = cm.create_course(
        name=name,
        code=code,
        grading_scale_id=gs_id,
        university_id=uni_id,
        new_university_name=new_university_name if university_id == "add_new" else None,
    )

    user_courses = session.exec(
        select(UserCourse).where(UserCourse.user_id == user_id)
    ).all()
    is_default = len(user_courses) == 0

    user_course = UserCourse(
        user_id=user_id,
        course_id=course.id,  # type: ignore[arg-type]
        is_default=is_default,
    )
    session.add(user_course)
    session.commit()

    if is_default:
        request.session["current_course_id"] = course.id
        request.session["current_course_name"] = course.name
        request.session["current_course_code"] = course.code

    return RedirectResponse(url="/profile", status_code=303)


@router.post("/profile/remove-course/{user_course_id}")
def remove_course_from_user(
    request: Request,
    user_course_id: int,
    session: Session = Depends(get_session),
):
    """Remove a course from the current authenticated user."""
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        return RedirectResponse(url="/login", status_code=303)
    user_id = ensure_uuid(user_id_val)

    user_course = session.get(UserCourse, user_course_id)
    if not user_course or user_course.user_id != user_id:
        return RedirectResponse(url="/profile?error=Course not found", status_code=303)

    was_default = user_course.is_default
    session.delete(user_course)
    session.commit()

    if was_default:
        remaining = session.exec(
            select(UserCourse).where(UserCourse.user_id == user_id)
        ).first()

        if remaining:
            remaining.is_default = True
            session.add(remaining)
            session.commit()

            course = session.get(Course, remaining.course_id)
            if course:
                request.session["current_course_id"] = course.id
                request.session["current_course_name"] = course.name
                request.session["current_course_code"] = course.code
        else:
            request.session.pop("current_course_id", None)
            request.session.pop("current_course_name", None)
            request.session.pop("current_course_code", None)

    return RedirectResponse(url="/profile", status_code=303)


@router.post("/profile/set-default/{user_course_id}")
def set_default_course(
    request: Request,
    user_course_id: int,
    session: Session = Depends(get_session),
):
    """Set a course as default/active for the current user."""
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        return RedirectResponse(url="/login", status_code=303)
    user_id = ensure_uuid(user_id_val)

    user_course = session.get(UserCourse, user_course_id)
    if not user_course or user_course.user_id != user_id:
        return RedirectResponse(url="/profile?error=Course not found", status_code=303)

    user_courses = session.exec(
        select(UserCourse).where(UserCourse.user_id == user_id)
    ).all()

    for uc in user_courses:
        uc.is_default = (uc.id == user_course_id)
        session.add(uc)

    session.commit()

    course = session.get(Course, user_course.course_id)
    if course:
        request.session["current_course_id"] = course.id
        request.session["current_course_name"] = course.name
        request.session["current_course_code"] = course.code

    return RedirectResponse(url="/profile", status_code=303)
