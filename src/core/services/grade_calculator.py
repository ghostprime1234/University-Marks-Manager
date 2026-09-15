import math
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Sequence, Any
from sqlalchemy.sql import select, func, case
from sqlalchemy.orm import Session
from sqlmodel import col
from src.infrastructure.db.models import Subject, Semester, GradeScale, Course, ExamSettings, SubjectRule, Assignment, Examination


def academic_round(val: float) -> int:
    """Round using strict half-up rules (e.g. 84.5 → 85, not banker's rounding)."""
    return int(Decimal(str(val)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _round2dp(val: float) -> float:
    """Half-up round to 2 decimal places. Avoids Python's banker's rounding (round())
    so that e.g. 19.125 → 19.13 instead of 19.12."""
    return math.floor(val * 100 + 0.5) / 100

WEIGHT_THRESHOLD = 40.0
MIN_CATEGORY_THRESHOLD = 5


def _derive_assessment_category(name: str, explicit_category: Any = None) -> str:
    if explicit_category:
        return str(explicit_category)
    derived = re.sub(r"\s*[-_:]*\s*\d+\s*$", "", name).strip()
    return derived or name or "Assessments"


def process_assessments(
    assessments: Sequence[Any],
    rules: Sequence[SubjectRule] | None = None,
    min_category_threshold: int = MIN_CATEGORY_THRESHOLD,
) -> dict[str, Any]:
    """
    Process and categorize assessments into standalone assessments and category groups.

    Assessments with weight > WEIGHT_THRESHOLD or belonging to categories with
    <= min_category_threshold entries remain ungrouped at the root level (standalone),
    retaining their original type labels and individual weights. Dedicated category
    groups are only formed when a category contains more than min_category_threshold entries.

    Parameters:
    - assessments (Sequence[Any]): Sequence of assessment objects or dicts.
    - rules (Sequence[SubjectRule] | None): Optional subject rules for pattern-based category derivation.
    - min_category_threshold (int): Minimum count threshold for forming a dedicated category group
      (default: MIN_CATEGORY_THRESHOLD = 5).

    Returns:
    - dict[str, Any]: Dictionary containing:
        - "standalone_assessments": List of individual assessments (each retaining name, weight, score, category)
        - "grouped_assessments": Dict of category -> {"rows": [...], "weight": float, "score": float | None}
    """
    standalone_assessments: list[dict[str, Any]] = []
    candidate_groups: dict[str, dict[str, Any]] = {}

    rule_patterns: list[tuple[str, str]] = []
    for rule in rules or []:
        pattern = (getattr(rule, "sql_pattern", "") or "").replace("%", "").strip().lower()
        label = str(getattr(rule, "rule_label", "") or "").strip()
        if pattern and label:
            rule_patterns.append((pattern, label))

    def _derive_category(name: str, explicit_category: Any = None) -> str:
        lowered_name = (name or "").lower()
        for pattern, label in rule_patterns:
            if pattern in lowered_name:
                return label
        return _derive_assessment_category(name, explicit_category)

    for index, assessment in enumerate(assessments, start=1):
        if isinstance(assessment, dict):
            name = assessment.get("name") or assessment.get("assessment") or f"Assessment {index}"
            weight_val = assessment.get("weight") if assessment.get("weight") is not None else assessment.get("mark_weight", 0.0)
            score_val = assessment.get("score") if assessment.get("score") is not None else assessment.get("weighted_mark", assessment.get("unweighted_mark"))
            explicit_category = assessment.get("category")
        else:
            name = getattr(assessment, "name", None) or getattr(assessment, "assessment", None) or f"Assessment {index}"
            weight_val = getattr(assessment, "weight", None) or getattr(assessment, "mark_weight", 0.0)
            score_val = getattr(assessment, "score", None) or getattr(assessment, "weighted_mark", None) or getattr(assessment, "unweighted_mark", None)
            explicit_category = getattr(assessment, "category", None)

        category = _derive_category(name, explicit_category)

        # Fix: Ensure types are cast safely once
        weight = float(weight_val) if weight_val is not None else 0.0
        score = float(score_val) if score_val is not None else None

        row = {
            "name": name,
            "weight": weight,
            "score": score,
            "category": category,
        }

        if weight > WEIGHT_THRESHOLD:
            standalone_assessments.append(row)
            continue

        group = candidate_groups.setdefault(
            category,
            {"rows": [], "weight": 0.0, "score_total": 0.0, "scored_weight": 0.0},
        )
        group["rows"].append(row)
        group["weight"] += weight
        
        # Fix: Only add to totals if a score exists to avoid NoneType errors
        if score is not None:
            group["score_total"] += score
            group["scored_weight"] += weight

    grouped_assessments: dict[str, dict[str, Any]] = {}
    for category, group in candidate_groups.items():
        if len(group["rows"]) > min_category_threshold:
            scored_weight = group.pop("scored_weight")
            score_total = group.pop("score_total")

            group["score"] = (
                round((score_total / scored_weight) * 100.0, 2)
                if scored_weight > 0
                else None
            )
            group["weight"] = round(group["weight"], 2)
            grouped_assessments[category] = group
        else:
            for row in group["rows"]:
                standalone_assessments.append(row)

    standalone_assessments.sort(key=lambda row: row["name"].lower())

    return {
        "standalone_assessments": standalone_assessments,
        "grouped_assessments": dict(sorted(grouped_assessments.items(), key=lambda item: item[0].lower())),
    }

class GradeCalculator:
    MIN_CATEGORY_THRESHOLD: int = MIN_CATEGORY_THRESHOLD

    def __init__(self, session: Session, min_category_threshold: int = MIN_CATEGORY_THRESHOLD):
        self.session = session
        self.min_category_threshold = min_category_threshold
    
    def _print_sql(self, query, label="SQL"):
        """Helper to print raw SQL for debugging"""
        # Uncomment below to see SQL queries during development
        # try:
        #     compiled = query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        #     print(f"[GRADE_CALCULATOR] {label}:")
        #     print(f"  {compiled}")
        # except Exception as e:
        #     print(f"[GRADE_CALCULATOR] {label}: Could not compile - {e}")
    
    def _build_base_query(self, course_id: int | None = None):
        """
        Function Description: Build base query for subjects with valid marks, optionally filtered by course.
        
        Parameters:
        - course_id (int | None): If provided, filters subjects to those in semesters of the specified course.
        
        Returns:
        - SQLModel Select query for Subject with joins and filters applied.
        """
        query = select(Subject).join(Semester)
        
        # Filter by course if provided
        if course_id is not None:
            query = query.where(col(Semester.course_id) == course_id)

        query = query.where(
            col(Subject.total_mark).is_not(None),
            col(Subject.total_mark) > 0,
            col(Subject.is_finalized).is_(True)
        )
        
        self._print_sql(query, f"Base Query (course_id={course_id})")
        return query

    def _get_grade_scales(self, course_id: int | None = None) -> list[GradeScale]:
        """Get grade scales for the course, seeding defaults if empty. Only use band_type='both'."""
        scale_name = None
        
        # Step 1: If course_id provided, get the scale_name from the course's grading_scale_id
        if course_id:
            course = self.session.get(Course, course_id)
            if course and getattr(course, "grading_scale_id", None):
                scale_id = course.grading_scale_id
                print(f"[GRADE_CALCULATOR] Course {course_id} has grading_scale_id={scale_id}")
                
                # Get the scale row to extract scale_name
                ref_scale = self.session.get(GradeScale, scale_id)
                if ref_scale:
                    scale_name = ref_scale.scale_name
                    print(f"[GRADE_CALCULATOR] Scale ID {scale_id} maps to scale_name: {scale_name}")

        # Step 2: Query for ALL scales with the determined scale_name (or Standard as fallback)
        if scale_name:
            query = select(GradeScale).where(
                col(GradeScale.scale_name) == scale_name,
                col(GradeScale.band_type) == "both"
            )
            self._print_sql(query, f"Grade Scales Query (scale_name={scale_name})")
            scales = self.session.execute(query).scalars().all()
        else:
            print(f"[GRADE_CALCULATOR] No specific scale_name, querying Standard scale")
            query = select(GradeScale).where(
                col(GradeScale.scale_name) == "Standard",
                col(GradeScale.band_type) == "both"
            )
            self._print_sql(query, "Grade Scales Query (Standard scale)")
            scales = self.session.execute(query).scalars().all()

        print(f"[GRADE_CALCULATOR] Found {len(scales)} grade scales from query")
        for s in scales:
            print(f"  - Grade: {s.grade}, min_mark: {s.min_mark}")

        if not scales:
            # Seed defaults for Standard scale if missing
            print("[GRADE_CALCULATOR] No scales found, seeding defaults...")
            defaults = [
                GradeScale(scale_name="Standard", grade="HD", label="High Distinction", min_mark=85.0, gpa_point=4.0, band_type="both"),
                GradeScale(scale_name="Standard", grade="D", label="Distinction", min_mark=75.0, gpa_point=3.7, band_type="both"),
                GradeScale(scale_name="Standard", grade="C", label="Credit", min_mark=65.0, gpa_point=3.3, band_type="both"),
                GradeScale(scale_name="Standard", grade="P", label="Pass", min_mark=50.01, gpa_point=2.0, band_type="both"),
                GradeScale(scale_name="Standard", grade="PS", label="Pass Supplementary", min_mark=50.0, gpa_point=2.0, band_type="both"),
                GradeScale(scale_name="Standard", grade="F", label="Fail", min_mark=0.0, gpa_point=0.0, band_type="both"),
            ]
            for d in defaults:
                self.session.add(d)
            self.session.commit()
            scales = defaults

        # Sort by min_mark descending for evaluation logic
        sorted_scales = sorted(scales, key=lambda x: x.min_mark, reverse=True)
        # Debug output
        print("[GRADE_CALCULATOR] Using grade scales for GPA:")
        for s in sorted_scales:
            print(f"  Grade: {s.grade}, min_mark: {s.min_mark}, gpa_point: {s.gpa_point}, band_type: {s.band_type}")
        return sorted_scales

    def calculate_wam(self, course_id: int | None = None) -> float | None:
        """
        Calculate Weighted Average Mark (WAM) using SQL aggregation.
        WAM = Sum(Mark * CreditPoints) / Sum(CreditPoints)
        Only includes subjects with a non-zero total_mark.
        """
        base_query = self._build_base_query(course_id)
        
        # Create subquery for aggregation
        sub = base_query.subquery()

        # Use SQL aggregation for efficiency
        wam_query = select(
            func.sum(sub.c.total_mark * sub.c.credit_points).label("weighted_sum"),
            func.sum(sub.c.credit_points).label("credit_sum")
        )
        
        self._print_sql(wam_query, f"WAM Query (course_id={course_id})")
        
        result = self.session.execute(wam_query).first()
        
        print(f"[GRADE_CALCULATOR] WAM calculation for course_id={course_id}: result={result}")
        
        if result is None or result[1] is None or result[1] == 0:
            print(f"[GRADE_CALCULATOR] WAM result is None/0")
            return None
        
        weighted_sum, credit_sum = result
        wam = round(weighted_sum / credit_sum, 2) if weighted_sum is not None else None
        print(f"[GRADE_CALCULATOR] WAM = {weighted_sum} / {credit_sum} = {wam}")
        return wam

    def calculate_grade_counts(self, course_id: int | None = None) -> dict[str, int]:
        """
        Calculate the count of subjects in each grade band.
        Uses GradeScale from DB. Each subject is counted in the HIGHEST grade it qualifies for.
        Pass Supplementary (PS) is counted using the ps_exam flag from exam_settings, and must be counted before P.
        """
        scales = self._get_grade_scales(course_id)
        # Always include all standard grades in the output, even if not present in DB
        all_grades = ['HD', 'D', 'C', 'P', 'PS', 'F']
        counts = {g: 0 for g in all_grades}
        for s in scales:
            if s.grade not in counts:
                counts[s.grade] = 0

        # Fetch all valid subjects (already filtered by total_mark > 0)
        base_query = self._build_base_query(course_id)
        subjects = self.session.execute(base_query).scalars().all()

        # Pre-fetch all exam_settings for efficiency
        subject_ids: list[int] = [subj.id for subj in subjects if subj.id is not None]
        exam_settings_map = {}
        if subject_ids:
            exam_settings = self.session.execute(
                select(ExamSettings).where(col(ExamSettings.subject_id).in_(subject_ids))
            ).scalars().all()
            exam_settings_map = {es.subject_id: es for es in exam_settings}

        for subject in subjects:
            mark = academic_round(subject.total_mark) if subject.total_mark is not None else None
            subj_exam = exam_settings_map.get(subject.id) if subject.id is not None else None
            is_ps = subj_exam.ps_exam if subj_exam else False
            
            if is_ps:
                counts['PS'] += 1
                continue

            for scale in scales:
                if scale.grade == 'PS':
                    continue
                if mark is not None and mark >= scale.min_mark:
                    counts[scale.grade] += 1
                    break
        
        return counts

    def calculate_gpa(self, course_id: int | None = None) -> float | None:
        """
        Calculate Grade Point Average (GPA) using SQL CASE expressions.
        GPA = Sum(GradePoints * CreditPoints) / Sum(CreditPoints)
        Uses GradeScale from DB.
        """
        print(f"[GRADE_CALCULATOR] Calculating GPA for course_id={course_id}")
        scales = self._get_grade_scales(course_id)
        base_query = self._build_base_query(course_id)
        
        # Define the subquery variable so it can be referenced
        grade_subquery = base_query.subquery()
        # Build CASE expression for GPA points based on grade scales
        # Use SQLAlchemy's case() for proper typing
        # In SQL, ROUND(total_mark) performs half-up rounding to the nearest integer:
        whens = [
            (func.round(grade_subquery.c.total_mark) >= scale.min_mark, scale.gpa_point)
            for scale in scales
        ]
        gpa_point_expr = case(*whens, else_=0.0)
        
        # Calculate weighted GPA
        gpa_query = select(
            func.sum(gpa_point_expr * grade_subquery.c.credit_points).label("gpa_weighted_sum"),
            func.sum(grade_subquery.c.credit_points).label("credit_sum")
        )
        
        self._print_sql(gpa_query, f"GPA Query (course_id={course_id})")
        
        result = self.session.execute(gpa_query).first()

        # Debug: Print each subject's mark, credit points, and assigned GPA point
        print("[GRADE_CALCULATOR] GPA subject breakdown (course_id={}):".format(course_id))
        subjects = self.session.execute(base_query).scalars().all()
        for subj in subjects:
            mark = subj.total_mark
            cp = subj.credit_points
            course = getattr(subj, 'semester', None)
            course_id_dbg = getattr(course, 'course_id', None) if course else None
            assigned_gpa = 0.0
            for scale in scales:
                if mark is not None and mark >= scale.min_mark:
                    assigned_gpa = scale.gpa_point
                    break
            print(f"  Subject: {subj.subject_code}, Mark: {mark}, Credit Points: {cp}, GPA Point: {assigned_gpa}, Subject Course ID: {course_id_dbg}")
        if result is None or result[1] is None or result[1] == 0:
            return None
        
        gpa_weighted_sum, credit_sum = result
        return round(gpa_weighted_sum / credit_sum, 2) if gpa_weighted_sum is not None else None


    def calculate_subject_summary(
        self,
        subject: Subject,
        min_category_threshold: int | None = None,
    ) -> dict[str, Any]:
        """
        Calculate the complete marks summary and target grade goals for a subject.

        Processes rules, final examinations, and general assignments. General assignments
        with weight >= WEIGHT_THRESHOLD or belonging to categories with <= min_category_threshold
        entries remain ungrouped at the root level (display_status: 'Standalone'), retaining
        their original type labels and individual weights. Dedicated category groups
        (display_status: 'Grouped') are only formed when a category contains more than
        min_category_threshold entries (e.g., high-frequency weekly quizzes).

        Parameters:
        - subject (Subject): The Subject model instance to calculate summary for.
        - min_category_threshold (int | None): Minimum count threshold for forming dedicated category groups.
          If None, uses self.min_category_threshold (default: MIN_CATEGORY_THRESHOLD = 5).

        Returns:
        - dict[str, Any]: Contains 'summaries', 'grade_goals', 'total_achieved',
          'remaining_weight', and 'is_fully_graded'.
        """
        if not subject.id:
            return {"summaries": [], "grade_goals": []}

        threshold = self.min_category_threshold if min_category_threshold is None else min_category_threshold

        # 1. Setup Data
        rules = self.session.execute(select(SubjectRule).where(col(SubjectRule.subject_id) == subject.id)).scalars().all()
        all_assignments = list(getattr(subject, "assignments", []) or [])
        exam_record = self.session.execute(select(Examination).where(col(Examination.subject_id) == subject.id)).scalars().first()

        summary: list[dict[str, Any]] = []
        used_assignment_ids: set[int] = set()
        rule_patterns = []

        def _item_score(item: Any) -> float | None:
            if isinstance(item, Examination):
                return float(item.exam_mark) if item.exam_mark is not None else None
            score = getattr(item, "unweighted_mark", None)
            return float(score) if score is not None else None

        def _is_item_scored(item: Any) -> bool:
            if isinstance(item, Examination):
                return item.exam_mark is not None
            return getattr(item, "unweighted_mark", None) is not None or getattr(item, "weighted_mark", None) is not None

        def _item_weight(item: Any) -> float:
            if isinstance(item, Examination):
                return float(item.exam_weight) if item.exam_weight is not None else 0.0
            weight = getattr(item, "mark_weight", None)
            return float(weight) if weight is not None else 0.0

        def _item_weighted_score(item: Any) -> float:
            if isinstance(item, Examination):
                return float(item.exam_mark) if item.exam_mark is not None else 0.0
            weighted_mark = getattr(item, "weighted_mark", None)
            return float(weighted_mark) if weighted_mark is not None else 0.0

        # 2. Process Rules
        for rule in rules:
            clean_pattern = (rule.sql_pattern or "").replace('%', '').lower()
            rule_patterns.append(clean_pattern)
            
            matches = [a for a in all_assignments if not a.is_exam and a.id is not None 
                       and a.id not in used_assignment_ids and clean_pattern in (a.assessment or "").lower()]
            
            matches.sort(key=lambda x: (x.unweighted_mark or 0), reverse=True)
            core_items = matches[:rule.max_count]
            scored_core_items = [item for item in core_items if _is_item_scored(item)]
            scored_core_weight = sum(_item_weight(item) for item in scored_core_items)
            weighted_score = sum(_item_weighted_score(item) for item in scored_core_items)
            
            for item in matches:
                if item.id is not None: used_assignment_ids.add(item.id)
            
            summary.append({
                "type": "rule",
                "label": rule.rule_label,
                "category": rule.rule_label,
                "count": len(core_items),
                "unweighted_avg": (weighted_score / scored_core_weight) if scored_core_weight > 0 else None,
                "total_weight": sum((a.mark_weight or 0) for a in core_items),
                "weighted_score": _round2dp(weighted_score),
                "bonus_count": max(0, len(matches) - rule.max_count),  # Required by template
                "display_status": "Grouped",
                "has_scored_items": bool(scored_core_items),
                "core_items": core_items,
            })

       # 3. Process Exam
        has_exam_flag = getattr(subject, "has_exam", False)
        exam_assignment = next((a for a in all_assignments if a.is_exam), None)

        score: float | None = None
        if exam_record or exam_assignment or has_exam_flag:
            if exam_record:
                e_mark = exam_record.exam_mark
                e_weight = exam_record.exam_weight
                weight = float(e_weight) if e_weight is not None else 50.0
                score = float(e_mark) if e_mark is not None else None
                unweighted = (score / weight) if (score is not None and weight > 0) else None
            elif exam_assignment:
                weight = float(exam_assignment.mark_weight or 50.0)
                score = float(exam_assignment.weighted_mark) if exam_assignment.weighted_mark is not None else None
                
                # Robust ratio conversion: handles both 0.94 and 94.0
                raw_unweighted = getattr(exam_assignment, "unweighted_mark", None)
                if raw_unweighted is not None:
                    raw_float = float(raw_unweighted)
                    unweighted = raw_float / 100.0 if raw_float > 1.0 else raw_float
                elif score is not None and weight > 0:
                    unweighted = score / weight
                else:
                    unweighted = None
            else:
                weight = 50.0
                unweighted, score = None, None

            if weight > 0:
                summary.append({
                    "type": "exam",
                    "label": "Final Examination",
                    "category": "Exam",
                    "count": 1,
                    "unweighted_avg": round(unweighted, 4) if unweighted is not None else None,
                    "total_weight": weight,
                    "weighted_score": _round2dp(score) if score is not None else 0.0,
                    "bonus_count": 0,
                    "display_status": "Standalone",
                    "has_scored_items": score is not None,
                    "core_items": [exam_record] if exam_record else ([exam_assignment] if exam_assignment else []),
                })

        # 4. Process General Assignments
        remaining = [a for a in all_assignments if a.id not in used_assignment_ids 
                    and not a.is_exam and not any(p in (a.assessment or "").lower() for p in rule_patterns)]

        def _make_standalone_entry(a: Assignment, cat: str) -> dict[str, Any]:
            a_weight = _item_weight(a)
            a_score = _item_score(a)
            if a_score is not None:
                a_score_ratio = (a_score / 100.0) if a_score > 1.0 else a_score
            elif getattr(a, "weighted_mark", None) is not None and a_weight > 0:
                a_score_ratio = float(a.weighted_mark) / a_weight
            else:
                a_score_ratio = None

            return {
                "type": "general",
                "label": a.assessment or cat or "Assessment",
                "category": cat,
                "count": 1,
                "unweighted_avg": round(a_score_ratio, 4) if a_score_ratio is not None else None,
                "total_weight": round(a_weight, 2),
                "weighted_score": _round2dp(_item_weighted_score(a)),
                "bonus_count": 0,  # Required by template
                "display_status": "Standalone",
                "has_scored_items": _is_item_scored(a),
                "core_items": [a],
            }

        grouped_general: dict[str, list[Assignment]] = {}
        for a in remaining:
            a_weight = _item_weight(a)
            cat = _derive_assessment_category(str(a.assessment or ""), getattr(a, "category", None))

            if a_weight >= WEIGHT_THRESHOLD:
                summary.append(_make_standalone_entry(a, cat))
            else:
                grouped_general.setdefault(cat, []).append(a)

        for category, items in grouped_general.items():
            if len(items) > threshold:
                scored_items = [item for item in items if _is_item_scored(item)]
                total_weight = sum(_item_weight(item) for item in items)
                weighted_score = sum(_item_weighted_score(item) for item in scored_items)
                scored_weight = sum(_item_weight(item) for item in scored_items)
                unweighted_avg = (weighted_score / scored_weight) if scored_weight > 0 else None
                
                summary.append({
                    "type": "general",
                    "label": category,
                    "category": category,
                    "count": len(items),
                    "unweighted_avg": round(unweighted_avg, 4) if unweighted_avg is not None else None,
                    "total_weight": round(total_weight, 2),
                    "weighted_score": _round2dp(weighted_score),
                    "bonus_count": 0,  # Required by template
                    "display_status": "Grouped",
                    "has_scored_items": bool(scored_items),
                    "core_items": items,
                })
            else:
                for a in items:
                    summary.append(_make_standalone_entry(a, category))

        # 5. Determine whether every weighted item has a real (non-zero) mark.
        # Used by the template to decide whether to show the target-grade calculator
        # or a static "Course Complete" badge.
        def _is_graded(item: Any) -> bool:
            if isinstance(item, Examination):
                if float(item.exam_weight or 0) <= 0:
                    return True
                return item.exam_mark is not None

            weight = float(getattr(item, "mark_weight", 0) or 0)

            if weight <= 0:
                return True

            if getattr(item, "grade_type", "numeric") in ("S", "U"):
                return True

            return getattr(item, "weighted_mark", None) is not None

        gradeable: list[Any] = [
            a for a in all_assignments if float(getattr(a, "mark_weight", 0) or 0) > 0
        ]
        if exam_record and float(exam_record.exam_weight or 0) > 0:
            gradeable.append(exam_record)

        is_fully_graded: bool = bool(gradeable) and all(_is_graded(item) for item in gradeable)

        # 6. Final Calculations & Grade Goals
        type_order = {"rule": 0, "general": 1, "exam": 2}
        summary.sort(key=lambda x: (type_order.get(x["type"], 99), x["label"]))

        # Check for Total Mark override (e.g., if a final grade is already uploaded without individual assignments)
        raw_total = getattr(subject, "total_mark", None)
        db_total_mark = float(raw_total) if raw_total is not None else None

        total_achieved = 0.0
        remaining_weight_value = 0.0

        for row in summary:
            total_achieved += float(row["weighted_score"] or 0.0)

            for item in row["core_items"]:
                if not _is_item_scored(item):
                    remaining_weight_value += _item_weight(item)

        if not summary and db_total_mark is not None and db_total_mark > 0 and getattr(subject, "is_finalized", False) and not all_assignments:
            total_achieved = _round2dp(db_total_mark)
            remaining_weight_value = 0.0

        remaining_weight = _round2dp(remaining_weight_value)
        total_achieved = _round2dp(total_achieved)

        # Apply university half-up rounding before threshold checks so that
        # e.g. 84.61 rounds to 85 and correctly satisfies an HD (85) threshold.
        rounded_total = academic_round(total_achieved)

        grade_goals = []
        for label, target in [("Pass", 50), ("Credit", 65), ("Distinction", 75), ("High Distinction", 85)]:
            if rounded_total >= target:
                status = "Achieved"
                req_p = 0.0
            elif remaining_weight <= 0:
                status = "Impossible"
                req_p = 0.0
            else:
                needed_from_remaining = (target - total_achieved)
                req_p = (needed_from_remaining / remaining_weight) * 100

                if req_p > 100:
                    status = "Impossible"
                else:
                    status = f"{max(0.0, req_p):.2f}%"

            grade_goals.append({
                "label": label,
                "target": target,
                "status": status,
                "required_percent": round(req_p, 2)
            })

        return {
            "summaries": summary,
            "grade_goals": grade_goals,
            "total_achieved": total_achieved,
            "remaining_weight": remaining_weight,
            "is_fully_graded": is_fully_graded,
        }

    def sync_subject_total(self, subject_id: int, min_category_threshold: int | None = None) -> float | None:
        """Recalculates subject summary and persists total_mark and is_finalized."""
        subject = self.session.get(Subject, subject_id)
        if not subject:
            return None

        # Re-use existing comprehensive calculation logic
        summary_result = self.calculate_subject_summary(subject, min_category_threshold=min_category_threshold)
        all_assignments = list(getattr(subject, "assignments", []) or [])
        exam_record = self.session.execute(select(Examination).where(col(Examination.subject_id) == subject.id)).scalars().first()
        has_exam = getattr(subject, "has_exam", False)

        if not all_assignments and not exam_record and not has_exam:
            total_achieved = 0.0
            is_fully_graded = False
        else:
            total_achieved = summary_result.get("total_achieved", 0.0)
            is_fully_graded = summary_result.get("is_fully_graded", False)

        # Update subject fields
        subject.total_mark = total_achieved
        subject.is_finalized = is_fully_graded

        self.session.add(subject)
        self.session.commit()
        return total_achieved