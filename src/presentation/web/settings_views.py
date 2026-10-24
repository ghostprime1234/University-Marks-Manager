from typing import List, Optional
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, col, select, desc, func

from src.presentation.api.deps import get_session
from src.infrastructure.db.models import GradeScale, Course, UserCourse
from src.presentation.web.template_helpers import _render
from src.core.services.grade_calculator import GradeCalculator
from src.core.auth import ensure_uuid

router = APIRouter()

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_session)):
    """Render the settings page."""
    # Determine active course to show its scale
    sess = request.session
    active_course_id = sess.get("current_course_id")

    # Robustly resolve active course ID if it's missing or stringified
    if active_course_id is None:
        code = sess.get("current_course_code")
        if code:
            course_obj = session.exec(
                select(Course).where(func.upper(func.trim(Course.code)) == str(code).strip().upper())
            ).first()
            if course_obj and course_obj.id is not None:
                active_course_id = course_obj.id
                sess["current_course_id"] = active_course_id

    # If still None, try resolving the user's default or first enrolled course
    if active_course_id is None and sess.get("user_id"):
        try:
            uid = ensure_uuid(sess.get("user_id"))
            uc = session.exec(
                select(UserCourse).where(UserCourse.user_id == uid, UserCourse.is_default == True)
            ).first() or session.exec(
                select(UserCourse).where(UserCourse.user_id == uid)
            ).first()
            if uc and uc.course_id is not None:
                active_course_id = uc.course_id
                sess["current_course_id"] = active_course_id
        except Exception:
            pass

    current_scale_id = None
    course = None
    if active_course_id is not None:
        try:
            cid = int(str(active_course_id).strip())
            course = session.get(Course, cid)
            if course:
                sess["current_course_name"] = course.name
                sess["current_course_code"] = course.code
                if getattr(course, "grading_scale_id", None):
                    current_scale_id = course.grading_scale_id
            else:
                sess.pop("current_course_id", None)
        except (ValueError, TypeError):
            pass

    # Ensure defaults exist for this scale (via calculator logic)
    gc = GradeCalculator(session)
    try:
        gc._get_grade_scales(course.id if (course and course.id is not None) else None)
    except Exception:
        pass

    # Fetch all available scale names
    all_scale_names = [
        name for name in session.exec(select(GradeScale.scale_name).distinct()).all() if name
    ]

    # Get the scale_name for the current grading_scale id
    current_scale_name = None
    if current_scale_id:
        scale_obj = session.get(GradeScale, current_scale_id)
        if scale_obj:
            current_scale_name = scale_obj.scale_name
    if not current_scale_name:
        current_scale_name = "Standard"
    wam_bands = session.exec(
        select(GradeScale)
        .where(
            (col(GradeScale.scale_name) == current_scale_name) &
            (col(GradeScale.band_type).in_(["wam", "both"]))
        )
        .order_by(desc(GradeScale.min_mark))
    ).all()
    gpa_bands = session.exec(
        select(GradeScale)
        .where(
            (col(GradeScale.scale_name) == current_scale_name) &
            (col(GradeScale.band_type).in_(["gpa", "both"]))
        )
        .order_by(desc(GradeScale.min_mark))
    ).all()
    return _render(request, "settings.html", {
        "wam_bands": wam_bands,
        "gpa_bands": gpa_bands,
        "current_scale_name": current_scale_name,
        "course": course,
        "all_scale_names": all_scale_names,
        "breadcrumbs": [{"label": "Settings", "url": None}],
    })


@router.get("/courses", response_class=HTMLResponse)
def courses_page(request: Request, session: Session = Depends(get_session)):
    """Render the manage courses page, including a list of all existing courses.

    NOTE: The dedicated creation workflow lives at /courses/create on the courses
    router; this endpoint is retained only for any legacy usages.
    """
    courses = session.exec(select(Course)).all()
    return _render(request, "courses.html", {
        "courses": courses,
        # Hide the Active Course header on the legacy manage-courses page
        "no_courses_warning": True,
    })

# Endpoint to update the grading scale for the active course
@router.post("/settings/scale/update")
def update_course_scale(
    request: Request,
    scale_name: str = Form(...),
    session: Session = Depends(get_session)
):
    sess = request.session
    active_course_id = sess.get("current_course_id")
    if active_course_id:
        try:
            cid = int(str(active_course_id))
            course = session.get(Course, cid)
            if course:
                # Find the GradeScale by scale_name, then set grading_scale_id to its id
                scale = session.exec(select(GradeScale).where(GradeScale.scale_name == scale_name)).first()
                if scale and scale.id is not None:
                    course.grading_scale_id = scale.id
                    session.add(course)
                    session.commit()
                    sess["flash_message"] = f"Grading scale for '{course.name}' updated to '{scale_name}'."
        except Exception:
            pass
    return RedirectResponse(url="/settings", status_code=303)

@router.post("/settings/grade-scales/add")
def add_grade_scale_band(
    request: Request,
    scale_name: str = Form(...),
    grade: str = Form(...),
    label: str = Form(...),
    min_mark: float = Form(...),
    gpa_point: float = Form(...),
    band_type: str = Form("both"),
    session: Session = Depends(get_session),
):
    """Add or update a single grade band within a named GPA scale.

    Matches on the unique constraint (scale_name, grade, band_type): if a row
    already exists it is updated in-place; otherwise a new row is inserted.
    This allows callers to build up a complete scale one band at a time, or to
    correct an existing band without touching the rest of the scale.
    """
    scale_name = scale_name.strip()
    grade = grade.strip().upper()
    label = label.strip()
    if not scale_name or not grade or not label:
        request.session["flash_message"] = "Scale name, grade, and label are all required."
        return RedirectResponse(url="/profile", status_code=303)
    if band_type not in ("both", "wam", "gpa"):
        band_type = "both"

    existing = session.exec(
        select(GradeScale).where(
            GradeScale.scale_name == scale_name,
            GradeScale.grade == grade,
            GradeScale.band_type == band_type,
        )
    ).first()

    if existing:
        existing.label = label
        existing.min_mark = min_mark
        existing.gpa_point = gpa_point
        session.add(existing)
        session.commit()
        request.session["flash_message"] = f"Updated '{grade}' band in scale '{scale_name}'."
    else:
        row = GradeScale(
            scale_name=scale_name,
            grade=grade,
            label=label,
            min_mark=min_mark,
            gpa_point=gpa_point,
            band_type=band_type,
        )
        session.add(row)
        session.commit()
        request.session["flash_message"] = f"Added '{grade}' to scale '{scale_name}'."

    return RedirectResponse(url="/profile", status_code=303)


@router.post("/settings/grades/update")
def update_grades(
    request: Request,
    scale_name: str = Form(...),
    grades: List[str] = Form(...),
    labels: List[str] = Form(...),
    session: Session = Depends(get_session),
    min_marks: Optional[List[float]] = Form(None),
    gpa_points: Optional[List[float]] = Form(None),
    band_type: str = Form(...)
):
    """Update grade scales for a specific scale name and band type (wam/gpa)."""
    scale_name = scale_name.strip()
    min_marks = min_marks if min_marks is not None else []
    gpa_points = gpa_points if gpa_points is not None else []

    existing_rows = session.exec(
        select(GradeScale).where(
            col(GradeScale.scale_name) == scale_name,
            col(GradeScale.band_type).in_([band_type, "both"]),
        )
    ).all()
    existing_by_grade = {r.grade.strip().upper(): r for r in existing_rows}
    submitted_grades: set[str] = set()

    if band_type == "wam":
        for g, l, m in zip(grades, labels, min_marks):
            g_norm = g.strip().upper()
            if not g_norm:
                continue
            submitted_grades.add(g_norm)
            if g_norm in existing_by_grade:
                row = existing_by_grade[g_norm]
                row.grade = g.strip()
                row.label = l.strip()
                row.min_mark = m
                session.add(row)
            else:
                session.add(
                    GradeScale(
                        scale_name=scale_name,
                        grade=g.strip(),
                        label=l.strip(),
                        min_mark=m,
                        gpa_point=0.0,
                        band_type="both",
                    )
                )
    elif band_type == "gpa":
        for g, l, p in zip(grades, labels, gpa_points):
            g_norm = g.strip().upper()
            if not g_norm:
                continue
            submitted_grades.add(g_norm)
            if g_norm in existing_by_grade:
                row = existing_by_grade[g_norm]
                row.grade = g.strip()
                row.label = l.strip()
                row.gpa_point = p
                session.add(row)
            else:
                session.add(
                    GradeScale(
                        scale_name=scale_name,
                        grade=g.strip(),
                        label=l.strip(),
                        min_mark=0.0,
                        gpa_point=p,
                        band_type="both",
                    )
                )

    for g_norm, row in existing_by_grade.items():
        if g_norm not in submitted_grades and row.band_type == band_type:
            session.delete(row)

    session.commit()
    request.session["flash_message"] = f"{band_type.upper()} bands for '{scale_name}' updated successfully."
    return RedirectResponse(url="/settings", status_code=303)
