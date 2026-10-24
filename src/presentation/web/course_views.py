"""Web views for managing courses."""
from __future__ import annotations

from typing import Optional, cast

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlmodel import Session, select, func
from sqlmodel import Table

from src.core.services.course_manager import CourseManager
from src.core.services.semester_manager import SemesterManager
from src.core.services.grade_calculator import GradeCalculator
from src.infrastructure.db.engine import get_session
from src.infrastructure.db.models import Subject, Course, Semester, University, GradeScale

router = APIRouter()


def _resolve_course(course_manager: CourseManager, key: str) -> Optional[Course]:
    """Resolve a course by code or numeric id (backward compatible).

    Prefer code lookup FIRST, even if the key is all-digits, to support numeric codes like "000".
    Fallback to numeric id lookup only if code lookup fails and the key is digits.
    """
    # Try by code first (supports numeric codes like "000")
    by_code = course_manager.get_course_by_code(key)
    if by_code:
        return by_code
    # Fallback to id only when code lookup fails
    if key.isdigit():
        return course_manager.get_course_by_id(int(key))
    return None


@router.get("/courses", response_class=HTMLResponse)
def get_courses_page(
    request: Request,
    session: Session = Depends(get_session),
):
    """Redirect to profile page where users manage their courses."""
    return RedirectResponse(url="/profile", status_code=303)


@router.head("/courses")
def get_courses_head(request: Request, session: Session = Depends(get_session)):
    """HEAD variant for the courses redirect."""
    return Response(status_code=303, headers={"Location": "/profile"})


# The selector fragment must be registered before the dynamic /courses/{course_code}
# route to avoid the literal path "_selector" being captured as a course_code.
# See: requests like /courses/_selector should match this static route.
@router.get("/courses/_selector", response_class=HTMLResponse)
def courses_selector_fragment(request: Request, session: Session = Depends(get_session)):
    """Return a small HTML fragment with a course dropdown for inline selection.

    This endpoint is intended to be loaded into the header via HTMX so users can
    change the active course without leaving the current page.
    """
    jinja_env = request.app.state.jinja_env
    cm = CourseManager(session)
    courses = cm.get_all_courses()
    template = jinja_env.get_template("partials/course_header.html")
    current_course_id = request.session.get("current_course_id")
    current_course_name = request.session.get("current_course_name")
    current_course_code = request.session.get("current_course_code")
    return template.render(
        request=request,
        courses=courses,
        current_course_id=current_course_id,
        current_course_name=current_course_name,
        current_course_code=current_course_code,
    )


@router.get("/courses/_codes", response_class=HTMLResponse)
@router.get("/courses/_debug/codes", response_class=HTMLResponse)
def debug_course_codes(
    request: Request,
    session: Session = Depends(get_session),
):
    """Debug page: list all course codes with raw/trimmed values and potential collisions.

    Useful for spotting trailing spaces or inconsistent casing in Course.code.
    """
    # Gate behind env flag and optional token
    app = request.app
    if not getattr(app.state, "enable_debug_routes", False):
        return HTMLResponse("Not found", status_code=404)
    expected = getattr(app.state, "debug_token", None)
    if expected and request.query_params.get("token") != expected:
        return HTMLResponse("Not found", status_code=404)
    cm = CourseManager(session)
    courses = cm.get_all_courses()
    # Build a simple HTML table (no separate template needed)
    def esc(s: str) -> str:
        """
        Short description.

        Args:
            s: Description.

        Returns:
            Description.

        Raises:
            Description.
        """
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
    rows = []
    for c in courses:
        code = c.code or ""
        trimmed = code.strip()
        rows.append(
            f"<tr>"
            f"<td>{c.id}</td>"
            f"<td>{esc(c.name)}</td>"
            f"<td class='codecell'><code>{esc(repr(code))}</code></td>"
            f"<td style='text-align:right'>{len(code)}</td>"
            f"<td class='codecell'><code>{esc(repr(trimmed))}</code></td>"
            f"<td style='text-align:right'>{len(trimmed)}</td>"
            f"</tr>"
        )
    # Detect potential collisions after TRIM+lower()
    from collections import defaultdict
    buckets = defaultdict(list)
    for c in courses:
        norm = (c.code or "").strip().lower()
        if norm:
            buckets[norm].append(c)
    collisions = {k: v for k, v in buckets.items() if len(v) > 1}

    html = [
        "<html><head><title>Course Codes Debug</title>",
        "<meta name=\"robots\" content=\"noindex,nofollow\">",
        # Tailwind may or may not be present; add inline CSS fallback for readability
        "<link rel=\"stylesheet\" href=\"/static/css/tailwind.css\">",
        "<style>",
        ":root{color-scheme:light dark;}\n",
    "body{margin:16px;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,\"Apple Color Emoji\",\"Segoe UI Emoji\";color:#111;background:#ffffff;}\n",
        ".container{max-width:1200px;margin:0 auto;}\n",
        "table{border-collapse:collapse;width:100%;table-layout:fixed;}\n",
    "thead th{background:#f3f4f6;color:#111;text-align:left;}\n",
        "th,td{border:1px solid #e5e7eb;padding:8px 10px;vertical-align:top;}\n",
        "col.id{width:64px;}col.name{width:320px;}col.code{width:300px;}col.len{width:80px;}col.trim{width:300px;}col.trimlen{width:100px;}\n",
        "code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,\"Liberation Mono\",monospace;font-size:12px;}\n",
        ".codecell{white-space:pre-wrap;word-break:break-word;}\n",
        ".muted{opacity:.75;}\n",
    "@media (prefers-color-scheme: dark){\n",
    "  body{background:#111827;color:#e5e7eb;}\n",
    "  thead th{background:#374151;color:#e5e7eb;}\n",
    "  th,td{border-color:#374151;}\n",
    "}\n",
        "</style>",
        "</head><body>",
        "<div class='container'>",
        "<h1 style='font-size:1.25rem;font-weight:600;margin:0 0 0.5rem;'>Course Codes (raw vs trimmed)</h1>",
        "<p class='muted' style='margin:0 0 1rem;'>This debug page helps identify trailing spaces or case inconsistencies. Raw values are shown using Python repr().</p>",
        "<div style='overflow-x:auto;'>",
        "<table>",
        "<colgroup>",
        "<col class='id'/><col class='name'/><col class='code'/><col class='len'/><col class='trim'/><col class='trimlen'/>",
        "</colgroup>",
        "<thead><tr>",
        "<th>id</th>",
        "<th>name</th>",
        "<th>code (raw)</th>",
        "<th>len</th>",
        "<th>trimmed</th>",
        "<th>trim_len</th>",
        "</tr></thead>",
        "<tbody>",
        *rows,
        "</tbody></table></div>",
        "</div>",
        "</body></html>",
    ]
    if collisions:
        html += [
            "<div class='container' style='margin-top:1rem'>",
            "<h2 style='font-size:1.125rem;font-weight:600'>Potential collisions after TRIM+lower()</h2>",
            "<ul style='margin-top:.5rem;padding-left:1.25rem;list-style:disc'>",
        ]
        for k, vs in collisions.items():
            ids = ", ".join(str(v.id) for v in vs)
            codes = ", ".join(esc(v.code or "") for v in vs)
            html.append(f"<li><code>{esc(k)}</code> -> ids [{ids}], codes [{codes}]</li>")
        html.append("</ul></div>")
    else:
        html.append("<p class='container muted' style='margin-top:1rem'>No collisions detected after TRIM+lower().</p>")
    return HTMLResponse("".join(html))


@router.get("/courses/_resolve/{key}")
def debug_resolve_course(key: str, request: Request, session: Session = Depends(get_session)):
    """Return how a course key resolves (by_code or by_id) and the course info.

    Helpful for debugging why numeric codes like "000" may not resolve in routes.
    """
    app = request.app
    if not getattr(app.state, "enable_debug_routes", False):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    expected = getattr(app.state, "debug_token", None)
    if expected and request.query_params.get("token") != expected:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    cm = CourseManager(session)
    # Try code first
    course = cm.get_course_by_code(key)
    matched_by = "code" if course else None
    if not course and key.isdigit():
        course = cm.get_course_by_id(int(key))
        matched_by = "id" if course else None
    if not course:
        return JSONResponse({"matched_by": None, "found": False}, status_code=404)
    return JSONResponse({
        "matched_by": matched_by,
        "found": True,
        "course": {
            "id": course.id,
            "name": course.name,
            "code": course.code,
        }
    })


@router.post("/courses/", response_class=HTMLResponse)
async def create_course_view(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    grading_scale_id: str = Form(...),
    university_id: str = Form(None),
    new_university_name: str = Form(None),
    session: Session = Depends(get_session),
):
    """Handle the form submission to create a new course and return the HTML fragment."""
    jinja_env = request.app.state.jinja_env
    course_manager = CourseManager(session)
    # Handle grading_scale_id: if 'other', pass None or handle custom logic
    gs_id = None
    custom_scale_id = None
    if grading_scale_id and grading_scale_id != "other":
        try:
            gs_id = int(grading_scale_id)
        except Exception:
            gs_id = None
    elif grading_scale_id == "other":
        # Handle custom grading scale creation (async)
        form = await request.form()
        custom_scale_name = str(form.get("custom_grading_scale")) if form.get("custom_grading_scale") is not None else None
        custom_grades = [str(x) for x in form.getlist("custom_grades[]")]
        custom_labels = [str(x) for x in form.getlist("custom_labels[]")]
        custom_min_marks = [str(x) for x in form.getlist("custom_min_marks[]")]
        custom_gpa_points = [str(x) for x in form.getlist("custom_gpa_points[]")]
        custom_band_types = [str(x) for x in form.getlist("custom_band_types[]")]

        # Validate required fields
        if not custom_scale_name or not custom_grades or not custom_labels or not custom_min_marks or not custom_gpa_points:
            return HTMLResponse("Missing custom grading scale data.", status_code=400)

        # Create GradeScale entries for each row
        from src.infrastructure.db.models import GradeScale
        scale_ids = []
        for i in range(len(custom_grades)):
            grade = custom_grades[i]
            label = custom_labels[i]
            try:
                min_mark = float(custom_min_marks[i])
            except Exception:
                min_mark = 0.0
            try:
                gpa_point = float(custom_gpa_points[i])
            except Exception:
                gpa_point = 0.0
            band_type = custom_band_types[i] if custom_band_types and i < len(custom_band_types) else "both"
            # Check if this GradeScale row already exists (avoid duplicates)
            existing = session.exec(
                select(GradeScale).where(
                    GradeScale.scale_name == custom_scale_name,
                    GradeScale.grade == grade,
                    GradeScale.band_type == band_type
                )
            ).first()
            if existing:
                scale_ids.append(existing.id)
            else:
                gs = GradeScale(
                    scale_name=custom_scale_name,
                    grade=grade,
                    label=label,
                    min_mark=min_mark,
                    gpa_point=gpa_point,
                    band_type=band_type
                )
                session.add(gs)
                session.commit()
                session.refresh(gs)
                scale_ids.append(gs.id)
        # Use the first GradeScale row's id as the grading_scale_id for the course
        if scale_ids:
            gs_id = scale_ids[0]
        else:
            return HTMLResponse("Failed to create custom grading scale.", status_code=400)

    # Handle university_id: if 'add_new', use new_university_name
    uni_id = None
    if university_id and university_id != "add_new":
        try:
            uni_id = int(university_id)
        except Exception:
            uni_id = None
    elif university_id == "add_new" and new_university_name:
        uni_id = None  # Will be handled in create_course
    if gs_id is not None:
        course = course_manager.create_course(
            name=name,
            code=code,
            grading_scale_id=gs_id,
            university_id=uni_id,
            new_university_name=new_university_name if university_id == "add_new" else None,
        )
    else:
        return HTMLResponse("Invalid grading scale selected.", status_code=400)
    # After creating, render the full course list for HTMX swap
    courses = course_manager.get_all_courses()
    template = jinja_env.get_template("partials/course_list.html")
    content = template.render(request=request, courses=courses)
    if request.headers.get("HX-Request"):
        resp = HTMLResponse(content=content, status_code=200)
        resp.headers["Content-Type"] = "text/html"
        resp.headers["HX-Trigger"] = "courseListChanged"
        return resp
    return HTMLResponse(content=content, status_code=200)


@router.get("/courses/create", response_class=HTMLResponse)
def create_course_page(
    request: Request,
    session: Session = Depends(get_session),
):
    """Render the create/manage courses page used from the profile screen.

    This must be declared before the dynamic /courses/{course_code} route so
    that the literal path segment "create" is not treated as a course code.
    """
    jinja_env = request.app.state.jinja_env
    course_manager = CourseManager(session)

    # All existing courses for the list
    courses = course_manager.get_all_courses()

    # Distinct grading scales (by scale_name) for the dropdown
    all_scales = session.exec(select(GradeScale)).all()
    seen_scale_names = set()
    grading_scales = []
    for scale in all_scales:
        name = getattr(scale, "scale_name", None)
        if name and name not in seen_scale_names:
            seen_scale_names.add(name)
            grading_scales.append(scale)

    # All universities for the university selector
    universities = session.exec(select(University)).all()

    template = jinja_env.get_template("courses.html")
    return template.render(
        request=request,
        courses=courses,
        grading_scales=grading_scales,
        universities=universities,
        # Hide the "Active Course" header on the global manage-courses page
        no_courses_warning=True,
    )


@router.get("/courses/{course_identifier}", response_class=HTMLResponse)
def get_course_detail_page(
    request: Request,
    course_identifier: str,
    session: Session = Depends(get_session),
):
    """Render the detail page for a specific course."""
    jinja_env = request.app.state.jinja_env
    course_manager = CourseManager(session)
    semester_manager = SemesterManager(session)
    grade_calculator = GradeCalculator(session)

    # First check by course code (case-insensitive, supports numeric codes like "766")
    course = session.exec(
        select(Course).where(func.upper(func.trim(Course.code)) == course_identifier.strip().upper())
    ).first()
    if not course and course_identifier.isdigit():
        course = session.exec(select(Course).where(Course.id == int(course_identifier))).first()
        if course and course.code:
            # Canonical redirect to course code URL
            return RedirectResponse(url=f"/courses/{course.code.strip()}", status_code=302)

    if not course:
        # Handle course not found, maybe redirect or show an error page
        # For now, we'll just return a simple error
        return HTMLResponse("Course not found", status_code=404)

    # Assigned semesters for this course
    assigned_semesters = list(getattr(course, "semesters", []) or [])
    # Unassigned (available) semesters and years
    unassigned_semesters = course_manager.get_unassigned_semesters()
    unassigned_years = course_manager.get_unassigned_years()

    # Map assigned semester -> subjects within that term for optional display
    subjects_by_semester: dict[int, list[Subject]] = {}
    subjects_table = cast(Table, getattr(Subject, "__table__"))
    for sem in assigned_semesters:
        candidates = session.exec(
            select(Subject)
            .where(
                Subject.semester_id == sem.id,
            )
            .order_by(subjects_table.c.subject_code.asc())
        ).all()
        subjects_by_semester[getattr(sem, "id")] = list(candidates)

    # Calculate grade statistics for dashboard
    wam = grade_calculator.calculate_wam(course.id) if course.id else None
    gpa = grade_calculator.calculate_gpa(course.id) if course.id else None
    grade_counts = grade_calculator.calculate_grade_counts(course.id) if course.id else {}

    # Resolve the GPA max from the stored gpa_scale (default 4 for Standard scale)
    gpa_max = int(getattr(course, "gpa_scale", None) or 4)

    # Get all universities for the dropdown
    from src.infrastructure.db.models import University
    universities = session.exec(select(University)).all()
    template = jinja_env.get_template("course_detail.html")
    breadcrumbs = [
        {"label": "Courses", "url": "/courses"},
        {"label": course.code or course.name, "url": None},
    ]
    return template.render(
        request=request,
        course=course,
        breadcrumbs=breadcrumbs,
        assigned_semesters=assigned_semesters,
        unassigned_semesters=unassigned_semesters,
        unassigned_years=unassigned_years,
        subjects_by_semester=subjects_by_semester,
        universities=universities,
        wam=wam,
        gpa=gpa,
        gpa_max=gpa_max,
        grade_counts=grade_counts,
    )


# Removed subject-level linking UI per requirements


@router.post("/courses/{course_identifier}/assign-semester", response_class=HTMLResponse)
def assign_semester_to_course_view(
    request: Request,
    course_identifier: str,
    semester_key: str = Form(...),
    session: Session = Depends(get_session),
):
    """Handle assigning a semester to a course and reload the page."""
    course_manager = CourseManager(session)
    course = _resolve_course(course_manager, course_identifier)
    if not course:
        return HTMLResponse("Course not found", status_code=404)
    assert course.id is not None
    try:
        sem_name, sem_year = semester_key.split("|", 1)
        sem_year_int = int(sem_year)
    except Exception:
        return HTMLResponse("Invalid semester selection", status_code=400)
    # Resolve semester by (name, year)
    from sqlmodel import select
    sem = session.exec(select(Semester).where(Semester.name == sem_name, Semester.year == sem_year_int)).first()
    if not sem:
        return HTMLResponse("Semester not found", status_code=404)
    course_manager.assign_semester_to_course(course_id=course.id, semester_id=sem.id)  # type: ignore[arg-type]

    # Redirect to the canonical course code page to see the change
    canonical = course.code.strip() if course.code else course_identifier
    response = HTMLResponse()
    response.headers["HX-Redirect"] = f"/courses/{canonical}"
    return response


@router.post("/courses/{course_identifier}/assign-year", response_class=HTMLResponse)
def assign_year_to_course_view(
    request: Request,
    course_identifier: str,
    year: int = Form(...),
    session: Session = Depends(get_session),
):
    """Assign all semesters for a given year to a course and reload the page."""
    course_manager = CourseManager(session)
    course = _resolve_course(course_manager, course_identifier)
    if not course:
        return HTMLResponse("Course not found", status_code=404)
    assert course.id is not None
    course_manager.assign_year_to_course(course_id=course.id, year=year)

    canonical = course.code.strip() if course.code else course_identifier
    response = HTMLResponse()
    response.headers["HX-Redirect"] = f"/courses/{canonical}"
    return response


@router.post("/courses/{course_identifier}/select")
def select_current_course(
    request: Request,
    course_identifier: str,
    session: Session = Depends(get_session),
):
    """Persist the selected course in the session and redirect to Home reliably.

    Always returns a 303 to "/" and also sets HX-Redirect for HTMX safety.
    """
    # Minimal session payload; name can be looked up lazily if needed
    sess = request.session
    # Resolve basic course info for display in header (supports code or legacy id)
    course = _resolve_course(CourseManager(session), course_identifier)
    if course:
        sess["current_course_id"] = course.id
        sess["current_course_name"] = course.name
        if getattr(course, "code", None):
            sess["current_course_code"] = course.code
        else:
            sess.pop("current_course_code", None)
        # one-time flash message for the next page view
        sess["flash_message"] = f"Active course changed to {course.name}{' (' + course.code + ')' if getattr(course, 'code', None) else ''}."
    # If HTMX requested, return an inline success alert instead of redirect
    if request.headers.get("HX-Request"):
        # For HTMX, instruct the browser to navigate to Home (which will default to current year)
        redirect_url = "/?selected=1"
        resp = HTMLResponse("")
        resp.headers["HX-Redirect"] = redirect_url
        return resp

    # Otherwise, return a redirect response to home with a hint for UI to show alert
    redirect_url = "/?selected=1"
    response = RedirectResponse(url=redirect_url, status_code=303)
    response.headers["HX-Redirect"] = redirect_url
    return response


@router.post("/courses/select-inline")
def select_course_inline(request: Request, course_key: str = Form(""), session: Session = Depends(get_session)) -> Response:
    """Set the active course via an inline POST and return the updated selector fragment.

    If the request is via HTMX the fragment returned will replace the selector and
    include a small success message via the flash mechanism.
    """
    # Debug: log invocation so container logs show whether the endpoint was hit
    try:
        print(f"[debug] select_course_inline hit course_key={course_key!r} HX-Request={request.headers.get('HX-Request')!r}")
    except Exception:
        # avoid any accidental logging errors breaking the flow
        pass
    sess = request.session
    # Treat explicit sentinel or empty value as a clear request
    if not course_key or course_key == "__clear__":
        sess.pop("current_course_id", None)
        sess.pop("current_course_name", None)
        sess.pop("current_course_code", None)
        sess["flash_message"] = "Active course cleared."
    else:
        course = _resolve_course(CourseManager(session), course_key)
        if course:
            sess["current_course_id"] = course.id
            sess["current_course_name"] = course.name
            if getattr(course, "code", None):
                sess["current_course_code"] = course.code
            else:
                sess.pop("current_course_code", None)
            sess["flash_message"] = f"Active course changed to {course.name}{' (' + course.code + ')' if getattr(course, 'code', None) else ''}."

    # After changing the session, navigate to Home so the header (active course)
    # is refreshed. For HTMX clients we provide both HX-Redirect and HX-Refresh
    # as some proxies or intermediary rewrites can prevent the browser from
    # following the redirect reliably; HX-Refresh forces a full client reload.
    redirect_url = "/?selected=1"
    if request.headers.get("HX-Request"):
        # Return an HTMLResponse with HTMX headers so HTMX will perform a
        # navigation or a full page refresh.
        resp = HTMLResponse("")
        resp.headers["HX-Redirect"] = redirect_url
        resp.headers["HX-Refresh"] = "true"
        return resp

    # Non-HTMX clients: standard 303 redirect to Home
    response = RedirectResponse(url=redirect_url, status_code=303)
    response.headers["HX-Redirect"] = redirect_url
    return response


@router.post("/courses/clear-selection")
def clear_current_course(
    request: Request,
):
    """Clear the selected course from the session and redirect to courses list."""
    sess = request.session
    sess.pop("current_course_id", None)
    response = RedirectResponse(url="/courses", status_code=303)
    response.headers["HX-Redirect"] = "/courses"
    return response


@router.post("/courses/{course_identifier}/assign-all", response_class=HTMLResponse)
def assign_all_semesters_to_course_view(
    request: Request,
    course_identifier: str,
    session: Session = Depends(get_session),
):
    """Assign all unassigned semesters to a course and reload the page."""
    course_manager = CourseManager(session)
    course = _resolve_course(course_manager, course_identifier)
    if not course:
        return HTMLResponse("Course not found", status_code=404)
    assert course.id is not None
    course_manager.assign_all_semesters_to_course(course_id=course.id)

    canonical = course.code.strip() if course.code else course_identifier
    response = HTMLResponse()
    response.headers["HX-Redirect"] = f"/courses/{canonical}"
    return response


@router.post("/courses/{course_identifier}/unassign-semester", response_class=HTMLResponse)
def unassign_semester_from_course_view(
    request: Request,
    course_identifier: str,
    semester_name: str = Form(...),
    year: int = Form(...),
    session: Session = Depends(get_session),
):
    """Remove a semester from the course."""
    course_manager = CourseManager(session)
    course = _resolve_course(course_manager, course_identifier)
    if not course:
        return HTMLResponse("Course not found", status_code=404)
    from sqlmodel import select
    sem = session.exec(select(Semester).where(Semester.name == semester_name, Semester.year == year)).first()
    if not sem:
        return HTMLResponse("Semester not found", status_code=404)
    course_manager.unassign_semester_from_course(course_id=course.id, semester_id=sem.id)  # type: ignore[arg-type]
    canonical = course.code.strip() if course.code else course_identifier
    if request.headers.get("HX-Request"):
        response = HTMLResponse()
        response.headers["HX-Redirect"] = f"/courses/{canonical}"
        return response
    return RedirectResponse(url=f"/courses/{canonical}", status_code=303)


@router.post("/courses/{course_identifier}/unassign-year", response_class=HTMLResponse)
def unassign_year_from_course_view(
    request: Request,
    course_identifier: str,
    year: int = Form(...),
    session: Session = Depends(get_session),
):
    """Remove all semesters for the given year from the course."""
    course_manager = CourseManager(session)
    course = _resolve_course(course_manager, course_identifier)
    if not course:
        return HTMLResponse("Course not found", status_code=404)
    assert course.id is not None
    course_manager.unassign_year_from_course(course_id=course.id, year=year)
    canonical = course.code.strip() if course.code else course_identifier
    if request.headers.get("HX-Request"):
        response = HTMLResponse()
        response.headers["HX-Redirect"] = f"/courses/{canonical}"
        return response
    return RedirectResponse(url=f"/courses/{canonical}", status_code=303)

@router.post("/courses/{course_identifier}/update", response_class=HTMLResponse)
def update_course_view(
    request: Request,
    course_identifier: str,
    name: str = Form(...),
    code: str = Form(...),
    university_id: str = Form(None),
    new_university_name: str = Form(None),
    session: Session = Depends(get_session),
):
    """Update course name, code, and university. Create university if needed."""
    cm = CourseManager(session)
    course = _resolve_course(cm, course_identifier)
    if not course:
        return HTMLResponse("Course not found", status_code=404)
    try:
        # Handle university_id: if 'add_new', use new_university_name
        uni_id = None
        if university_id and university_id != "add_new":
            try:
                uni_id = int(university_id)
            except Exception:
                uni_id = None
        elif university_id == "add_new" and new_university_name:
            uni_id = None  # Will be handled in update_course
        if course.id is None:
            return HTMLResponse("Course ID is missing.", status_code=400)
        updated = cm.update_course(
            course.id,
            name=name,
            code=code,
            university_id=uni_id,
            new_university_name=new_university_name if university_id == "add_new" else None,
        )
    except Exception as ex:
        return HTMLResponse(f"Update failed: {ex}", status_code=400)
    sess = request.session
    if updated and sess.get("current_course_id") == getattr(updated, "id", None):
        sess["current_course_name"] = getattr(updated, "name", None)
        sess["current_course_code"] = getattr(updated, "code", None)
        sess["flash_message"] = "Course details updated."
    target = f"/courses/{(getattr(updated, 'code', None) or getattr(updated, 'id', ''))}"
    if request.headers.get("HX-Request"):
        resp = HTMLResponse()
        resp.headers["HX-Redirect"] = target
        return resp
    return RedirectResponse(url=target, status_code=303)


@router.post("/courses/{course_identifier}/settings/update", response_class=HTMLResponse)
def update_course_settings_view(
    request: Request,
    course_identifier: str,
    gpa_scale: int = Form(...),
    return_to: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    """Update per-course GPA scale and sync the linked grading scale rows."""
    cm = CourseManager(session)
    course = _resolve_course(cm, course_identifier)
    if not course or course.id is None:
        return HTMLResponse("Course not found", status_code=404)
    # Validate dynamically: accepted values are the integer max GPA points of
    # any scale present in the grade_scales table, so newly added scales are
    # accepted without any code changes.
    scale_rows = session.exec(
        select(func.max(GradeScale.gpa_point))
        .where(GradeScale.band_type == "both")
        .group_by(GradeScale.scale_name)
    ).all()
    valid_gpa_scales = {int(v) for v in scale_rows if v is not None}
    if gpa_scale not in valid_gpa_scales:
        return HTMLResponse(
            f"gpa_scale {gpa_scale} is not a recognised scale maximum "
            f"(valid: {sorted(valid_gpa_scales)})",
            status_code=422,
        )
    cm.update_gpa_scale(course.id, gpa_scale)
    # Honour an explicit return_to path supplied by the caller (e.g. "/profile");
    # fall back to the canonical course detail page.
    canonical = course.code.strip() if course.code else course_identifier
    target = return_to if (return_to and return_to.startswith("/")) else f"/courses/{canonical}"
    if request.headers.get("HX-Request"):
        resp = HTMLResponse()
        resp.headers["HX-Redirect"] = target
        return resp
    return RedirectResponse(url=target, status_code=303)


@router.post("/courses/{course_identifier}/delete", response_class=HTMLResponse)
def delete_course_view(
    request: Request,
    course_identifier: str,
    session: Session = Depends(get_session),
):
    """Delete a course and redirect to courses list; clears active banner if needed."""
    cm = CourseManager(session)
    course = _resolve_course(cm, course_identifier)
    if not course:
        return HTMLResponse("Course not found", status_code=404)
    cm.delete_course(course.id)  # type: ignore[arg-type]
    # Clear session if active
    sess = request.session
    if sess.get("current_course_id") == course.id:
        sess.pop("current_course_id", None)
        sess.pop("current_course_name", None)
        sess.pop("current_course_code", None)
        sess["flash_message"] = "Course deleted."
    target = "/courses"
    if request.headers.get("HX-Request"):
        resp = HTMLResponse()
        resp.headers["HX-Redirect"] = target
        return resp
    return RedirectResponse(url=target, status_code=303)
