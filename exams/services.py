from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone


def grade_submission(submission):
    """Sum marks for correctly-answered questions — options by is_correct, blanks by accepted answers."""
    total = 0
    for answer in submission.answers.select_related("question", "selected_choice"):
        question = answer.question
        if question.is_blank:
            correct = question.is_correct_text(answer.text_answer)
        else:
            correct = bool(answer.selected_choice and answer.selected_choice.is_correct)
        if correct:
            total += question.marks
    submission.score = total
    submission.save(update_fields=["score"])
    return total


def push_submission_to_raddai(submission, timeout=30):
    """
    Push a graded submission's score into the matching Result slot on
    raddai-backend. Returns (success, error_message); a failure is also
    recorded on the submission.
    """
    student = submission.student
    exam = submission.exam

    payload = {
        "student_id": student.raddai_id,
        "subject_id": exam.subject.raddai_id,
        "academic_year_id": exam.academic_year.raddai_id,
        "term": exam.term,
        "score_field": exam.raddai_score_field,
        "score": str(submission.score),
    }

    try:
        response = requests.post(
            f"{settings.RADDAI_API_BASE_URL}/exam-portal/results/",
            json=payload,
            headers={"X-Exam-Portal-Key": settings.EXAM_PORTAL_API_KEY},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return _record_push_failure(submission, str(exc))

    if response.status_code >= 400:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        return _record_push_failure(submission, detail)

    submission.pushed_to_raddai = True
    submission.pushed_at = timezone.now()
    submission.push_error = ""
    submission.push_failed_at = None
    submission.save(update_fields=["pushed_to_raddai", "pushed_at", "push_error", "push_failed_at"])
    return True, None


def _record_push_failure(submission, error):
    submission.push_error = str(error)[:500]
    submission.push_failed_at = timezone.now()
    submission.save(update_fields=["push_error", "push_failed_at"])
    return False, error
