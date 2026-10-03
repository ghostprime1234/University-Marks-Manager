"""SQLModel ORM models (infrastructure layer)."""

import uuid
from enum import Enum
from typing import Any, ClassVar, Optional

from sqlmodel import Field, SQLModel, Relationship, UniqueConstraint

class GradeType(str, Enum):
    """Supported grading modes for an assessment component."""
    NUMERIC = "numeric"
    SATISFACTORY = "S"
    UNSATISFACTORY = "U"
    HIGH_DISTINCTION = "HD"
    DISTINCTION = "D"
    CREDIT = "C"
    PASS = "P"
    PASS_SUPPLEMENTARY = "PS"
    FAIL = "F"

class User(SQLModel, table=True):
    """Represents a user of the Marks Manager system."""
    __tablename__: ClassVar[Any] = "users"
    id: Optional[uuid.UUID] = Field(default=None, primary_key=True)
    username: Optional[str] = Field(default=None, index=True, unique=True)
    email: Optional[str] = Field(default=None, index=True, unique=True)

class UserCourse(SQLModel, table=True):
    """Association table linking users to their courses."""
    __tablename__: ClassVar[Any] = "user_courses"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(index=True)
    course_id: int = Field(foreign_key="courses.id", ondelete="CASCADE")
    is_default: bool = Field(default=False)
    
    course: "Course" = Relationship(back_populates="user_links")
    
    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="uq_user_course"),
    )

    def __init__(self, **data: Any):
        if "user_id" in data and isinstance(data["user_id"], str):
            data["user_id"] = uuid.UUID(data["user_id"].strip())
        super().__init__(**data)

class University(SQLModel, table=True):
    """Represents a university."""
    __tablename__: ClassVar[Any] = "university"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    
    courses: list["Course"] = Relationship(back_populates="university")

class Course(SQLModel, table=True):
    """Represents a degree or program of study."""
    __tablename__: ClassVar[Any] = "courses"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    code: str = Field(index=True)
    grading_scale_id: int = Field(foreign_key="grade_scales.id")
    university_id: Optional[int] = Field(default=None, foreign_key="university.id")
    gpa_scale: Optional[int] = Field(default=4)  # Max GPA points: 4 (Standard) or 7 (Australian)
    
    university: Optional["University"] = Relationship(back_populates="courses")
    grading_scale: Optional["GradeScale"] = Relationship()
    
    semesters: list["Semester"] = Relationship(back_populates="course")
    user_links: list["UserCourse"] = Relationship(back_populates="course")
    
    __table_args__ = (
        UniqueConstraint("code", name="uq_course_code"),
    )

class Semester(SQLModel, table=True):
    """Academic semester (e.g., Autumn 2025)."""
    __tablename__: ClassVar[Any] = "semesters"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    year: int = Field(index=True)
    course_id: Optional[int] = Field(default=None, foreign_key="courses.id")
    
    course: Optional["Course"] = Relationship(back_populates="semesters")
    subjects: list["Subject"] = Relationship(back_populates="semester")

    __table_args__ = (
        UniqueConstraint("course_id", "name", "year", name="uq_semester_course_name_year"),
    )

class Subject(SQLModel, table=True):
    """Subject/course within a semester."""
    __tablename__: ClassVar[Any] = "subjects"
    id: Optional[int] = Field(default=None, primary_key=True)
    subject_code: str = Field(index=True)
    semester_id: int = Field(foreign_key="semesters.id", index=True)
    subject_name: str
    total_mark: Optional[float] = 0.0
    credit_points: int = Field(default=6)
    sync_subject: bool = False
    has_exam: bool = Field(default=True)
    is_finalized: bool = Field(default=False)

    semester: Optional["Semester"] = Relationship(back_populates="subjects")
    assignments: list["Assignment"] = Relationship(back_populates="subject")
    rules: list["SubjectRule"] = Relationship(back_populates="subject")

    prerequisites: list["SubjectPrerequisite"] = Relationship(
        back_populates="subject",
        sa_relationship_kwargs={
            "primaryjoin": "Subject.id==SubjectPrerequisite.subject_id",
            "cascade": "all, delete-orphan"
        }
    )
    required_for: list["SubjectPrerequisite"] = Relationship(
        back_populates="prerequisite_subject",
        sa_relationship_kwargs={
            "primaryjoin": "Subject.id==SubjectPrerequisite.prerequisite_subject_id",
            "cascade": "all, delete-orphan"
        }
    )

    __table_args__ = (
        UniqueConstraint("subject_code", "semester_id", name="uq_subject_code_semester"),
    )

class SubjectPrerequisite(SQLModel, table=True):
    __tablename__: ClassVar[Any] = "subject_prerequisite"
    id: Optional[int] = Field(default=None, primary_key=True)
    subject_id: int = Field(foreign_key="subjects.id", index=True)
    prerequisite_subject_id: Optional[int] = Field(default=None, foreign_key="subjects.id", index=True)
    custom_text: Optional[str] = None
    is_corequisite: bool = Field(default=False, nullable=False)

    subject: Optional["Subject"] = Relationship(
        back_populates="prerequisites",
        sa_relationship_kwargs={"foreign_keys": "[SubjectPrerequisite.subject_id]"}
    )
    prerequisite_subject: Optional["Subject"] = Relationship(
        back_populates="required_for",
        sa_relationship_kwargs={"foreign_keys": "[SubjectPrerequisite.prerequisite_subject_id]"}
    )

class Assignment(SQLModel, table=True):
    __tablename__: ClassVar[Any] = "assignments"
    id: Optional[int] = Field(default=None, primary_key=True)
    assessment: str = Field(index=True)
    category: Optional[str] = Field(default=None, index=True)
    subject_id: int = Field(foreign_key="subjects.id", index=True)
    weighted_mark: Optional[float] = Field(default=None, nullable=True)
    unweighted_mark: Optional[float] = Field(default=None, nullable=True)
    mark_weight: Optional[float] = None
    grade_type: str = Field(default=GradeType.NUMERIC.value)
    is_exam: bool = Field(default=False)
    
    subject: Optional["Subject"] = Relationship(back_populates="assignments")
    
    __table_args__ = (
        UniqueConstraint("assessment", "subject_id", name="uq_assignment_subject"),
    )

class Examination(SQLModel, table=True):
    __tablename__: ClassVar[Any] = "examinations"
    id: Optional[int] = Field(default=None, primary_key=True)
    subject_id: int = Field(foreign_key="subjects.id", index=True, unique=True)
    exam_mark: float = 0
    exam_weight: float = 100
    exam_type: str = Field(default="main", max_length=32)

class ExamSettings(SQLModel, table=True):
    __tablename__: ClassVar[Any] = "exam_settings"
    id: Optional[int] = Field(default=None, primary_key=True)
    subject_id: int = Field(foreign_key="subjects.id", index=True, unique=True)
    ps_exam: bool = False
    ps_factor: float = 40.0

class GradeScale(SQLModel, table=True):
    __tablename__: ClassVar[Any] = "grade_scales"
    id: Optional[int] = Field(default=None, primary_key=True)
    scale_name: str = Field(default="Standard", index=True)
    grade: str
    label: str
    min_mark: float
    gpa_point: float
    band_type: str = Field(default="both", index=True)
    
    __table_args__ = (
        UniqueConstraint("scale_name", "grade", "band_type", name="uq_scale_grade_type"),
    )

class SubjectRule(SQLModel, table=True):
    __tablename__: ClassVar[Any] = "subject_rules"
    id: Optional[int] = Field(default=None, primary_key=True)
    subject_id: int = Field(foreign_key="subjects.id", index=True)
    rule_label: str
    sql_pattern: str
    max_count: int = 9
    weight_each: float = 2.0
    
    subject: Optional["Subject"] = Relationship(back_populates="rules")

# Final Model Rebuilds
User.model_rebuild()
UserCourse.model_rebuild()
University.model_rebuild()
Course.model_rebuild()
Semester.model_rebuild()
Subject.model_rebuild()
SubjectPrerequisite.model_rebuild()
Assignment.model_rebuild()
SubjectRule.model_rebuild()

__all__ = [
    "User",
    "UserCourse",
    "GradeType",
    "Semester",
    "Subject",
    "Assignment",
    "Examination",
    "ExamSettings",
    "Course",
    "University",
    "GradeScale",
    "SubjectRule",
    "SubjectPrerequisite",
]