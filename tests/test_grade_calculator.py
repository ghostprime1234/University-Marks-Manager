"""Tests for GradeCalculator service and sync_subject_total."""
from src.core.services.grade_calculator import GradeCalculator
from src.infrastructure.db.models import Assignment, Examination, Subject


def test_sync_subject_total_nonexistent_subject(session):
    """Test sync_subject_total returns None when subject does not exist."""
    calc = GradeCalculator(session)
    result = calc.sync_subject_total(99999)
    assert result is None


def test_sync_subject_total_single_assignment(session, sample_data):
    """Test sync_subject_total updates total_mark and is_finalized for a single assignment."""
    subject_id = sample_data["subject"].id

    a1 = Assignment(
        subject_id=subject_id,
        assessment="Assignment 1",
        weighted_mark=30.0,
        mark_weight=30.0,
        unweighted_mark=1.0,
        grade_type="numeric",
        is_exam=False,
    )
    session.add(a1)
    session.commit()

    calc = GradeCalculator(session)
    total = calc.sync_subject_total(subject_id)
    assert total == 30.0

    session.refresh(sample_data["subject"])
    assert sample_data["subject"].total_mark == 30.0
    assert sample_data["subject"].is_finalized is True


def test_sync_subject_total_multiple_assignments(session, sample_data):
    """Test sync_subject_total with multiple assignments."""
    subject_id = sample_data["subject"].id

    a1 = Assignment(
        subject_id=subject_id,
        assessment="Quiz 1",
        weighted_mark=15.0,
        mark_weight=20.0,
        unweighted_mark=0.75,
        grade_type="numeric",
        is_exam=False,
    )
    a2 = Assignment(
        subject_id=subject_id,
        assessment="Quiz 2",
        weighted_mark=25.0,
        mark_weight=30.0,
        unweighted_mark=0.8333,
        grade_type="numeric",
        is_exam=False,
    )
    session.add_all([a1, a2])
    session.commit()

    calc = GradeCalculator(session)
    total = calc.sync_subject_total(subject_id)
    assert total == 40.0

    session.refresh(sample_data["subject"])
    assert sample_data["subject"].total_mark == 40.0
    assert sample_data["subject"].is_finalized is True


def test_sync_subject_total_partially_graded(session, sample_data):
    """Test sync_subject_total when one assignment has no mark yet (not fully graded)."""
    subject_id = sample_data["subject"].id

    a1 = Assignment(
        subject_id=subject_id,
        assessment="Assignment 1",
        weighted_mark=20.0,
        mark_weight=25.0,
        unweighted_mark=0.80,
        grade_type="numeric",
        is_exam=False,
    )
    a2 = Assignment(
        subject_id=subject_id,
        assessment="Assignment 2",
        weighted_mark=None,
        mark_weight=25.0,
        unweighted_mark=None,
        grade_type="numeric",
        is_exam=False,
    )
    session.add_all([a1, a2])
    session.commit()

    calc = GradeCalculator(session)
    total = calc.sync_subject_total(subject_id)
    assert total == 20.0

    session.refresh(sample_data["subject"])
    assert sample_data["subject"].total_mark == 20.0
    assert sample_data["subject"].is_finalized is False


def test_sync_subject_total_update_and_delete(session, sample_data):
    """Test sync_subject_total after updating and deleting assignments."""
    subject_id = sample_data["subject"].id

    a1 = Assignment(
        subject_id=subject_id,
        assessment="Assignment 1",
        weighted_mark=20.0,
        mark_weight=20.0,
        unweighted_mark=1.0,
        grade_type="numeric",
        is_exam=False,
    )
    session.add(a1)
    session.commit()

    calc = GradeCalculator(session)
    calc.sync_subject_total(subject_id)
    session.refresh(sample_data["subject"])
    assert sample_data["subject"].total_mark == 20.0

    # Update mark
    a1.weighted_mark = 18.0
    a1.unweighted_mark = 0.90
    session.add(a1)
    session.commit()

    calc.sync_subject_total(subject_id)
    session.refresh(sample_data["subject"])
    assert sample_data["subject"].total_mark == 18.0

    # Delete assignment
    session.delete(a1)
    session.commit()

    calc.sync_subject_total(subject_id)
    session.refresh(sample_data["subject"])
    assert sample_data["subject"].total_mark == 0.0
    assert sample_data["subject"].is_finalized is False


def test_sync_subject_total_with_examination(session, sample_data):
    """Test sync_subject_total including an Examination record."""
    subject_id = sample_data["subject"].id

    a1 = Assignment(
        subject_id=subject_id,
        assessment="Assignment 1",
        weighted_mark=40.0,
        mark_weight=40.0,
        unweighted_mark=1.0,
        grade_type="numeric",
        is_exam=False,
    )
    exam = Examination(
        subject_id=subject_id,
        exam_mark=50.0,
        exam_weight=60.0,
    )
    session.add_all([a1, exam])
    session.commit()

    calc = GradeCalculator(session)
    total = calc.sync_subject_total(subject_id)
    assert total == 90.0

    session.refresh(sample_data["subject"])
    assert sample_data["subject"].total_mark == 90.0
    assert sample_data["subject"].is_finalized is True


def test_process_assessments_category_threshold_grouping():
    """
    Test process_assessments only forms dedicated category groups for categories
    with count > MIN_CATEGORY_THRESHOLD (5), while categories with count <= 5
    remain ungrouped at the root level (standalone_assessments) retaining their
    original category labels and individual weights.
    """
    from src.core.services.grade_calculator import process_assessments, MIN_CATEGORY_THRESHOLD

    assert MIN_CATEGORY_THRESHOLD == 5

    # 6 Quizzes (> 5) -> should be grouped
    quizzes = [
        {"name": f"Quiz {i}", "mark_weight": 2.0, "weighted_mark": 1.8, "unweighted_mark": 0.90, "category": "Quiz"}
        for i in range(1, 7)
    ]
    # 2 Assignments (<= 5) -> should remain ungrouped / standalone
    assignments = [
        {"name": "Assignment 1", "mark_weight": 20.0, "weighted_mark": 18.0, "unweighted_mark": 0.90, "category": "Assignment"},
        {"name": "Assignment 2", "mark_weight": 20.0, "weighted_mark": 17.0, "unweighted_mark": 0.85, "category": "Assignment"},
    ]
    # Exactly 5 Labs (<= 5 boundary) -> should remain ungrouped / standalone
    labs = [
        {"name": f"Lab {i}", "mark_weight": 4.0, "weighted_mark": 3.6, "unweighted_mark": 0.90, "category": "Lab"}
        for i in range(1, 6)
    ]

    all_items = quizzes + assignments + labs
    result = process_assessments(all_items)

    # "Quiz" should be in grouped_assessments
    assert "Quiz" in result["grouped_assessments"]
    quiz_group = result["grouped_assessments"]["Quiz"]
    assert len(quiz_group["rows"]) == 6
    assert quiz_group["weight"] == 12.0
    assert quiz_group["score"] == 90.0

    # "Assignment" and "Lab" should NOT be in grouped_assessments
    assert "Assignment" not in result["grouped_assessments"]
    assert "Lab" not in result["grouped_assessments"]
    assert "Other" not in result["grouped_assessments"]

    # Standalone assessments should include the 2 assignments and 5 labs (total 7)
    standalone = result["standalone_assessments"]
    assert len(standalone) == 7

    # Verify they retained their original category labels and individual weights
    assignment_rows = [r for r in standalone if r["category"] == "Assignment"]
    assert len(assignment_rows) == 2
    assert {r["name"] for r in assignment_rows} == {"Assignment 1", "Assignment 2"}
    assert all(r["weight"] == 20.0 for r in assignment_rows)

    lab_rows = [r for r in standalone if r["category"] == "Lab"]
    assert len(lab_rows) == 5
    assert all(r["weight"] == 4.0 for r in lab_rows)


def test_process_assessments_custom_threshold():
    """Test process_assessments allows custom min_category_threshold."""
    from src.core.services.grade_calculator import process_assessments

    items = [
        {"name": f"Exercise {i}", "weight": 5.0, "score": 4.5, "category": "Exercise"}
        for i in range(1, 4)
    ]
    # With default threshold=5, 3 items remain standalone
    default_result = process_assessments(items)
    assert "Exercise" not in default_result["grouped_assessments"]
    assert len(default_result["standalone_assessments"]) == 3

    # With custom threshold=2, 3 items form a group
    custom_result = process_assessments(items, min_category_threshold=2)
    assert "Exercise" in custom_result["grouped_assessments"]
    assert len(custom_result["grouped_assessments"]["Exercise"]["rows"]) == 3
    assert len(custom_result["standalone_assessments"]) == 0


def test_calculate_subject_summary_category_grouping(session, sample_data):
    """
    Test calculate_subject_summary groups types with count > 5,
    and leaves types with count <= 5 ungrouped at root level with accurate calculations.
    """
    subject_id = sample_data["subject"].id

    # 6 Quizzes (> 5) -> should form a grouped row
    quizzes = [
        Assignment(
            subject_id=subject_id,
            assessment=f"Quiz {i}",
            category="Quiz",
            weighted_mark=2.0,
            mark_weight=2.0,
            unweighted_mark=1.0,
            grade_type="numeric",
            is_exam=False,
        )
        for i in range(1, 7)
    ]
    # 2 Assignments (<= 5) -> should remain standalone at root level
    a1 = Assignment(
        subject_id=subject_id,
        assessment="Assignment 1",
        category="Assignment",
        weighted_mark=18.0,
        mark_weight=20.0,
        unweighted_mark=0.90,
        grade_type="numeric",
        is_exam=False,
    )
    a2 = Assignment(
        subject_id=subject_id,
        assessment="Assignment 2",
        category="Assignment",
        weighted_mark=16.0,
        mark_weight=20.0,
        unweighted_mark=0.80,
        grade_type="numeric",
        is_exam=False,
    )
    # 1 Presentation (<= 5) -> standalone
    p1 = Assignment(
        subject_id=subject_id,
        assessment="Oral Presentation",
        category="Presentation",
        weighted_mark=8.0,
        mark_weight=10.0,
        unweighted_mark=0.80,
        grade_type="numeric",
        is_exam=False,
    )
    # Exam (weight 38.0 < 40.0) -> single exam item
    exam = Examination(
        subject_id=subject_id,
        exam_mark=36.0,
        exam_weight=38.0,
    )

    session.add_all(quizzes + [a1, a2, p1, exam])
    session.commit()

    calc = GradeCalculator(session)
    result = calc.calculate_subject_summary(sample_data["subject"])

    summaries = result["summaries"]

    # Check the grouped Quiz row
    quiz_rows = [r for r in summaries if r["label"] == "Quiz"]
    assert len(quiz_rows) == 1
    quiz_row = quiz_rows[0]
    assert quiz_row["display_status"] == "Grouped"
    assert quiz_row["count"] == 6
    assert quiz_row["total_weight"] == 12.0
    assert quiz_row["weighted_score"] == 12.0
    assert quiz_row["unweighted_avg"] == 1.0

    # Check standalone Assignment rows (NOT grouped together and NOT in 'Other')
    assert not any(r["label"] == "Assignment" and r["display_status"] == "Grouped" for r in summaries)
    assert not any(r["label"] == "Other" for r in summaries)

    a1_row = next(r for r in summaries if r["label"] == "Assignment 1")
    assert a1_row["display_status"] == "Standalone"
    assert a1_row["category"] == "Assignment"
    assert a1_row["count"] == 1
    assert a1_row["total_weight"] == 20.0
    assert a1_row["weighted_score"] == 18.0
    assert a1_row["unweighted_avg"] == 0.90

    a2_row = next(r for r in summaries if r["label"] == "Assignment 2")
    assert a2_row["display_status"] == "Standalone"
    assert a2_row["category"] == "Assignment"
    assert a2_row["count"] == 1
    assert a2_row["total_weight"] == 20.0
    assert a2_row["weighted_score"] == 16.0
    assert a2_row["unweighted_avg"] == 0.80

    p1_row = next(r for r in summaries if r["label"] == "Oral Presentation")
    assert p1_row["display_status"] == "Standalone"
    assert p1_row["category"] == "Presentation"
    assert p1_row["count"] == 1
    assert p1_row["total_weight"] == 10.0
    assert p1_row["weighted_score"] == 8.0

    # Downstream calculations:
    # 12.0 (quizzes) + 18.0 (a1) + 16.0 (a2) + 8.0 (p1) + 36.0 (exam) = 90.0
    assert result["total_achieved"] == 90.0
    assert result["remaining_weight"] == 0.0
    assert result["is_fully_graded"] is True

    # High Distinction (85) should be Achieved
    hd_goal = next(g for g in result["grade_goals"] if g["label"] == "High Distinction")
    assert hd_goal["status"] == "Achieved"

    # Test sync_subject_total reflects this
    synced_total = calc.sync_subject_total(subject_id)
    assert synced_total == 90.0
