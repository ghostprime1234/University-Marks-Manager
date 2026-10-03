"""API router for managing user course enrollments with Supabase Auth."""
from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from src.core.auth import get_current_user_id
from src.infrastructure.db.models import Course, UserCourse
from src.presentation.api.deps import get_session

router = APIRouter(prefix="/user-courses", tags=["User Courses"])


class UserCourseCreate(BaseModel):
    course_id: int
    is_default: Optional[bool] = False


class UserCourseRead(BaseModel):
    id: int
    user_id: uuid.UUID
    course_id: int
    is_default: bool
    course_name: Optional[str] = None
    course_code: Optional[str] = None


@router.get("/", response_model=List[UserCourseRead])
def list_user_courses(
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> List[UserCourseRead]:
    """List all courses enrolled by the authenticated Supabase user."""
    statement = (
        select(UserCourse)
        .where(UserCourse.user_id == current_user_id)
        .options(selectinload(UserCourse.course))
    )
    user_courses = session.exec(statement).all()

    return [
        UserCourseRead(
            id=uc.id or 0,
            user_id=uc.user_id,
            course_id=uc.course_id,
            is_default=uc.is_default,
            course_name=uc.course.name if uc.course else None,
            course_code=uc.course.code if uc.course else None,
        )
        for uc in user_courses
    ]


@router.post("/", response_model=UserCourseRead, status_code=status.HTTP_201_CREATED)
def add_user_course(
    data: UserCourseCreate,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> UserCourseRead:
    """Enroll the authenticated user in a course."""
    course = session.get(Course, data.course_id)
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Course with ID {data.course_id} not found",
        )

    existing = session.exec(
        select(UserCourse).where(
            UserCourse.user_id == current_user_id,
            UserCourse.course_id == data.course_id,
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already enrolled in this course",
        )

    user_courses = session.exec(
        select(UserCourse).where(UserCourse.user_id == current_user_id)
    ).all()
    is_default = bool(data.is_default) or len(user_courses) == 0

    if is_default and user_courses:
        for uc in user_courses:
            uc.is_default = False
            session.add(uc)

    user_course = UserCourse(
        user_id=current_user_id,
        course_id=data.course_id,
        is_default=is_default,
    )
    session.add(user_course)
    session.commit()
    session.refresh(user_course)

    return UserCourseRead(
        id=user_course.id or 0,
        user_id=user_course.user_id,
        course_id=user_course.course_id,
        is_default=user_course.is_default,
        course_name=course.name,
        course_code=course.code,
    )


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_user_course(
    course_id: int,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> None:
    """Remove a course from the authenticated user's profile."""
    user_course = session.exec(
        select(UserCourse).where(
            UserCourse.user_id == current_user_id,
            UserCourse.course_id == course_id,
        )
    ).first()
    if not user_course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course enrollment not found",
        )

    was_default = user_course.is_default
    session.delete(user_course)
    session.commit()

    if was_default:
        next_course = session.exec(
            select(UserCourse).where(UserCourse.user_id == current_user_id)
        ).first()
        if next_course:
            next_course.is_default = True
            session.add(next_course)
            session.commit()


@router.patch("/{course_id}/default", response_model=UserCourseRead)
def set_default_user_course(
    course_id: int,
    current_user_id: uuid.UUID = Depends(get_current_user_id),
    session: Session = Depends(get_session),
) -> UserCourseRead:
    """Set a course as the default/active degree for the authenticated user."""
    user_courses = session.exec(
        select(UserCourse).where(UserCourse.user_id == current_user_id)
    ).all()

    target_uc: Optional[UserCourse] = None
    for uc in user_courses:
        if uc.course_id == course_id:
            uc.is_default = True
            target_uc = uc
        else:
            uc.is_default = False
        session.add(uc)

    if not target_uc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course enrollment not found",
        )

    session.commit()
    session.refresh(target_uc)
    course = session.get(Course, course_id)

    return UserCourseRead(
        id=target_uc.id or 0,
        user_id=target_uc.user_id,
        course_id=target_uc.course_id,
        is_default=target_uc.is_default,
        course_name=course.name if course else None,
        course_code=course.code if course else None,
    )
