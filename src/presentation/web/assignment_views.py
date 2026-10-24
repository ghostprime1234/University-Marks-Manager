from fastapi import Form, Request
import logging
import re
from typing import Any, Optional
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import col, Session, select
from src.infrastructure.db.models import Assignment, ExamSettings, Examination, GradeType, Semester, Subject
from src.presentation.api.deps import get_session
from src.core.services.grade_calculator import GradeCalculator
from urllib.parse import quote_plus, quote
from html import escape as html_escape
import json

assignment_router = APIRouter()
logger = logging.getLogger(__name__)


def _derive_category(assessment: str, category: Optional[str]) -> str:
    if category and category.strip():
        return category.strip()
    derived = re.sub(r"\s*[-_:]*\s*\d+\s*$", "", assessment).strip()
    return derived or assessment

@assignment_router.api_route("/assignment/create", methods=["POST"], response_class=HTMLResponse)
def create_assignment(
    semester: str,
    code: str,
    year: str = Form(...),
    assessment: str = Form(...),
    category: Optional[str] = Form(None),
    # Accept empty strings from form without validation errors; parse manually below
    weighted_mark: Optional[str] = Form(None),
    mark_weight: Optional[str] = Form(None),
    grade_type: str = Form("numeric"),
    is_exam: bool = Form(False),
    exam_type: str = Form("assignment"),
    total_mark: Optional[str] = Form(None),  # propagate desired final total to trigger recompute
    return_to: Optional[str] = Form(None),
    session: Session = Depends(get_session),  # noqa: B008 - FastAPI dependency injection is intended here
):
    """
    Create a new assignment for the subject.
    
    Args:
        semester (str): Semester name.
        code (str): Subject code.
        year (str): Semester year.
        assessment (str): Assignment name/description.
        weighted_mark (Optional[str]): Weighted mark as string.
        mark_weight (Optional[str]): Mark weight as string.
        grade_type (str): Grade type ("numeric", "satisfactory", "unsatisfactory").
        session (Session): Database session dependency.
    
    Raises:
        ValueError: If numeric values are invalid.
    
    Returns:
        RedirectResponse: Redirect to subject detail page.
    """
    unweighted_val = None
    weighted_val = None
    mark_weight_val = None
    logger.info("[DEBUG] Assignment creation requested: code=%s, semester=%s, year=%s, assessment=%s, weighted_mark=%s, mark_weight=%s, grade_type=%s, is_exam=%s", code, semester, year, assessment, weighted_mark, mark_weight, grade_type, is_exam)
    try:
        category_value = _derive_category(assessment, category)
        if grade_type == GradeType.NUMERIC.value:
            try:
                if weighted_mark is not None and weighted_mark != "":
                    weighted_val = float(weighted_mark)
                if mark_weight is not None and mark_weight != "":
                    mark_weight_val = float(mark_weight)
                # Only calculate unweighted if both are provided
                if weighted_val is not None and mark_weight_val is not None and mark_weight_val:
                    unweighted_val = round(weighted_val / mark_weight_val, 4)
            except ValueError:
                weighted_val = None
                mark_weight_val = None
                unweighted_val = None
        if grade_type in (GradeType.SATISFACTORY.value, GradeType.UNSATISFACTORY.value):
            weighted_val = None
            mark_weight_val = None
            unweighted_val = None
        # Resolve subject_id first for normalized lookups
        logger.info("[DEBUG] Looking up subject with: subject_code=%s, semester_name=%s, year=%s", code, semester, year)
        subj = session.exec(
            select(Subject)
            .join(Semester)
            .where(
                Subject.subject_code == code,
                Semester.name == semester,
                Semester.year == int(year),
            )
        ).first()
        logger.info("[DEBUG] Subject lookup result: %s", subj)
        if subj is None:
            logger.error("[DEBUG] Subject not found for code=%s, semester=%s, year=%s", code, semester, year)
            return HTMLResponse("Subject not found for the given code, semester, and year. Cannot create assignment.", status_code=400)
        if getattr(subj, "is_finalized", False):
            return RedirectResponse(
                f"/semester/{semester}/subject/{code}?year={year}&error=Subject+is+finalized.+Uncheck+Finalized+to+modify+assignments.",
                status_code=303,
            )
        subject_id = getattr(subj, "id", None)
        logger.info("[DEBUG] subject_id resolved: %s", subject_id)
        if subject_id is None:
            logger.error("[DEBUG] Subject found but has no valid ID. Database may be corrupted. subj=%s", subj)
            return HTMLResponse("Subject found but has no valid ID. Database may be corrupted. Cannot create assignment.", status_code=500)
        # Check duplicate by (subject_id, assessment)
        existing_assignment = session.exec(
            select(Assignment).where(
                Assignment.subject_id == subject_id,
                Assignment.assessment == assessment,
            )
        ).first()
        logger.info("[DEBUG] Existing assignment lookup: %s", existing_assignment)
        if existing_assignment:
            logger.warning("[DEBUG] Assignment with this name already exists for subject_id=%s, assessment=%s", subject_id, assessment)
            # Redirect back to the subject page with an inline error so the user stays on the page
            msg = quote_plus("An assignment with this name already exists for this subject.")
            return RedirectResponse(
                url=f"/year/{year}/semester/{semester}/subject/{code}?error={msg}",
                status_code=303,
            )
        new_assignment = Assignment(
            subject_id=subject_id,
            assessment=assessment,
            category=category_value,
            # Persist numeric weighted marks as floats; S/U is tracked via grade_type.
            weighted_mark=(weighted_val if (grade_type == GradeType.NUMERIC.value and weighted_val is not None) else None),
            unweighted_mark=unweighted_val,
            mark_weight=mark_weight_val,
            grade_type=grade_type,
            is_exam=is_exam,
        )
        logger.info("[DEBUG] Creating new assignment: %s", new_assignment)
        session.add(new_assignment)
        session.commit()

        # If this assignment is marked as the exam, sync the Examination table immediately
        if is_exam:
            # Use provided exam_type (default to 'assignment', can be 'main')
            exam = session.exec(
                select(Examination).where(
                    (Examination.subject_id == subject_id) & (Examination.exam_type == exam_type)
                )
            ).first()
            exam_mark = new_assignment.weighted_mark if new_assignment.weighted_mark is not None else 0.0
            exam_weight = new_assignment.mark_weight if new_assignment.mark_weight is not None else 0.0
            if exam:
                exam.exam_mark = exam_mark
                exam.exam_weight = exam_weight
            else:
                session.add(
                    Examination(
                        subject_id=subject_id,
                        exam_mark=exam_mark,
                        exam_weight=exam_weight,
                        exam_type=exam_type,
                    )
                )
            session.commit()
    except Exception as e:
        logger.exception("[DEBUG] Exception during assignment creation: %s", e)
        # On server errors, keep user on the page with an inline error
        msg = quote_plus("Internal error while creating assignment. Please try again.")
        return RedirectResponse(url=f"/year/{year}/semester/{semester}/subject/{code}?error={msg}", status_code=303)

    # If total_mark is not provided or is empty, use the subject's stored total_mark
    target_val: str | float | None
    if total_mark in (None, ""):
        subject = subj
        target_val = subject.total_mark if subject and subject.total_mark is not None else None
    else:
        target_val = total_mark

    # Only parse and use target_val if it is not empty or None
    if target_val not in (None, ""):
        try:
            goal = float(str(target_val))
        except ValueError:
            goal = None
        if goal is not None and 0 < goal <= 100:
            # Recompute assignment aggregates including newly added assignment
            assignments = session.exec(
                select(Assignment).where(
                    Assignment.subject_id == subject_id,
                ).order_by(col(Assignment.id))
            ).all()
            assign_weight_sum = 0.0
            assign_weighted_total = 0.0
            for a in assignments:
                if a.grade_type == GradeType.NUMERIC.value:
                    if a.mark_weight is not None and a.mark_weight != "":
                        try:
                            assign_weight_sum += float(a.mark_weight)
                        except ValueError:
                            pass
                    if a.weighted_mark is not None and a.weighted_mark != "":
                        try:
                            assign_weighted_total += float(a.weighted_mark)
                        except ValueError:
                            pass
            existing_exam = session.exec(
                select(Examination).where(
                    Examination.subject_id == subject_id,
                )
            ).first()
            exam_weight = existing_exam.exam_weight if existing_exam else max(0.0, 100.0 - assign_weight_sum)
            # PS is a hurdle, not a weight scaler
            setting = session.exec(
                select(ExamSettings).where(
                    ExamSettings.subject_id == subject_id,
                )
            ).first()
            ps_enabled = bool(setting.ps_exam) if setting else False
            factor_val = setting.ps_factor if (setting and setting.ps_factor) else 40.0
            effective_exam_weight = exam_weight
            if effective_exam_weight > 0:
                needed_weighted = (goal / 100.0) * (assign_weight_sum + effective_exam_weight) - assign_weighted_total
                needed_raw = (needed_weighted * 100.0) / effective_exam_weight
                # Clamp 0..100 and enforce PS hurdle if enabled
                if needed_raw < 0:
                    needed_raw = 0.0
                if needed_raw > 100:
                    needed_raw = 100.0
                if ps_enabled and needed_raw < factor_val:
                    needed_raw = float(factor_val)
                weighted_contrib = (needed_raw / 100.0) * exam_weight
                if existing_exam:
                    existing_exam.exam_mark = round(weighted_contrib, 4)
                    existing_exam.exam_weight = exam_weight
                else:
                    session.add(
                        Examination(
                            subject_id=subject_id,
                            exam_mark=round(weighted_contrib, 4),
                            exam_weight=exam_weight,
                        )
                    )
                session.commit()
    if subject_id is not None:
        GradeCalculator(session).sync_subject_total(subject_id)

    url = f"/semester/{semester}/subject/{code}?year={year}&total_mark={target_val or ''}"
    if return_to:
        url += f"&return_to={return_to}"
    return RedirectResponse(url, status_code=303)


@assignment_router.api_route("/assignment/{assessment}/{year}/delete", methods=["POST"], response_class=HTMLResponse)
def delete_assignment(
    assessment: str,
    code: str,
    semester: str,
    year: str,
    return_to: Optional[str] = Form(None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """
    Delete an assignment by ID.
    
    Args:
        semester (str): Semester name.
        code (str): Subject code.
        assignment_id (int): ID of the assignment to delete.
        year (str): Semester year.
        session (Session): Database session dependency.
    
    Returns:
        RedirectResponse: Redirect to subject detail page.
    """
    # Resolve subject and delete by (subject_id, assessment)
    subj = session.exec(
        select(Subject)
        .join(Semester)
        .where(
            Subject.subject_code == code,
            Semester.name == semester,
            Semester.year == int(year),
        )
    ).first()
    if getattr(subj, "is_finalized", False):
        return RedirectResponse(
            f"/semester/{semester}/subject/{code}?year={year}&error=Subject+is+finalized.+Uncheck+Finalized+to+modify+assignments.",
            status_code=303,
        )
    sid = getattr(subj, "id", None)
    existing = session.exec(
        select(Assignment).where(
            Assignment.subject_id == sid,
            Assignment.assessment == assessment,
        )
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
        if sid is not None:
            GradeCalculator(session).sync_subject_total(sid)
    # Always include total_mark in redirect to ensure summary recalculates
    total_mark = getattr(subj, "total_mark", None)
    url = f"/semester/{semester}/subject/{code}?year={year}"
    if total_mark not in (None, ""):
        url += f"&total_mark={total_mark}"
    if return_to:
        url += f"&return_to={return_to}"
    return RedirectResponse(url, status_code=303)

# AJAX endpoint: return assignment edit form HTML
@assignment_router.api_route("/assignment/{assessment}/{year}/edit", response_class=HTMLResponse, methods=["GET", "HEAD"])
def edit_assignment_form(
    request: Request,
    assessment: str,
    year: str,
    code: str,
    semester: str,
    session: Session = Depends(get_session),
):
    """
    Short description.

    Args:
        request: Description.
        assessment: Description.
        year: Description.
        code: Description.
        semester: Description.
        session: Description.

    Raises:
        Description.
    """
    # Resolve subject to query by normalized ID
    subj = session.exec(
        select(Subject)
        .join(Semester)
        .where(
            Subject.subject_code == code,
            Semester.name == semester,
            Semester.year == int(year),
        )
    ).first()
    sid = getattr(subj, "id", None)
    assignment = session.exec(
        select(Assignment).where(
            Assignment.subject_id == sid,
            Assignment.assessment == assessment,
        )
    ).first()
    if not assignment:
        return HTMLResponse("Assignment not found", status_code=404)
    # Return only <td> cells for inline editing, with a form inside the last cell
    is_exam_checked = "checked" if getattr(assignment, "is_exam", False) else ""
    # exam_type is not a field on Assignment; default to 'assignment' for UI
    exam_type_val = "assignment"
    
    safe_assessment_text = html_escape(str(assignment.assessment or ""))
    safe_category_text = html_escape(str(getattr(assignment, "category", "") or ""))
    safe_assessment_js = json.dumps(str(assessment))
    safe_code_js = json.dumps(str(code))
    safe_semester_js = json.dumps(str(semester))
    safe_year_js = json.dumps(str(year))
    
    return HTMLResponse(f"""
<td class='pl-2 pr-1 align-top'>
    <div class='flex flex-col gap-1 w-full max-w-full'>
        <input name='new_assessment' class='input input-bordered input-xs w-full' value='{safe_assessment_text}' required />
        <div class='flex flex-wrap items-center gap-1.5 text-xs'>
            <label class='cursor-pointer flex items-center gap-1 p-0'><span class='label-text text-[10px]'>Exam?</span><input type='checkbox' name='is_exam' value='true' class='checkbox checkbox-xs' {is_exam_checked} onchange="document.getElementById('edit-exam-type-select').disabled = !this.checked;" /></label>
            <select id='edit-exam-type-select' name='exam_type' class='select select-bordered select-xs' {'disabled' if not getattr(assignment, 'is_exam', False) else ''}>
                <option value='assignment' {'selected' if exam_type_val == 'assignment' else ''}>Assignment</option>
                <option value='main' {'selected' if exam_type_val == 'main' else ''}>Main</option>
            </select>
        </div>
    </div>
</td>
<td class='hidden md:table-cell align-top'><input name='category' class='input input-bordered input-xs w-full max-w-[9rem]' value='{safe_category_text}' placeholder='Category' /></td>
<td class='px-1.5 sm:px-2 text-center tabular-nums align-top'><input name='weighted_mark' type='number' step='any' min='0' class='input input-bordered input-xs w-12 px-1 text-center tabular-nums' value='{assignment.weighted_mark if assignment.weighted_mark is not None else ''}' placeholder='Mark' /></td>
<td class='assignment-unweighted hidden sm:table-cell px-1.5 sm:px-2 text-center tabular-nums align-top'><input name='unweighted_mark' type='text' class='input input-xs w-12 px-1 text-center tabular-nums bg-gray-200 cursor-not-allowed' style='background-color:#e5e7eb;cursor:not-allowed;' value="{'-' if assignment.grade_type in ['S','U'] else ('%.2f' % (float(assignment.unweighted_mark)*100) if assignment.unweighted_mark is not None else 'Pending')}" readonly tabindex='-1' /></td>
<td class='px-2 text-center tabular-nums align-top'><input name='mark_weight' type='number' step='any' min='0' class='input input-bordered input-xs w-12 px-1 text-center tabular-nums' value='{assignment.mark_weight if assignment.mark_weight is not None else ''}' placeholder='Weight' /></td>
<td class='hidden md:table-cell text-center align-top'><select name='grade_type' class='select select-bordered select-xs w-20'>
        <option value='numeric' {'selected' if assignment.grade_type == 'numeric' else ''}>Numeric</option>
        <option value='S' {'selected' if assignment.grade_type == 'S' else ''}>S</option>
        <option value='U' {'selected' if assignment.grade_type == 'U' else ''}>U</option>
    </select></td>
<td class='w-px whitespace-nowrap pl-2 pr-2 text-right align-top'>
    <div class='flex items-center justify-end gap-1'>
        <button type='button' class='btn btn-xs btn-primary p-1 min-h-0 h-7 w-7 sm:w-auto sm:px-2' title='Save' onclick='window.submitInlineEditAssignmentRow({safe_assessment_js}, {safe_code_js}, {safe_semester_js}, {safe_year_js})'><span class='sm:hidden'>✓</span><span class='hidden sm:inline'>Save</span></button>
        <button type='button' class='btn btn-xs p-1 min-h-0 h-7 w-7 sm:w-auto sm:px-2' title='Cancel' onclick='window.cancelInlineEditAssignment()'><span class='sm:hidden'>✕</span><span class='hidden sm:inline'>Cancel</span></button>
    </div>
</td>
""")

# AJAX endpoint: update assignment and return JSON result
@assignment_router.api_route("/assignment/{assessment}/{year}/update", methods=["POST"], response_class=JSONResponse)
def update_assignment_ajax(
    assessment: str,
    code: str,
    semester: str,
    year: str,
    category: Optional[str] = Form(None),
    # Accept blanks from form; parse manually
    new_assessment: Optional[str] = Form(None),
    weighted_mark: Optional[str] = Form(None),
    mark_weight: Optional[str] = Form(None),
    grade_type: str = Form("numeric"),
    is_exam: bool = Form(False),
    exam_type: str = Form("assignment"),
    session: Session = Depends(get_session),
):
    """
    Update an existing assignment via AJAX and synchronize subject totals.

    Args:
        assessment: Original assessment name.
        code: Subject code.
        semester: Semester name.
        year: Semester year.
        category: Optional category override.
        new_assessment: Optional updated assessment name.
        weighted_mark: Numeric weighted mark string.
        mark_weight: Mark weight string.
        grade_type: Grade type ("numeric", "S", "U").
        is_exam: Whether the assignment represents the final exam.
        exam_type: Exam type ("assignment" or "main").
        session: Database session dependency.

    Returns:
        JSONResponse: Success status and URL to reload the page.
    """
    try:
        # Resolve subject to use normalized ID-based lookups
        subj = session.exec(
            select(Subject)
            .join(Semester)
            .where(
                Subject.subject_code == code,
                Semester.name == semester,
                Semester.year == int(year),
            )
        ).first()
        sid = getattr(subj, "id", None)
        if sid is None:
            return JSONResponse({"success": False, "error": "Subject not found."}, status_code=404)
        if getattr(subj, "is_finalized", False):
            return JSONResponse(
                {"success": False, "error": "Subject is finalized. Uncheck Finalized to modify assignments."},
                status_code=403,
            )
        assignment = session.exec(
            select(Assignment).where(
                Assignment.subject_id == sid,
                Assignment.assessment == assessment,
            )
        ).first()
        if not assignment:
            return JSONResponse({"success": False, "error": "Assignment not found."}, status_code=404)
        
        # Update assignment name if changed
        if new_assessment and new_assessment != assessment:
            existing = session.exec(
                select(Assignment).where(
                    Assignment.subject_id == sid,
                    Assignment.assessment == new_assessment,
                )
            ).first()
            if existing:
                return JSONResponse({"success": False, "error": "An assignment with this name already exists."}, status_code=400)
            assignment.assessment = new_assessment
        assignment.category = _derive_category(new_assessment or assignment.assessment, category)
        
        # Update marks and weights
        if grade_type == GradeType.NUMERIC.value:
            try:
                if mark_weight is not None and mark_weight.strip() != "":
                    mark_weight_val = float(mark_weight)
                    assignment.mark_weight = mark_weight_val
                else:
                    mark_weight_val = float(assignment.mark_weight) if assignment.mark_weight is not None else 0.0

                if weighted_mark is not None and weighted_mark.strip() != "":
                    weighted_val = float(weighted_mark)
                    assignment.weighted_mark = weighted_val
                    assignment.unweighted_mark = round(weighted_val / mark_weight_val, 4) if mark_weight_val else None
                else:
                    assignment.weighted_mark = None
                    assignment.unweighted_mark = None
            except ValueError:
                return JSONResponse({"success": False, "error": "Invalid numeric values."}, status_code=400)
        elif grade_type in (GradeType.SATISFACTORY.value, GradeType.UNSATISFACTORY.value):
            assignment.weighted_mark = None
            assignment.mark_weight = None
            assignment.unweighted_mark = None

        assignment.grade_type = grade_type
        assignment.is_exam = is_exam
        session.commit()

        # --- Sync Examination table if assignment is (un)marked as exam ---
        exam = session.exec(
            select(Examination).where(
                (Examination.subject_id == sid) & (Examination.exam_type == exam_type)
            )
        ).first()
        if is_exam:
            exam_mark = float(assignment.weighted_mark) if assignment.weighted_mark is not None else None
            exam_weight = float(assignment.mark_weight) if assignment.mark_weight is not None else 0.0
            if exam:
                exam.exam_mark = exam_mark
                exam.exam_weight = exam_weight
            else:
                session.add(
                    Examination(
                        subject_id=sid,
                        exam_mark=exam_mark,
                        exam_weight=exam_weight,
                        exam_type=exam_type,
                    )
                )
            session.commit()
        else:
            if exam:
                session.delete(exam)
                session.commit()

        # Recalculate and persist subject total_mark and is_finalized status
        if sid is not None:
            GradeCalculator(session).sync_subject_total(sid)

        # Reload the page to reflect recalculated summaries and grouping
        reload_url = f"/semester/{semester}/subject/{code}?year={year}"
        return JSONResponse({"success": True, "reload_url": reload_url})
    except Exception:
        logger.exception("update_assignment_ajax failed")
        return JSONResponse({"success": False, "error": "Internal server error"}, status_code=500)
        