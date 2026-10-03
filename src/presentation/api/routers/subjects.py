"""Subject API endpoints."""
from __future__ import annotations

from typing import List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlmodel import Session, select
    # No need for and_ import unless using multiple join conditions

import uuid
from src.core.auth import get_optional_user_id
from src.infrastructure.db.models import Subject, Semester, UserCourse
from src.presentation.api.schemas import SubjectCreate, SubjectRead
from src.presentation.api.deps import get_session

from sqlalchemy.sql import expression

router = APIRouter()


@router.get("/", response_model=List[SubjectRead])
def list_subjects(
    session: Session = Depends(get_session),
    current_user_id: Optional[uuid.UUID] = Depends(get_optional_user_id),
    user_id: Optional[uuid.UUID] = None,
    user_only: bool = False,
    semester_id: Optional[int] = None,
    semester_name: Optional[str] = None,
    year: Optional[int] = None,
    code: Optional[str] = None,
) -> Sequence[Subject]:
    """
    List all subjects, optionally filtered by authenticated user's enrolled courses,
    semester name, year, and subject code.
    """
    stmt = select(Subject)
    joined_semester = False

    effective_user_id = user_id or (current_user_id if user_only else None)
    if effective_user_id is not None:
        user_courses = session.exec(
            select(UserCourse).where(UserCourse.user_id == effective_user_id)
        ).all()
        user_course_ids = [uc.course_id for uc in user_courses]
        if not user_course_ids:
            return []
        stmt = stmt.join(Semester, Subject.semester_id == Semester.id).where(
            Semester.course_id.in_(user_course_ids)
        )
        joined_semester = True

    # Prefer normalized filter by semester_id
    if semester_id is not None:
        stmt = stmt.where(Subject.semester_id == semester_id)
    elif semester_name or year:
        if not joined_semester:
            stmt = stmt.join(Semester, expression.true() & (Subject.semester_id == Semester.id))
            joined_semester = True
        if semester_name:
            stmt = stmt.where(Semester.name == semester_name)
        if year:
            stmt = stmt.where(Semester.year == year)
    if code:
        stmt = stmt.where(Subject.subject_code == code)
    return session.exec(stmt).all()


@router.post("/", response_model=SubjectRead, status_code=status.HTTP_201_CREATED)
def create_subject(data: SubjectCreate, session: Session = Depends(get_session)) -> Subject:
    """
    Create a new subject in a specific semester (normalized: uses semester_id).

    Args:
        data (Subject): Subject data to create.
        session (Session): Database session dependency.
    
    Raises:
        HTTPException: If the subject already exists in the semester (409).
        HTTPException: If the semester_id is invalid (400).
    
    Returns:
        Subject: The created subject.
    """
    # Validate semester exists
    sem = session.get(Semester, data.semester_id)
    if not sem:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="semester not found")

    # Enforce uniqueness: (subject_code, semester_id)
    exists = session.exec(
        select(Subject).where(
            Subject.subject_code == data.subject_code,
            Subject.semester_id == data.semester_id,
        )
    ).first()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="subject already exists for semester")

    # semester_year is now always derived from Semester.year, not stored in Subject
    sub = Subject(
        subject_code=data.subject_code,
        subject_name=data.subject_name,
        semester_id=data.semester_id,
        sync_subject=getattr(data, 'sync_subject', False),
        total_mark=data.total_mark,
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return sub


@router.get("/{subject_id}", response_model=SubjectRead)
def get_subject(subject_id: int, session: Session = Depends(get_session)) -> Subject:
    """
    Retrieve a subject by its ID.
    
    Args:
        subject_id (int): The ID of the subject to retrieve.
        session (Session): Database session dependency.
    
    Raises:
        HTTPException: If the subject is not found (404).
    
    Returns:
        Subject: The requested subject.
    """
    subj = session.get(Subject, subject_id)
    if not subj:
        raise HTTPException(status_code=404, detail="Not found")
    return subj


@router.put("/{subject_id}", response_model=SubjectRead)
def update_subject(subject_id: int, data: SubjectCreate, session: Session = Depends(get_session)) -> Subject:
    """
    Update an existing subject's details.

    Args:
        subject_id (int): The ID of the subject to update.
        data (Subject): Updated subject data.
        session (Session): Database session dependency.
    
    Raises:
        HTTPException: If the subject is not found (404).
        HTTPException: If another subject with same code/semester exists (409).
        HTTPException: If the semester_id is invalid (400).
    
    Returns:
        Subject: The updated subject.
    """
    subj = session.get(Subject, subject_id)
    if not subj:
        raise HTTPException(status_code=404, detail="Not found")

    # Verify semester exists
    semester = session.get(Semester, data.semester_id)
    if not semester:
        raise HTTPException(status_code=400, detail="Invalid semester_id")

    # Check if another subject with the same identifiers exists
    exists = session.exec(
        select(Subject).where(
            Subject.subject_code == data.subject_code,
            Subject.semester_id == data.semester_id,
            Subject.id != subject_id,
        )
    ).first()
    
    if exists:
        raise HTTPException(status_code=409, detail="Another subject with same code/semester exists")

    subj.subject_code = data.subject_code
    subj.subject_name = data.subject_name
    subj.semester_id = data.semester_id
    subj.total_mark = data.total_mark if data.total_mark is not None else subj.total_mark
    # credit_points is not part of SubjectCreate (normalized); keep existing value
    if hasattr(data, 'sync_subject'):
        subj.sync_subject = data.sync_subject
    session.add(subj)
    session.commit()
    session.refresh(subj)
    return subj

@router.delete("/{subject_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_subject(subject_id: int, session: Session = Depends(get_session)) -> Response:
    """
    Delete a subject by its ID.

    Args:
        subject_id (int): The ID of the subject to delete.
        session (Session): Database session dependency.

    Raises:
        HTTPException: If the subject is not found (404).
    """
    subj = session.get(Subject, subject_id)
    if not subj:
        raise HTTPException(status_code=404, detail="Not found")
    session.delete(subj)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

__all__ = ["router"]
