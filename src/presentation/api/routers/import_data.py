import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlmodel import Session

from src.core.auth import ensure_uuid, get_optional_user_id
from src.core.services.grade_calculator import GradeCalculator
from src.infrastructure.db.models import (
    Assignment,
    Examination,
    ExamSettings,
    Semester,
    Subject,
    SubjectPrerequisite,
)
from src.presentation.api.deps import get_session

router = APIRouter()


@router.post("", response_model=Dict[str, Any])
@router.post("/{user_id}", response_model=Dict[str, Any])
async def import_user_data(
    user_id: Optional[str] = None,
    file: UploadFile = File(...),
    current_user_id: Optional[uuid.UUID] = Depends(get_optional_user_id),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    try:
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
                detail="Authentication required to import data",
            )

        content = await file.read()
        try:
            json_data = json.loads(content)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON file.",
            )

        # Robust helper to filter columns based on SQLModel
        def get_json_records(sheet_name: str, model_class: Any) -> List[Dict[str, Any]]:
            records = json_data.get(sheet_name, [])
            if not isinstance(records, list):
                return []

            valid_fields = getattr(model_class, "model_fields", getattr(model_class, "__fields__", {})).keys()

            result: List[Dict[str, Any]] = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                clean_record: Dict[str, Any] = {}
                for k, v in record.items():
                    key_str = str(k)
                    if key_str not in valid_fields:
                        continue

                    if v is None:
                        clean_record[key_str] = None
                    else:
                        clean_record[key_str] = v
                result.append(clean_record)
            return result

        payload = {
            "semesters": get_json_records("semesters", Semester),
            "subjects": get_json_records("subjects", Subject),
            "assignments": get_json_records("assignments", Assignment),
            "examinations": get_json_records("examinations", Examination),
            "exam_settings": get_json_records("exam_settings", ExamSettings),
            "subject_prerequisites": get_json_records("subject_prerequisites", SubjectPrerequisite),
        }

        imported_counts = {k: 0 for k in payload.keys()}

        # STEP 1: Semesters
        for sem_data in payload.get("semesters", []):
            session.merge(Semester(**sem_data))
            imported_counts["semesters"] += 1
        session.flush()

        # STEP 2: Subjects
        for subj_data in payload.get("subjects", []):
            session.merge(Subject(**subj_data))
            imported_counts["subjects"] += 1
        session.flush()

        # STEP 3: Assignments & Others
        for assign_data in payload.get("assignments", []):
            assign = Assignment(**assign_data)
            if assign.unweighted_mark is None and assign.weighted_mark is not None and assign.mark_weight:
                try:
                    weight = float(assign.mark_weight)
                    if weight > 0:
                        assign.unweighted_mark = round(float(assign.weighted_mark) / weight, 4)
                except (ValueError, TypeError):
                    pass
            session.merge(assign)
            imported_counts["assignments"] += 1

        for exam_data in payload.get("examinations", []):
            session.merge(Examination(**exam_data))
            imported_counts["examinations"] += 1

        for exam_set_data in payload.get("exam_settings", []):
            session.merge(ExamSettings(**exam_set_data))
            imported_counts["exam_settings"] += 1

        for prereq_data in payload.get("subject_prerequisites", []):
            session.merge(SubjectPrerequisite(**prereq_data))
            imported_counts["subject_prerequisites"] += 1

        session.flush()
        session.commit()

        # Collect distinct subject_ids from imported records and sync totals
        distinct_subject_ids = {
            assign_data["subject_id"]
            for assign_data in payload.get("assignments", [])
            if assign_data.get("subject_id") is not None
        } | {
            exam_data["subject_id"]
            for exam_data in payload.get("examinations", [])
            if exam_data.get("subject_id") is not None
        } | {
            subj_data["id"]
            for subj_data in payload.get("subjects", [])
            if subj_data.get("id") is not None
        }

        for sid in distinct_subject_ids:
            GradeCalculator(session).sync_subject_total(sid)

        return {
            "success": True,
            "message": f"Successfully imported data for user {target_uuid}",
            "imported": imported_counts,
        }

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Import failed: {str(e)}",
        )
