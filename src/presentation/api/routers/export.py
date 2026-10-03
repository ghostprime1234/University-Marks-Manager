import json
import uuid
from typing import Any, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, col, select

from src.core.auth import ensure_uuid, get_optional_user_id
from src.infrastructure.db.models import (
    Assignment,
    Examination,
    ExamSettings,
    Semester,
    Subject,
    SubjectPrerequisite,
    UserCourse,
)
from src.presentation.api.deps import get_session

router = APIRouter()


@router.get("", response_class=Response)
@router.get("/{user_id}", response_class=Response)
def export_user_data(
    user_id: Optional[str] = None,
    current_user_id: Optional[uuid.UUID] = Depends(get_optional_user_id),
    session: Session = Depends(get_session),
) -> Any:
    """Export all data associated with a specific user identified by UUID."""
    target_uuid = None
    if user_id:
        try:
            target_uuid = ensure_uuid(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid user UUID: {user_id}",
            )
    elif current_user_id:
        target_uuid = current_user_id

    if not target_uuid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to export data",
        )

    # Fetch UserCourse links for this user
    user_courses = session.exec(
        select(UserCourse).where(UserCourse.user_id == target_uuid)
    ).all()
    course_ids = [uc.course_id for uc in user_courses]

    if not course_ids:
        # User has no courses, return empty lists
        data: dict[str, Any] = {
            "semesters": [],
            "subjects": [],
            "assignments": [],
            "examinations": [],
            "exam_settings": [],
            "subject_prerequisites": [],
        }
        return Response(
            content=json.dumps(data, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="marks_manager_export.json"'},
        )

    # Fetch Semesters
    semesters = session.exec(select(Semester).where(col(Semester.course_id).in_(course_ids))).all()
    semester_ids = [s.id for s in semesters]

    # Fetch Subjects
    subjects: Sequence[Subject] | List[Subject] = []
    subject_ids = []
    if semester_ids:
        subjects = session.exec(select(Subject).where(col(Subject.semester_id).in_(semester_ids))).all()
        subject_ids = [s.id for s in subjects]

    # Fetch Assignments
    assignments: Sequence[Assignment] | List[Assignment] = []
    if subject_ids:
        assignments = session.exec(select(Assignment).where(col(Assignment.subject_id).in_(subject_ids))).all()

    # Fetch Examinations
    examinations: Sequence[Examination] | List[Examination] = []
    if subject_ids:
        examinations = session.exec(select(Examination).where(col(Examination.subject_id).in_(subject_ids))).all()

    # Fetch ExamSettings
    exam_settings: Sequence[ExamSettings] | List[ExamSettings] = []
    if subject_ids:
        exam_settings = session.exec(select(ExamSettings).where(col(ExamSettings.subject_id).in_(subject_ids))).all()

    # Fetch SubjectPrerequisites
    subject_prerequisites: Sequence[SubjectPrerequisite] | List[SubjectPrerequisite] = []
    if subject_ids:
        subject_prerequisites = session.exec(
            select(SubjectPrerequisite).where(col(SubjectPrerequisite.subject_id).in_(subject_ids))
        ).all()

    export_payload = {
        "semesters": jsonable_encoder(semesters),
        "subjects": jsonable_encoder(subjects),
        "assignments": jsonable_encoder(assignments),
        "examinations": jsonable_encoder(examinations),
        "exam_settings": jsonable_encoder(exam_settings),
        "subject_prerequisites": jsonable_encoder(subject_prerequisites),
    }

    return Response(
        content=json.dumps(export_payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="marks_manager_export.json"'},
    )
