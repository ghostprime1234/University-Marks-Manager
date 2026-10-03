from __future__ import annotations
"""HTML view routes rendering Jinja templates (spaces only)."""

from typing import Any, Optional, List, cast
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from sqlmodel import Session, select, col, Table
from sqlalchemy.orm import selectinload
import re

from src.presentation.api.deps import get_session
from src.infrastructure.db.models import (
    Semester,
    Subject,
    SubjectPrerequisite,
    SubjectRule,
    UserCourse,
    Course,
)
from .template_helpers import _render
from .assignment_views import assignment_router
from .exam_views import exam_router
from .semester_views import semester_router, build_semester_context
from .subject_views import subject_router, build_subject_context, build_subject_page_context
from .course_views import router as course_router
from .settings_views import router as settings_router
from .auth_views import router as auth_router
from .types import IndexContext
from src.core.auth import ensure_uuid, get_current_user_token
from src.core.services.semester_manager import SemesterManager
from src.core.services.course_manager import CourseManager
from src.core.services.grade_calculator import GradeCalculator, process_assessments, _round2dp

async def verify_user(request: Request):
    user_id = None
    try:
        user_data = await get_current_user_token(request)
        user_id = ensure_uuid(user_data["sub"])
        request.state.user = user_data
        request.state.user_id = user_id
    except HTTPException:
        pass

    if not user_id:
        # For normal browser requests, redirect to login
        hdr = request.headers.get
        is_htmx = hdr("hx-request") == "true"
        is_xrw = hdr("x-requested-with") == "XMLHttpRequest"
        accept = (hdr("accept") or "")
        wants_json = "application/json" in accept
        # If this appears to be an AJAX/HTMX/fetch request, return 401 JSON
        if is_htmx or is_xrw or wants_json:
            raise HTTPException(status_code=401, detail="Not Logged In")
        # Otherwise, redirect the browser to the login page
        raise HTTPException(status_code=307, detail="Not Logged In", headers={"location": "/login"})

# Create router with verification dependency (but will exclude auth routes)
views = APIRouter()
views.include_router(auth_router, prefix="", tags=["auth"])
views.include_router(assignment_router, prefix="/semester/{semester}/subject/{code}", tags=["assignments"], dependencies=[Depends(verify_user)])
views.include_router(exam_router, prefix="/semester/{semester}/subject/{code}", tags=["exams"], dependencies=[Depends(verify_user)])
views.include_router(semester_router, prefix="/semester", tags=["semesters"], dependencies=[Depends(verify_user)])
views.include_router(subject_router, prefix="", tags=["subjects"], dependencies=[Depends(verify_user)])
views.include_router(course_router, prefix="", tags=["courses"], dependencies=[Depends(verify_user)])
views.include_router(settings_router, prefix="", tags=["settings"], dependencies=[Depends(verify_user)])

def _render_home_body(request: Request, session: Session, parsed_year: Optional[int]) -> HTMLResponse:
    """Render Home with a concrete parsed_year (None means All years)."""
    sm = SemesterManager(session)
    cm = CourseManager(session)
    
    # Get user_id from session
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        raise HTTPException(status_code=307, detail="Not Logged In", headers={"location": "/login"})
    user_id = ensure_uuid(user_id_val)
    
    # Get only courses for this user
    user_courses = session.exec(
        select(UserCourse).where(UserCourse.user_id == user_id)
    ).all()
    user_course_ids = [uc.course_id for uc in user_courses]
    
    if not user_course_ids:
        # No courses assigned to user - show helpful message
        ctx: IndexContext = {
            "semesters": [],
            "years": [],
            "selected_year": parsed_year,
            "current_year": str(datetime.now().year),
            "flash_message": "Welcome! You haven't added any courses yet. Visit your profile to add courses.",
            "course_filter": None,
            "wam": None,
            "gpa": None,
            "grade_counts": None,
            "no_courses_warning": True,
            "user_courses": [],
            "username": request.session.get("username"),
            "subjects_by_semester": {},
        }
        return _render(request, "index.html", ctx)
    
    # Verify and get active course - must be in user's courses
    sess = request.session
    active_course_id = sess.get("current_course_id")
    course_id = None
    
    if active_course_id is not None:
        try:
            course_id = int(str(active_course_id).strip())
            # Verify user has access to this course
            if course_id not in user_course_ids:
                course_id = None
        except Exception:
            course_id = None
    
    # If no valid active course, select the default or first available
    if course_id is None:
        default_uc = session.exec(
            select(UserCourse).where(
                UserCourse.user_id == user_id,
                UserCourse.is_default == True
            )
        ).first()
        
        if default_uc:
            course_id = default_uc.course_id
        elif user_course_ids:
            course_id = user_course_ids[0]
    
    # Update session with active course info
    if course_id is not None:
        course = session.get(Course, course_id)
        if course:
            sess["current_course_id"] = course_id
            sess["current_course_name"] = course.name
            sess["current_course_code"] = course.code

    if course_id is not None:
        all_semesters = sm.get_semesters_for_course(course_id)
        years = sm.get_distinct_years_for_course(course_id)
    else:
        all_semesters = []
        years = []

    # If the selected year is not in the available years for this course, fallback to All
    if parsed_year is not None and parsed_year not in years:
        parsed_year = None

    display_semesters = [s for s in all_semesters if (parsed_year is None or int(s.year) == int(parsed_year))]
    # Pop any one-time flash message (set after selecting a course)
    flash_message = request.session.pop("flash_message", None)
    # Fallback: if URL indicates a selection just happened, synthesize a message
    if (flash_message is None) and (request.query_params.get("selected") == "1"):
        cname = request.session.get("current_course_name")
        ccode = request.session.get("current_course_code")
        if cname or ccode:
            if cname and ccode:
                flash_message = f"Active course set to {cname} ({ccode})."
            else:
                flash_message = f"Active course set."

    # Helpful context for template to show filter banner
    course_filter = None
    if course_id is not None:
        course_filter = {
            "name": sess.get("current_course_name"),
            "code": sess.get("current_course_code"),
        }

    # Get user's courses for the course selector in template
    user_courses_data = []
    for uc in user_courses:
        course = session.get(Course, uc.course_id)
        if course:
            user_courses_data.append({
                "id": uc.id,
                "course": course,
                "is_default": uc.is_default,
                "is_active": uc.course_id == course_id
            })

    # Calculate grades fresh from database each time (never cached in session)
    gc = GradeCalculator(session)
    wam = gc.calculate_wam(course_id)
    gpa = gc.calculate_gpa(course_id)
    grade_counts = gc.calculate_grade_counts(course_id)

    # Build subject schedule: map semester_id -> list of subjects
    subjects_by_semester: dict[int, list] = {}
    subjects_table = cast(Table, getattr(Subject, "__table__"))
    for sem in display_semesters:
        sem_subjects = session.exec(
            select(Subject)
            .where(Subject.semester_id == sem.id)
            .order_by(subjects_table.c.subject_code.asc())
        ).all()
        if sem.id is not None:
            subjects_by_semester[sem.id] = list(sem_subjects)

    ctx = {
        "semesters": display_semesters,
        "years": years,
        "selected_year": parsed_year,
        "current_year": str(datetime.now().year),
        "flash_message": flash_message,
        "course_filter": course_filter,
        "wam": wam,
        "gpa": gpa,
        "grade_counts": grade_counts,
        "user_courses": user_courses_data,
        "username": sess.get("username"),
        "no_courses_warning": False,
        "subjects_by_semester": subjects_by_semester,
    }
    return _render(request, "index.html", ctx)


@views.api_route("/", methods=["GET"], response_class=HTMLResponse)
def home(request: Request, year: Optional[str] = None, session: Session = Depends(get_session)) -> Response:
    """Landing route that supports legacy query param and redirects to pretty URLs.

    - /?year=2025 -> 303 /year/2025 (preserving selected=1)
    - /?year= or /?year=all -> 303 /all (preserving selected=1)
    - / with no year -> if current year exists -> 303 /year/<current>, else render All
    """
    # Check if user is logged in
    if not request.session.get("user_id"):
        return RedirectResponse(url="/login", status_code=303)
    
    qp = request.query_params
    selected_suffix = "?selected=1" if qp.get("selected") == "1" else ""
    if "year" in qp:
        y = (qp.get("year") or "").strip()
        if y == "" or y.lower() == "all":
            return cast(HTMLResponse, RedirectResponse(url=f"/all{selected_suffix}", status_code=303))
        if y.isdigit():
            return cast(HTMLResponse, RedirectResponse(url=f"/year/{int(y)}{selected_suffix}", status_code=303))
        # invalid -> treat as All
        return cast(HTMLResponse, RedirectResponse(url=f"/all{selected_suffix}", status_code=303))

    # No year provided: prefer current year if any data exists, else All
    sm = SemesterManager(session)
    cm = CourseManager(session)
    user_id_val = request.session.get("user_id")
    if not user_id_val:
        return RedirectResponse(url="/login", status_code=303)
    user_id = ensure_uuid(user_id_val)
    # Determine if a course is selected
    sess = request.session
    active_course_id = sess.get("current_course_id")
    course_id: Optional[int] = None
    if active_course_id is not None:
        try:
            course_id = int(str(active_course_id).strip())
        except Exception:
            course_id = None
    if course_id is not None:
        # Type guard: create non-optional variable for function calls
        course_id_int: int = course_id
        years = sm.get_distinct_years_for_course(course_id_int)
        semesters = sm.get_semesters_for_course(course_id_int)
    else:
        # Check if any courses exist
        all_courses = cm.get_all_courses()
        if all_courses:
            # Auto-select the first (default) course
            default_course = all_courses[0]
            course_id = default_course.id
            if course_id is not None:
                sess["current_course_id"] = course_id
                sess["current_course_name"] = default_course.name
                sess["current_course_code"] = default_course.code
                years = sm.get_distinct_years_for_course(course_id)
                semesters = sm.get_semesters_for_course(course_id)
            else:
                years = sm.get_distinct_years()
                semesters = sm.get_all_semesters()
        else:
            years = sm.get_distinct_years()
            semesters = sm.get_all_semesters()
    now_year = int(datetime.now().year)
    # Only redirect to current year if there are semesters for that year
    if now_year in years and any(int(s.year) == now_year for s in semesters):
        return cast(HTMLResponse, RedirectResponse(url=f"/year/{now_year}{selected_suffix}", status_code=303))
    elif years:
        first_year = min(years)
        return cast(HTMLResponse, RedirectResponse(url=f"/year/{first_year}{selected_suffix}", status_code=303))
    return _render_home_body(request, session, None)


def _infer_level_from_text(text: str) -> Optional[int]:
    """Infer the level (1,2,3,4,...) from the numeric part in subject code.
    
    For undergraduate (first digit 1-5): uses the first digit as level
    For postgraduate (first digit 6+): uses the second digit as level to separate streams
    
    Examples: 
    - CSIT101 -> 101 -> first digit 1 -> level 1
    - CSIT201 -> 201 -> first digit 2 -> level 2
    - MADS6001 -> 6001 -> first digit 6, second digit 0 -> level 0 (or 10)
    - MADS6101 -> 6101 -> first digit 6, second digit 1 -> level 1 (or 11)
    """
    if not text:
        return None
    # Find the first sequence of one or more digits
    code_match = re.search(r"(\d+)", text)
    if code_match:
        try:
            digit_sequence = code_match.group(1)
            first_digit = int(digit_sequence[0])
            
            # For postgraduate (6+), use second digit for finer separation
            if first_digit >= 6:
                if len(digit_sequence) >= 2:
                    second_digit = int(digit_sequence[1])
                    # Return level as 10 + second_digit to keep postgrad separate from undergrad
                    return 10 + second_digit
                else:
                    return 10
            else:
                # For undergraduate, use first digit
                return max(1, first_digit)
        except (ValueError, IndexError):
            pass
    return None


@views.get("/year/{year}/prerequisite_graph/json", response_class=JSONResponse)
def prerequisite_graph_all_subjects(
    
    year: int,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """
    Generate a prerequisite graph visualization for all subjects in a given academic year.
    This function retrieves all subjects for the specified year (optionally filtered by the
    user's selected course) and constructs a directed graph representing prerequisite and
    corequisite relationships between subjects.
    Args:
        year: The academic year for which to retrieve subjects and prerequisites.
        request: The HTTP request object containing session data.
        session: SQLAlchemy session for database queries (injected via FastAPI Depends).
    Returns:
        JSONResponse: A JSON object containing:
            - nodes: List of node dictionaries with properties:
                - id: Unique identifier (positive for real subjects, negative for synthetic)
                - label: Subject code or custom text label
                - main: Boolean flag (always False in current implementation)
                - corequisite: Boolean flag (always False in current implementation)
                - level: Visual grouping level for layout
                - x, y: Calculated 2D coordinates for graph visualization
                - physics: Boolean flag for physics engine (always False)
            - edges: List of edge dictionaries with properties:
                - from: Prerequisite subject/node id
                - to: Dependent subject id
                - type: Either "prerequisite" or "corequisite"
    Process:
        1. Determines the active course (auto-selects first if none specified)
        2. Retrieves semesters for the given year/course
        3. Fetches all subjects in those semesters
        4. Extracts prerequisite relationships (both subject links and custom text patterns)
        5. Creates synthetic nodes for external prerequisites matching subject code patterns
        6. Infers academic level from subject codes
        7. Calculates node positions using level-based visual grouping
        8. Returns empty graph if no subjects or relationships exist
    """
    sess = request.session
    cm = CourseManager(session)
    active_course_id = sess.get("current_course_id")
    course_id = None
    if active_course_id is not None:
        try:
            course_id = int(str(active_course_id).strip())
        except Exception:
            course_id = None

    # If no course selected, auto-select the first (default) course
    if course_id is None:
        all_courses = cm.get_all_courses()
        if all_courses:
            default_course = all_courses[0]
            course_id = default_course.id
            sess["current_course_id"] = course_id
            sess["current_course_name"] = default_course.name
            sess["current_course_code"] = default_course.code

    if course_id is not None:
        semesters = session.exec(
            select(Semester).where(Semester.year == year, Semester.course_id == course_id)
        ).all()
    else:
        semesters = session.exec(select(Semester).where(Semester.year == year)).all()
    
    # FIX: Filter None explicitly so type becomes list[int]
    semester_ids = [s.id for s in semesters if s.id is not None]
    if not semester_ids:
        return JSONResponse({"nodes": [], "edges": []})

    # FIX: Use col() for .in_()
    subjects = session.exec(select(Subject).where(col(Subject.semester_id).in_(semester_ids))).all()
    if not subjects:
        return JSONResponse({"nodes": [], "edges": []})

    subject_lookup = {s.id: s for s in subjects if s.id is not None}
    subject_ids = list(subject_lookup.keys())

    # FIX: Use col() for .in_()
    links = session.exec(
        select(SubjectPrerequisite).where(
            col(SubjectPrerequisite.subject_id).in_(subject_ids)
        )
    ).all()

    edges: list[dict] = []
    # Start with all subjects as nodes (not just those with prerequisites)
    subject_node_ids: set[int] = set(subject_ids)
    # Synthetic nodes for short custom-text prerequisites (e.g. external 5000-level units)
    custom_label_to_id: dict[str, int] = {}
    custom_nodes: dict[int, str] = {}
    next_custom_id = -1
    
    for link in links:
        # FIX: Check for None before casting to int
        if link.subject_id is None:
            continue
            
        subject_id = int(link.subject_id)
        
        # First handle subject-to-subject prerequisite relationships
        if link.prerequisite_subject_id is not None:
            try:
                prerequisite_id: int | None = int(link.prerequisite_subject_id)
            except (TypeError, ValueError):
                continue
            subject_node_ids.add(subject_id)
            subject_node_ids.add(cast(int, prerequisite_id))
            edges.append(
                {
                    "from": prerequisite_id,
                    "to": subject_id,
                    "type": "corequisite" if link.is_corequisite else "prerequisite",
                }
            )
        else:
            # If there is no linked subject but we have custom_text, only
            # create a node when we can extract a subject-like code from it.
            # This avoids rendering generic credit requirements such as
            # "18cp @ 200 level CSIT" and focuses the graph on concrete
            # prerequisite subjects (e.g. "CSC5020").
            requirement_text = (link.custom_text or "").strip()
            if not requirement_text:
                continue

            # Extract the first token that looks like a subject code, e.g.
            # CSIT110, CSC5020, STAT5000. If none is found we skip.
            code_match = re.search(r"\b[A-Za-z]{3,5}\d{3,4}\b", requirement_text)
            if not code_match:
                continue
            label = code_match.group(0)

            # Check if this code matches an existing subject in the database
            matching_subject = session.exec(
                select(Subject).where(Subject.subject_code == label)
            ).first()
            
            if matching_subject and matching_subject.id is not None:
                # Use the existing subject ID instead of creating a synthetic node
                prerequisite_id = int(matching_subject.id)
                if prerequisite_id not in subject_lookup:
                    subject_lookup[prerequisite_id] = matching_subject
            else:
                # Create a synthetic node for external prerequisites
                prerequisite_id = custom_label_to_id.get(label)
                if prerequisite_id is None:
                    prerequisite_id = next_custom_id
                    next_custom_id -= 1
                    custom_label_to_id[label] = prerequisite_id
                    custom_nodes[prerequisite_id] = label

            subject_node_ids.add(subject_id)
            subject_node_ids.add(prerequisite_id)
            edges.append(
                {
                    "from": prerequisite_id,
                    "to": subject_id,
                    "type": "corequisite" if link.is_corequisite else "prerequisite",
                }
            )

    nodes: list[dict] = []
    # Ensure we have Subject rows for all real subject IDs (exclude synthetic negatives)
    missing_ids = {subject_id for subject_id in subject_node_ids if subject_id > 0} - set(subject_lookup.keys())
    if missing_ids:
        # FIX: cast set to list explicitly for .in_()
        extra = session.exec(select(Subject).where(col(Subject.id).in_(list(missing_ids)))).all()
        for extra_sub in extra:
            if extra_sub.id is not None:
                subject_lookup[extra_sub.id] = extra_sub

    # Build label and initial level for every node. For synthetic
    # custom-text nodes, use the custom label directly.

    labels: dict[int, str] = {}
    levels: dict[int, int] = {}
    for subject_id in sorted(subject_node_ids):
        if subject_id in custom_nodes:
            code = custom_nodes[subject_id]
        else:
            s = subject_lookup.get(subject_id)
            if not s:
                continue
            code = str(getattr(s, "subject_code", ""))
        labels[subject_id] = code
        inferred = _infer_level_from_text(code)
        levels[subject_id] = inferred if inferred is not None else 0

    # Adjust levels so that a prerequisite is always placed at least
    # one visual band above any subject that depends on it. This fixes
    # cases like CSIT110 → CSIT121 where the codes imply the same
    # level but we still want CSIT110 to appear above CSIT121.
    if edges:
        for _ in range(len(levels)):
            changed = False
            for edge in edges:
                from_id = edge.get("from")
                to_id = edge.get("to")
                if from_id not in levels or to_id not in levels:
                    continue
                if levels[from_id] >= levels[to_id]:
                    levels[from_id] = levels[to_id] - 1
                    changed = True
            if not changed:
                break

    if not levels:
        return JSONResponse({"nodes": [], "edges": edges})

    # Remap (possibly negative) logical levels to compact visual
    # bands 0,1,2,... so spacing between rows stays tight.
    unique_levels = sorted(set(levels.values()))
    level_to_visual = {lvl: idx for idx, lvl in enumerate(unique_levels)}

    # Group nodes by visual level for positioning.
    nodes_by_visual: dict[int, list[int]] = {}
    for subject_id in sorted(levels.keys()):
        visual_level = level_to_visual[levels[subject_id]]
        nodes_by_visual.setdefault(visual_level, []).append(subject_id)

    # Decide orientation based on the number of levels.
    num_levels = max(1, len(nodes_by_visual))
    max_nodes_in_level = max((len(v) for v in nodes_by_visual.values()), default=1)
    max_vertical_levels = 5  # up to this many levels: vertical; above: horizontal

    # Base spacing: larger gap between levels than between nodes.
    level_separation = 200.0
    node_spacing = 150.0

    nodes_orientation_vertical = num_levels <= max_vertical_levels

    nodes = []
    for visual_level, node_ids in sorted(nodes_by_visual.items()):
        num_nodes = len(node_ids)
        for index, subject_id in enumerate(node_ids):
            if nodes_orientation_vertical:
                y = visual_level * level_separation
                x = (index - (num_nodes - 1) / 2) * node_spacing
            else:
                x = visual_level * level_separation
                y = (index - (num_nodes - 1) / 2) * node_spacing

            s = subject_lookup.get(subject_id)
            is_completed = bool(getattr(s, "is_finalized", False)) if s else False
            is_in_progress = False

            if s and not is_completed:
                # EXPLICIT LOOKUP: Bypasses lazy loading by checking the tables directly via SQL
                # Imports inside the loop safely to avoid circular dependencies if any exist
                from src.infrastructure.db.models import Assignment, Examination
                
                # Check if any assignment marks exist for this subject id
                has_assignment_marks = session.exec(
                    select(Assignment)
                    .where(Assignment.subject_id == s.id, col(Assignment.weighted_mark) > 0)
                ).first() is not None

                # Check if any exam marks exist for this subject id
                has_exam_marks = session.exec(
                    select(Examination)
                    .where(Examination.subject_id == s.id, col(Examination.exam_mark) > 0)
                ).first() is not None

                if has_assignment_marks or has_exam_marks:
                    is_in_progress = True

            node: dict = {
                "id": subject_id,
                "label": labels[subject_id],
                "main": False,
                "corequisite": False,
                "level": visual_level,
                "x": x,
                "y": y,
                "physics": False,
                "is_completed": is_completed,
                "is_in_progress": is_in_progress
            }
            nodes.append(node)

    return JSONResponse({"nodes": nodes, "edges": edges})

@views.get("/year/{year}", response_class=HTMLResponse)
def home_year(request: Request, year: int, session: Session = Depends(get_session)) -> HTMLResponse:
    """
    Short description.

    Args:
        request: Description.
        year: Description.
        session: Description.

    Returns:
        Description.

    Raises:
        Description.
    """
    return _render_home_body(request, session, year)


@views.head("/year/{year}")
def home_year_head(request: Request, year: int, session: Session = Depends(get_session)):
    """HEAD variant for year overview: return same status/headers as GET but no body.

    This reuses the GET view to compute the same headers so HEAD checks succeed.
    """
    resp = home_year(request=request, year=year, session=session)
    headers = dict(resp.headers) if resp is not None else {}

    if not request.session.get("user_id"):
        return RedirectResponse(url="/login", status_code=303)
    return Response(status_code=resp.status_code if resp is not None else 200, headers=headers)


@views.get("/all", response_class=HTMLResponse)
def home_all(request: Request, session: Session = Depends(get_session)) -> Response:
    """Render the All years page, or redirect to current/first year if appropriate."""
    sm = SemesterManager(session)
    sess = request.session
    if not sess.get("user_id"):
        return RedirectResponse(url="/login", status_code=303)
    active_course_id = sess.get("current_course_id")
    course_id = None
    if active_course_id is not None:
        try:
            course_id = int(str(active_course_id).strip())
        except Exception:
            course_id = None
    if course_id is not None:
        years = sm.get_distinct_years_for_course(course_id)
        semesters = sm.get_semesters_for_course(course_id)
    else:
        years = sm.get_distinct_years()
        semesters = sm.get_all_semesters()
    now_year = int(datetime.now().year)
    selected_suffix = "?selected=1" if request.query_params.get("selected") == "1" else ""
    # Only redirect if this is a legacy redirect (e.g., has ?redirect=1)
    if request.query_params.get("redirect") == "1":
        if now_year in years and any(int(s.year) == now_year for s in semesters):
            return cast(HTMLResponse, RedirectResponse(url=f"/year/{now_year}{selected_suffix}", status_code=303))
        elif years:
            first_year = min(years)
            return cast(HTMLResponse, RedirectResponse(url=f"/year/{first_year}{selected_suffix}", status_code=303))
    # Otherwise, render all years view (even if empty)
    return _render_home_body(request, session, None)

@views.get("/all/prerequisite_graph/json", response_class=JSONResponse)
def prerequisite_graph_all_years(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Return prerequisite graph data for all subjects in the active course (all years)."""
    sess = request.session
    active_course_id = sess.get("current_course_id")
    course_id = None
    if active_course_id is not None:
        try:
            course_id = int(str(active_course_id).strip())
        except Exception:
            course_id = None

    if course_id is not None:
        semesters = session.exec(
            select(Semester).where(Semester.course_id == course_id)
        ).all()
    else:
        semesters = session.exec(select(Semester)).all()

    # FIX: Filter None explicitly
    semester_ids = [s.id for s in semesters if s.id is not None]
    if not semester_ids:
        return JSONResponse({"nodes": [], "edges": []})

    # FIX: Use col()
    subjects = session.exec(select(Subject).where(col(Subject.semester_id).in_(semester_ids))).all()
    if not subjects:
        return JSONResponse({"nodes": [], "edges": []})

    subject_lookup = {s.id: s for s in subjects if s.id is not None}
    subject_ids = list(subject_lookup.keys())

    # FIX: Use col()
    links = session.exec(
        select(SubjectPrerequisite).where(
            col(SubjectPrerequisite.subject_id).in_(subject_ids)
        )
    ).all()

    edges: list[dict] = []
    # Start with all subjects as nodes (not just those with prerequisites)
    subject_node_ids: set[int] = set(subject_ids)
    # Synthetic nodes for custom-text prerequisites
    custom_label_to_id: dict[str, int] = {}
    custom_nodes: dict[int, str] = {}
    next_custom_id = -1
    
    for link in links:
        # FIX: Check for None
        if link.subject_id is None:
            continue
        subject_id = int(link.subject_id)

        # Only include subject-to-subject prerequisite relationships
        if link.prerequisite_subject_id is not None:
            try:
                prerequisite_id: int | None = int(link.prerequisite_subject_id)
            except (TypeError, ValueError):
                continue
            subject_node_ids.add(subject_id)
            subject_node_ids.add(cast(int, prerequisite_id))
            edges.append(
                {
                    "from": prerequisite_id,
                    "to": subject_id,
                    "type": "corequisite" if link.is_corequisite else "prerequisite",
                }
            )
        else:
            # Custom free-text prerequisite: only include when a subject-like
            # code can be extracted (e.g. CSIT110, CSC5020). This skips
            # generic requirements such as "18cp @ 200 level CSIT".
            requirement_text = (link.custom_text or "").strip()
            if not requirement_text:
                continue

            code_match = re.search(r"\b[A-Za-z]{3,5}\d{3,4}\b", requirement_text)
            if not code_match:
                continue
            label = code_match.group(0)

            prerequisite_id = custom_label_to_id.get(label)
            if prerequisite_id is None:
                prerequisite_id = next_custom_id
                next_custom_id -= 1
                custom_label_to_id[label] = prerequisite_id
                custom_nodes[prerequisite_id] = label
            subject_node_ids.add(subject_id)
            subject_node_ids.add(prerequisite_id)
            edges.append(
                {
                    "from": prerequisite_id,
                    "to": subject_id,
                    "type": "corequisite" if link.is_corequisite else "prerequisite",
                }
            )

    nodes: list[dict] = []
    missing_ids = subject_node_ids - set(subject_lookup.keys())
    if missing_ids:
        # FIX: use col() and explicit list cast
        extra = session.exec(select(Subject).where(col(Subject.id).in_(list(missing_ids)))).all()
        for extra_sub in extra:
            if extra_sub.id is not None:
                subject_lookup[extra_sub.id] = extra_sub

    # Build label and initial level for every node (using either the
    # subject_code or the synthetic custom-text label).
    labels: dict[int, str] = {}
    levels: dict[int, int] = {}
    for subject_id in sorted(subject_node_ids):
        if subject_id in custom_nodes:
            code = custom_nodes[subject_id]
        else:
            s = subject_lookup.get(subject_id)
            if not s:
                continue
            code = str(getattr(s, "subject_code", ""))
        labels[subject_id] = code
        inferred = _infer_level_from_text(code)
        levels[subject_id] = inferred if inferred is not None else 0

    # Ensure prerequisites are always at least one level above any
    # subjects that depend on them, even when the codes imply the
    # same level.
    if edges:
        for _ in range(len(levels)):
            changed = False
            for edge in edges:
                from_id = edge.get("from")
                to_id = edge.get("to")
                if from_id not in levels or to_id not in levels:
                    continue
                if levels[from_id] >= levels[to_id]:
                    levels[from_id] = levels[to_id] - 1
                    changed = True
            if not changed:
                break

    if not levels:
        return JSONResponse({"nodes": [], "edges": edges})

    # Map logical levels (which may be negative) to contiguous visual
    # bands 0,1,2,... for a compact layout.
    unique_levels = sorted(set(levels.values()))
    level_to_visual = {lvl: idx for idx, lvl in enumerate(unique_levels)}

    nodes_by_visual: dict[int, list[int]] = {}
    for subject_id in sorted(levels.keys()):
        visual_level = level_to_visual[levels[subject_id]]
        nodes_by_visual.setdefault(visual_level, []).append(subject_id)

    # Orientation: vertical for a small number of levels, horizontal
    # (levels left-to-right) when the graph is deep.
    num_levels = max(1, len(nodes_by_visual))
    max_nodes_in_level = max((len(v) for v in nodes_by_visual.values()), default=1)
    max_vertical_levels = 5

    level_separation = 200.0
    node_spacing = 150.0

    nodes_orientation_vertical = num_levels <= max_vertical_levels

    nodes = []
    for visual_level, node_ids in sorted(nodes_by_visual.items()):
        num_nodes = len(node_ids)
        for index, subject_id in enumerate(node_ids):
            if nodes_orientation_vertical:
                y = visual_level * level_separation
                x = (index - (num_nodes - 1) / 2) * node_spacing
            else:
                x = visual_level * level_separation
                y = (index - (num_nodes - 1) / 2) * node_spacing

            s = subject_lookup.get(subject_id)
            is_completed = bool(getattr(s, "is_finalized", False)) if s else False
            is_in_progress = False

            if s and not is_completed:
                # EXPLICIT LOOKUP: Bypasses lazy loading by checking the tables directly via SQL
                # Imports inside the loop safely to avoid circular dependencies if any exist
                from src.infrastructure.db.models import Assignment, Examination
                
                # Check if any assignment marks exist for this subject id
                has_assignment_marks = session.exec(
                    select(Assignment)
                    .where(Assignment.subject_id == s.id, col(Assignment.weighted_mark) > 0)
                ).first() is not None

                # Check if any exam marks exist for this subject id
                has_exam_marks = session.exec(
                    select(Examination)
                    .where(Examination.subject_id == s.id, col(Examination.exam_mark) > 0)
                ).first() is not None

                if has_assignment_marks or has_exam_marks:
                    is_in_progress = True

            node: dict = {
                "id": subject_id,
                "label": labels[subject_id],
                "main": False,
                "corequisite": False,
                "level": visual_level,
                "x": x,
                "y": y,
                "physics": False,
                "is_completed": is_completed,
                "is_in_progress": is_in_progress
            }
            nodes.append(node)

    return JSONResponse({"nodes": nodes, "edges": edges})

@views.get("/year/{year}/semester/{semester}", response_class=HTMLResponse)
def semester_detail_pretty(
    request: Request,
    year: int,
    semester: str,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render the semester overview page for a given year and semester.

    This pretty URL replaces legacy redirects and returns a rendered
    `semester.html` with subject summaries for the selected term.
    """
    ctx = build_semester_context(session, semester=semester, year=str(year))
    # If there are no subjects/semester, still render the page with empty lists
    # build_semester_context always returns a context dict
    return _render(request, "semester.html", ctx)


@views.head("/year/{year}/semester/{semester}/subject/{code}", response_class=HTMLResponse)
@views.get("/year/{year}/semester/{semester}/subject/{code}", response_class=HTMLResponse)
def subject_detail_pretty(
        request: Request,
        year: str,
        semester: str,
        code: str,
        session: Session = Depends(get_session),
) -> HTMLResponse:
        """
        Render a subject detail HTML page with optional projected-mark query parameters.

        This view reads optional query parameters from the incoming request to support
        on-the-fly projections and then builds a rendering context for the subject page.

        Behavior:
        - Reads the following optional query parameters from request.query_params:
                - "exam_weight": parsed to float when present and non-empty; invalid values
                    are ignored and cause a one-time flash message to be written to the session
                    (if possible).
                - "final_total": forwarded as-is (string) to the context builder.
                - "total_mark": forwarded as-is (string) to the context builder.
                - "return_to": forwarded as-is to the context builder.
        - If "exam_weight" is missing or empty, it is treated as None.
        - Any ValueError during float parsing of exam_weight is caught; parsed_exam_weight
            becomes None and an attempt is made to set request.session["flash_message"]
            with an explanatory message. Errors while writing the flash message are ignored.
        - Calls build_subject_page_context(session, semester, year, code, exam_weight=..., final_total=..., total_mark=..., return_to=...)
            to construct the context used to render the page.
        - If the context builder returns None, the view returns an HTMLResponse with
            a 404 status ("Subject not found").
        - Otherwise the view renders and returns the "subject.html" template using _render.

        Parameters:
        - request (Request): the incoming HTTP request (used for query params and session).
        - year (str): academic year identifier for the subject lookup.
        - semester (str): semester identifier for the subject lookup.
        - code (str): subject/code identifier for the subject lookup.
        - session (Session): database/session dependency (defaults to Depends(get_session)).

        Returns:
        - HTMLResponse: either the rendered subject page (200) or a 404 response when the
            subject is not found.

        subject_ide effects:
        - May set request.session["flash_message"] once when an invalid exam_weight is supplied.
        - Uses dependency injection to acquire a session (get_session).

        Notes:
        - final_total and total_mark are forwarded as provided (no parsing/validation).
        - exam_weight accepts numeric input and is resilient to invalid formats.
        """
        # Support optional query params for projections
        qp = request.query_params
        exam_weight = qp.get("exam_weight")
        final_total = qp.get("final_total")
        total_mark = qp.get("total_mark")
        # Defensive parse for exam_weight
        parsed_exam_weight: Optional[float] = None
        if exam_weight not in (None, ""):
                try:
                        parsed_exam_weight = float(exam_weight)  # type: ignore[arg-type]
                except ValueError:
                        parsed_exam_weight = None
                        # Optional: set a one-time message (shown on pages that render flash_message)
                        try:
                                request.session["flash_message"] = "Ignored invalid exam_weight query parameter."
                        except Exception:
                                pass
        ctx = build_subject_page_context(
        session=session,
        semester=semester,
        year=year,
        code=code,
        exam_weight=parsed_exam_weight,
        final_total=final_total,
        total_mark=total_mark,
        return_to=qp.get("return_to"),
        error_message=qp.get("error"),
         )
        if ctx is None:
            return HTMLResponse("Subject not found", status_code=404)

        subject_obj = ctx.get("subject")

        # Initialize result with defaults so it is never unbound
        result: dict[str, Any] = {"summaries": [], "grade_goals": []}

        # Use 'isinstance' to narrow the type so Pylance knows 'id' exists
        if isinstance(subject_obj, Subject) and subject_obj.id is not None:
            statement = (
                select(Subject)
                .where(Subject.id == subject_obj.id)
                .options(selectinload(cast(Any, Subject.assignments)))
            )
            loaded_subject = session.exec(statement).first()
            if loaded_subject is not None:
                ctx["subject"] = loaded_subject
                subject_obj = loaded_subject
            
            calc = GradeCalculator(session)
            # Now result is properly assigned inside the scope
            result = calc.calculate_subject_summary(cast(Subject, subject_obj))

        ctx["summaries"] = result["summaries"]
        ctx["grade_goals"] = result["grade_goals"]
        if result.get("total_achieved") is not None:
            ctx["total_achieved"] = result["total_achieved"]
        else:
            ctx["total_achieved"] = _round2dp(
                sum(float(row.get("weighted_score") or 0) for row in result["summaries"])
            )
        ctx["remaining_weight"] = result.get("remaining_weight")
        ctx["is_fully_graded"] = result.get("is_fully_graded", False)
        ctx.setdefault("assessment_summary", {"standalone_assessments": [], "grouped_assessments": {}})

        # --- AUTOMATED SINGLE-SUBJECT PREREQUISITE GRAPH GENERATOR ---
        graph_nodes = []
        graph_edges = []

        if isinstance(subject_obj, Subject) and subject_obj.id is not None:
            # 1. Collect the main subject and its immediate prerequisites links
            links = session.exec(
                select(SubjectPrerequisite).where(SubjectPrerequisite.subject_id == subject_obj.id)
            ).all()

            subject_node_ids = {subject_obj.id}
            custom_nodes: dict[int, str] = {}
            next_custom_id = -1

            for link in links:
                if link.subject_id is None:
                    continue
                
                if link.prerequisite_subject_id is not None:
                    prereq_id = int(link.prerequisite_subject_id)
                    subject_node_ids.add(prereq_id)
                    graph_edges.append({
                        "from": prereq_id,
                        "to": subject_obj.id,
                        "type": "corequisite" if link.is_corequisite else "prerequisite"
                    })
                elif link.custom_text:
                    requirement_text = link.custom_text.strip()
                    code_match = re.search(r"\b[A-Za-z]{3,5}\d{3,4}\b", requirement_text)
                    if code_match:
                        label = code_match.group(0)
                        matching_subject = session.exec(select(Subject).where(Subject.subject_code == label)).first()
                        
                        if matching_subject and matching_subject.id is not None:
                            prereq_id = int(matching_subject.id)
                        else:
                            prereq_id = next_custom_id
                            next_custom_id -= 1
                            custom_nodes[prereq_id] = label
                        
                        subject_node_ids.add(prereq_id)
                        graph_edges.append({
                            "from": prereq_id,
                            "to": subject_obj.id,
                            "type": "corequisite" if link.is_corequisite else "prerequisite"
                        })

            # 2. Evaluate labels and color statuses automatically for each gathered node
            for node_id in sorted(subject_node_ids):
                is_completed = False
                is_in_progress = False
                label = "Unknown"

                if node_id in custom_nodes:
                    label = custom_nodes[node_id]
                else:
                    s = session.get(Subject, node_id)
                    if s:
                        label = str(getattr(s, "subject_code", ""))
                        is_completed = bool(getattr(s, "is_finalized", False))
                        
                        if not is_completed:
                            from src.infrastructure.db.models import Assignment, Examination
                            has_assignment_marks = session.exec(
                                select(Assignment).where(Assignment.subject_id == s.id, col(Assignment.weighted_mark) > 0)
                            ).first() is not None
                            has_exam_marks = session.exec(
                                select(Examination).where(Examination.subject_id == s.id, col(Examination.exam_mark) > 0)
                            ).first() is not None
                            
                            if has_assignment_marks or has_exam_marks:
                                is_in_progress = True

                graph_nodes.append({
                    "id": node_id,
                    "label": label,
                    "main": node_id == subject_obj.id,
                    "corequisite": False,
                    "is_completed": is_completed,
                    "is_in_progress": is_in_progress
                })

        ctx["prereq_graph_data"] = {"nodes": graph_nodes, "edges": graph_edges}
        # --- END OF GRAPH GENERATOR ---

        return _render(request, "subject.html", ctx)

@views.get("/year/{year}/semester/{semester}/subject/{code}/settings", response_class=HTMLResponse)
def subject_settings_page(
    request: Request,
    year: str,
    semester: str,
    code: str,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render the dedicated subject settings page."""
    ctx = build_subject_page_context(session, semester=semester, year=year, code=code)
    if ctx is None:
        return HTMLResponse("Subject not found", status_code=404)

    grade_calculator = GradeCalculator(session)
    subject = ctx.get("subject")
    
    if subject:
        subject_obj = cast(Subject, subject)
        
        # 1. Unpack the new dictionary structure from the calculator
        calc_result = grade_calculator.calculate_subject_summary(subject_obj)
        subject_summary_list = calc_result["summaries"]
        grade_goals = calc_result["grade_goals"]

        subject_rules = session.exec(
            select(SubjectRule).where(SubjectRule.subject_id == subject_obj.id)
        ).all() if subject_obj.id is not None else []
        
        # 2. Use the extracted list for the dictionary comprehension
        summary_by_label: dict[str, dict[str, Any]] = {
            str(row["label"]): row for row in subject_summary_list
        }
        
        ctx["subject_rule_summary"] = [
            {
                "rule_label": rule.rule_label,
                "sql_pattern": rule.sql_pattern,
                "max_count": rule.max_count,
                "weight_each": rule.weight_each,
                "core_count": summary_by_label.get(str(rule.rule_label), {}).get("core_count", 0),
                "bonus_count": summary_by_label.get(str(rule.rule_label), {}).get("bonus_count", 0),
                "weighted_score": summary_by_label.get(str(rule.rule_label), {}).get("weighted_score", 0),
            }
            for rule in subject_rules
        ]
        
        # 3. Pass both variables to the template
        ctx["subject_summary"] = subject_summary_list
        ctx["grade_goals"] = grade_goals
        ctx["subject_rules"] = subject_rules
    else:
        ctx["subject_summary"] = []
        ctx["subject_rules"] = []
        ctx["subject_rule_summary"] = []
        ctx["grade_goals"] = []

    return _render(request, "subject_settings.html", ctx)

@views.get("/subjects/{year}/{code}", response_class=HTMLResponse)
def subject_detail_short(
    request: Request,
    year: str,
    code: str,
    semester: Optional[str] = None,
    session: Session = Depends(get_session),
) -> Response:
    """Shorter subject URL. Standard joins used to satisfy Pylance and prevent Cartesian products."""
    qp = request.query_params
    offering = qp.get("offering")
    return_to = qp.get("return_to") or request.session.pop("return_to", None)

    if semester:
        # Redirect to canonical route
        return RedirectResponse(url=f"/year/{year}/semester/{semester}/{code}", status_code=303)

    if offering:
        # Join using the Relationship attribute 'Subject.semester'
        # This is the cleanest way to avoid the "bool" type error
        candidate = session.exec(
            select(Subject)
            .join(Semester) 
            .where(
                Semester.year == year,
                Subject.subject_code == code,
                Semester.name == offering
            )
        ).all()
        if len(candidate) == 1:
            subj = candidate[0]
            sem_obj = session.get(Semester, subj.semester_id)
            if sem_obj:
                ctx = build_subject_page_context(session, semester=sem_obj.name, year=year, code=code, return_to=return_to)
                if ctx:
                    return _render(request, "subject.html", ctx)

        parts = [p for p in offering.split("-") if p]
        if parts:
            for token in parts:
                candidate = session.exec(
                    select(Subject)
                    .join(Semester)
                    .where(
                        Semester.year == year,
                        Subject.subject_code == code,
                        Semester.name == token
                    )
                ).all()
                if len(candidate) == 1:
                    subj = candidate[0]
                    sem_obj = session.get(Semester, subj.semester_id)
                    if sem_obj:
                        ctx = build_subject_page_context(session, semester=sem_obj.name, year=year, code=code, return_to=return_to)
                        if ctx:
                            return _render(request, "subject.html", ctx)

    # Final query also updated to use standard join
    rows = session.exec(
        select(Subject)
        .join(Semester)
        .where(
            Semester.year == year,
            Subject.subject_code == code
        )
    ).all()

    if not rows:
        return HTMLResponse("Subject not found", status_code=404)
    
    if len(rows) == 1:
        subj = rows[0]
        sem_obj = session.get(Semester, subj.semester_id)
        if not sem_obj:
            return HTMLResponse("Subject not found", status_code=404)
        
        ctx = build_subject_page_context(
            session=session,
            semester=sem_obj.name,
            year=year,
            code=code,
            return_to=return_to,
        )
        if ctx is None:
            return HTMLResponse("Subject not found", status_code=404)
        return _render(request, "subject.html", ctx)

    links = []
    for s in rows:
        sem_obj = session.get(Semester, s.semester_id)
        sem_name = sem_obj.name if sem_obj else "Unknown"
        links.append(f"<li><a href='/year/{year}/semester/{sem_name}/subject/{code}'>Semester {sem_name}</a></li>")
    body = f"<h1>Multiple semesters</h1><p>Choose semester for {code} {year}:</p><ul>{''.join(links)}</ul>"
    return HTMLResponse(body)

@views.post("/subjects/{year}/{code}/open")
def subject_open(
    request: Request,
    year: str,
    code: str,
    semester: str = Form(...),
    return_to: Optional[str] = Form(None),
) -> RedirectResponse:
    """Accept a POST that sets a one-time return_to in session and redirects to the canonical subject GET.

    This prevents the return_to token from appearing in the query string while preserving
    the ability for the subject template to render a 'Back to Semester' link.
    """
    if return_to:
        try:
            request.session["return_to"] = return_to
        except Exception:
            pass
    return RedirectResponse(url=f"/year/{year}/semester/{semester}/subject/{code}", status_code=303)


@views.head("/year/{year}/semester/{semester}")
def semester_detail_head(
    request: Request,
    year: int,
    semester: str,
    session: Session = Depends(get_session),
):
    """HEAD variant for semester detail: return same status/headers as GET but no body.

    This delegates to the GET handler and returns headers (no body) so HEAD checks succeed
    and remain in sync with what a GET would return.
    """
    # Reuse the GET view to compute the same response/headers
    resp = semester_detail_pretty(request=request, year=year, semester=semester, session=session)
    headers = dict(resp.headers) if resp is not None else {}
    return Response(status_code=resp.status_code if resp is not None else 200, headers=headers)


__all__ = ["views"]
