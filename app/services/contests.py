from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.security import utc_datetime
from app.models import (
    PENALTY_STATUSES,
    Contest,
    ContestProblem,
    ContestProblemResult,
    ContestPublication,
    ContestRegistration,
    ContestResult,
    JudgeStatus,
    Problem,
    RegistrationStatus,
    Submission,
    User,
    UserRole,
    utcnow,
)
from app.schemas.contest import ContestWriteRequest
from app.services.problems import current_test_data


def contest_lifecycle(contest: Contest, now: datetime | None = None) -> str:
    current = now or utcnow()
    if current < utc_datetime(contest.start_time):
        return "upcoming"
    if current >= utc_datetime(contest.end_time):
        return "ended"
    return "ongoing"


def get_contest(db: Session, contest_id: int, include_unpublished: bool = False) -> Contest:
    contest = db.get(Contest, contest_id)
    if contest is None or (not include_unpublished and contest.status != ContestPublication.PUBLISHED):
        raise AppError(404, "CONTEST_NOT_FOUND", "竞赛不存在")
    return contest


def registration_for(db: Session, contest_id: int, user_id: int | None) -> ContestRegistration | None:
    if user_id is None:
        return None
    return db.scalar(
        select(ContestRegistration).where(
            ContestRegistration.contest_id == contest_id,
            ContestRegistration.user_id == user_id,
            ContestRegistration.status == RegistrationStatus.REGISTERED,
        )
    )


def serialize_contest(db: Session, contest: Contest, user: User | None, include_problems: bool = False) -> dict:
    now = utcnow()
    lifecycle = contest_lifecycle(contest, now)
    registration = registration_for(db, contest.id, user.id if user else None)
    participants = (
        db.scalar(
            select(func.count(ContestRegistration.id)).where(
                ContestRegistration.contest_id == contest.id,
                ContestRegistration.status == RegistrationStatus.REGISTERED,
            )
        )
        or 0
    )
    problems = []
    can_view = (
        bool(user and user.role == UserRole.ADMIN)
        or lifecycle == "ended"
        or (lifecycle == "ongoing" and registration is not None)
    )
    if include_problems and can_view:
        # Two aggregate queries for the whole contest instead of 2-3 per problem.
        problem_ids = [entry.problem_id for entry in contest.problems]
        stats_rows = db.execute(
            select(
                Submission.problem_id,
                func.count(Submission.id),
                func.count(func.distinct(case((Submission.status == JudgeStatus.AC, Submission.user_id)))),
            )
            .where(
                Submission.contest_id == contest.id,
                Submission.is_sample_run.is_(False),
                Submission.problem_id.in_(problem_ids),
            )
            .group_by(Submission.problem_id)
        ).all()
        stats_by_problem = {problem_id: (attempts, solved) for problem_id, attempts, solved in stats_rows}
        user_status_by_problem: dict[int, str] = {}
        if user:
            user_rows = db.execute(
                select(
                    Submission.problem_id,
                    func.max(case((Submission.status == JudgeStatus.AC, 1), else_=0)),
                )
                .where(
                    Submission.contest_id == contest.id,
                    Submission.problem_id.in_(problem_ids),
                    Submission.user_id == user.id,
                    Submission.is_sample_run.is_(False),
                )
                .group_by(Submission.problem_id)
            ).all()
            user_status_by_problem = {
                problem_id: "AC" if has_ac else "WA" for problem_id, has_ac in user_rows
            }
        for entry in contest.problems:
            attempts, solved = stats_by_problem.get(entry.problem_id, (0, 0))
            problems.append(
                {
                    "label": entry.label,
                    "problem_id": entry.problem_id,
                    "title": entry.problem.title,
                    "difficulty": entry.problem.difficulty,
                    "solved": solved,
                    "attempts": attempts,
                    "user_status": user_status_by_problem.get(entry.problem_id, "untouched"),
                }
            )
    return {
        "id": contest.id,
        "title": contest.title,
        "subtitle": contest.subtitle,
        "description": contest.description,
        "organizer": contest.organizer,
        "start_time": contest.start_time,
        "end_time": contest.end_time,
        "duration_seconds": int((utc_datetime(contest.end_time) - utc_datetime(contest.start_time)).total_seconds()),
        "status": lifecycle,
        "publication_status": contest.status,
        "participants": participants,
        "expected_participants": contest.expected_participants,
        "registered": registration is not None,
        "accent": contest.accent,
        "server_time": now,
        "problems": problems,
    }


def register_for_contest(db: Session, contest: Contest, user: User) -> None:
    now = utcnow()
    if contest.status != ContestPublication.PUBLISHED or now >= utc_datetime(contest.start_time):
        raise AppError(409, "REGISTRATION_CLOSED", "当前竞赛已停止报名")
    registration = db.scalar(
        select(ContestRegistration).where(
            ContestRegistration.contest_id == contest.id, ContestRegistration.user_id == user.id
        )
    )
    if registration is None:
        registration = ContestRegistration(contest_id=contest.id, user_id=user.id)
        db.add(registration)
    registration.status = RegistrationStatus.REGISTERED
    db.flush()
    recompute_contest_results(db, contest)


def cancel_registration(db: Session, contest: Contest, user: User) -> None:
    if utcnow() >= utc_datetime(contest.start_time):
        raise AppError(409, "CANCELLATION_CLOSED", "竞赛开始后不能取消报名")
    registration = registration_for(db, contest.id, user.id)
    if registration is not None:
        registration.status = RegistrationStatus.CANCELLED
        result = db.scalar(
            select(ContestResult).where(ContestResult.contest_id == contest.id, ContestResult.user_id == user.id)
        )
        if result:
            db.delete(result)
        db.flush()


def ensure_contest_submission_allowed(db: Session, contest: Contest, problem_id: int, user: User) -> None:
    now = utcnow()
    if contest.status != ContestPublication.PUBLISHED:
        raise AppError(409, "CONTEST_NOT_ACTIVE", "竞赛未开放")
    if now < utc_datetime(contest.start_time) or now >= utc_datetime(contest.end_time):
        raise AppError(409, "CONTEST_NOT_ACTIVE", "当前不在竞赛提交时间内")
    if registration_for(db, contest.id, user.id) is None:
        raise AppError(403, "REGISTRATION_REQUIRED", "报名后才能提交竞赛题目")
    if not any(entry.problem_id == problem_id for entry in contest.problems):
        raise AppError(404, "CONTEST_PROBLEM_NOT_FOUND", "该题不属于此竞赛")


def can_view_contest_problem(db: Session, contest: Contest, user: User | None) -> bool:
    lifecycle = contest_lifecycle(contest)
    return lifecycle == "ended" or (
        lifecycle == "ongoing" and registration_for(db, contest.id, user.id if user else None) is not None
    )


def upsert_contest(db: Session, data: ContestWriteRequest, contest: Contest | None = None) -> Contest:
    if contest is None:
        contest = Contest(**data.model_dump(exclude={"problems"}))
        db.add(contest)
        db.flush()
    else:
        if contest_lifecycle(contest) != "upcoming":
            raise AppError(409, "CONTEST_ALREADY_STARTED", "已开始或结束的竞赛不能直接编辑，请使用成绩重算或归档")
        for field in (
            "title",
            "subtitle",
            "description",
            "organizer",
            "start_time",
            "end_time",
            "accent",
            "expected_participants",
        ):
            setattr(contest, field, getattr(data, field))
    problem_ids = [item.problem_id for item in data.problems]
    if problem_ids:
        found = db.scalars(select(Problem.id).where(Problem.id.in_(problem_ids))).all()
        if set(found) != set(problem_ids):
            raise AppError(400, "INVALID_CONTEST_PROBLEMS", "竞赛包含不存在的题目")
    # Delete the previous rows before inserting replacements: SQLite enforces
    # the (contest_id, problem_id) unique key immediately, so relying on
    # delete-orphan flush ordering can otherwise fail with a duplicate key.
    contest.problems.clear()
    db.flush()
    contest.problems = [
        ContestProblem(contest_id=contest.id, problem_id=item.problem_id, label=item.label, position=item.position)
        for item in sorted(data.problems, key=lambda item: item.position)
    ]
    db.flush()
    return contest


def publish_contest(db: Session, contest: Contest) -> None:
    if not contest.problems:
        raise AppError(409, "CONTEST_PROBLEMS_REQUIRED", "发布竞赛前必须配置赛题")
    for entry in contest.problems:
        if current_test_data(db, entry.problem_id) is None:
            raise AppError(409, "VALID_TEST_DATA_REQUIRED", f"赛题 {entry.problem_id} 尚无有效测试数据")
    if utc_datetime(contest.end_time) <= utcnow():
        raise AppError(409, "CONTEST_ALREADY_ENDED", "不能发布已结束的竞赛")
    contest.status = ContestPublication.PUBLISHED


def recompute_contest_results(db: Session, contest: Contest) -> None:
    # Serialize recomputation for a contest; submission details remain the
    # authoritative source. Reads are a fixed number of queries regardless of
    # participant/problem count (the previous per-user-per-problem loop issued
    # O(users x problems) queries on every result, right at contest peak load).
    db.scalar(select(Contest.id).where(Contest.id == contest.id).with_for_update())
    registrations = db.scalars(
        select(ContestRegistration).where(
            ContestRegistration.contest_id == contest.id,
            ContestRegistration.status == RegistrationStatus.REGISTERED,
        )
    ).all()
    registered_ids = [registration.user_id for registration in registrations]
    problem_ids = [entry.problem_id for entry in contest.problems]

    submissions = db.scalars(
        select(Submission)
        .where(
            Submission.contest_id == contest.id,
            Submission.is_sample_run.is_(False),
            Submission.user_id.in_(registered_ids),
            Submission.problem_id.in_(problem_ids),
        )
        .order_by(Submission.user_id, Submission.problem_id, Submission.created_at, Submission.id)
    ).all()
    grouped: dict[tuple[int, int], list[Submission]] = {}
    for item in submissions:
        grouped.setdefault((item.user_id, item.problem_id), []).append(item)

    problem_results = {
        (result.user_id, result.problem_id): result
        for result in db.scalars(
            select(ContestProblemResult).where(ContestProblemResult.contest_id == contest.id)
        ).all()
    }
    contest_results = {
        result.user_id: result
        for result in db.scalars(select(ContestResult).where(ContestResult.contest_id == contest.id)).all()
    }

    aggregates: list[tuple[int, int, int]] = []
    for user_id in registered_ids:
        solved_count = 0
        total_penalty = 0
        for problem_id in problem_ids:
            items = grouped.get((user_id, problem_id), [])
            first_ac = next((item for item in items if item.status == JudgeStatus.AC), None)
            wrong_attempts = sum(
                item.status in PENALTY_STATUSES
                and (first_ac is None or (item.created_at, item.id) < (first_ac.created_at, first_ac.id))
                for item in items
            )
            if first_ac is not None:
                solved_count += 1
                total_penalty += int(
                    (utc_datetime(first_ac.created_at) - utc_datetime(contest.start_time)).total_seconds()
                )
                total_penalty += wrong_attempts * 20 * 60
            result = problem_results.get((user_id, problem_id))
            if result is None:
                result = ContestProblemResult(contest_id=contest.id, user_id=user_id, problem_id=problem_id)
                db.add(result)
                problem_results[(user_id, problem_id)] = result
            result.solved = first_ac is not None
            result.first_ac_at = first_ac.created_at if first_ac else None
            result.wrong_attempts = wrong_attempts
            result.attempts = len(items)
        aggregates.append((user_id, solved_count, total_penalty))

    aggregates.sort(key=lambda item: (-item[1], item[2], item[0]))
    previous_score: tuple[int, int] | None = None
    previous_rank = 0
    for index, (user_id, solved_count, penalty) in enumerate(aggregates, start=1):
        score = (solved_count, penalty)
        rank = previous_rank if score == previous_score else index
        previous_score, previous_rank = score, rank
        contest_result = contest_results.get(user_id)
        if contest_result is None:
            contest_result = ContestResult(contest_id=contest.id, user_id=user_id)
            db.add(contest_result)
            contest_results[user_id] = contest_result
        contest_result.solved_count = solved_count
        contest_result.penalty_seconds = penalty
        contest_result.rank = rank
    db.flush()


def ranking(db: Session, contest: Contest) -> list[dict]:
    registrations = db.scalars(
        select(ContestRegistration).where(
            ContestRegistration.contest_id == contest.id,
            ContestRegistration.status == RegistrationStatus.REGISTERED,
        )
    ).all()
    user_ids = [item.user_id for item in registrations]
    if not user_ids:
        return []
    users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()}
    results = db.scalars(select(ContestResult).where(ContestResult.contest_id == contest.id)).all()
    by_user = {result.user_id: result for result in results}
    ordered = sorted(
        user_ids,
        key=lambda user_id: (
            by_user[user_id].rank if user_id in by_user else 10**9,
            -(by_user[user_id].solved_count if user_id in by_user else 0),
            by_user[user_id].penalty_seconds if user_id in by_user else 0,
            user_id,
        ),
    )
    entries = []
    for user_id in ordered:
        result = by_user.get(user_id)
        problem_results = db.scalars(
            select(ContestProblemResult).where(
                ContestProblemResult.contest_id == contest.id, ContestProblemResult.user_id == user_id
            )
        ).all()
        result_by_problem = {item.problem_id: item for item in problem_results}
        per_problem = {}
        for entry in contest.problems:
            item = result_by_problem.get(entry.problem_id)
            if item is None or item.attempts == 0:
                per_problem[entry.label] = {"status": "none", "attempts": 0, "wrong_attempts": 0}
            else:
                per_problem[entry.label] = {
                    "status": "AC" if item.solved else "WA",
                    "time_seconds": int(
                        (utc_datetime(item.first_ac_at) - utc_datetime(contest.start_time)).total_seconds()
                    )
                    if item.first_ac_at
                    else None,
                    "attempts": item.attempts,
                    "wrong_attempts": item.wrong_attempts,
                }
        entries.append(
            {
                "rank": result.rank if result else 0,
                "username": users[user_id].username,
                "nickname": users[user_id].nickname,
                "solved": result.solved_count if result else 0,
                "penalty_seconds": result.penalty_seconds if result else 0,
                "results": per_problem,
            }
        )
    return entries
