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


def review_submission(submission):
    """
    Every question of the exam with what the student answered and what the
    answer key says, for management reviewing a score dispute. Marking mirrors
    grade_submission. A question with no saved answer counts as unanswered.
    Returns (rows, marks_from_key) — the second is what the key as it stands
    today would score, which differs from submission.score only if a
    question's answer was edited after the exam.
    """
    answers = {a.question_id: a for a in submission.answers.select_related("selected_choice")}
    rows = []
    marks = 0
    for number, question in enumerate(submission.exam.questions.prefetch_related("choices"), start=1):
        answer = answers.get(question.pk)
        if question.is_blank:
            given = (answer.text_answer if answer else "").strip()
            correct_text = " / ".join(a for a in question.accepted_answers if str(a).strip())
            is_correct = bool(answer) and question.is_correct_text(answer.text_answer)
        else:
            given = answer.selected_choice.text if answer and answer.selected_choice else ""
            correct_text = " / ".join(c.text for c in question.choices.all() if c.is_correct)
            is_correct = bool(answer and answer.selected_choice and answer.selected_choice.is_correct)
        earned = question.marks if is_correct else 0
        marks += earned
        rows.append({
            "number": number,
            "question": question,
            "given": given,
            "correct_answer": correct_text,
            "is_correct": is_correct,
            "marks": earned,
        })
    return rows, marks


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
