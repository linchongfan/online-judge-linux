from fastapi import APIRouter, Query, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, Db, OptionalUser
from app.core.exceptions import AppError
from app.models import Contest, ContestProblem, ContestPublication, Problem, ProblemStatus, Tag, UserRole, utcnow
from app.schemas.common import Page, make_page
from app.schemas.problem import ProblemDetail, ProblemListItem, ProblemPosition, SolutionResponse, TagResponse
from app.schemas.submission import SampleRunCreate, SubmissionAccepted
from app.services.contests import can_view_contest_problem, get_contest
from app.services.problems import (
    get_problem,
    list_problems,
    problem_stats_bulk,
    random_problem,
    serialize_detail,
    serialize_list_item,
)
from app.services.submissions import create_sample_run

router = APIRouter()
tags_router = APIRouter()


@router.get("", response_model=Page[ProblemListItem])
def problems(
    db: Db,
    user: OptionalUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = Query(None, max_length=100),
    difficulty: str | None = Query(None, max_length=10),
    tag: str | None = Query(None, max_length=40),
    progress: str | None = Query(None, pattern="^(solved|attempted|untouched)$"),
):
    if progress and user is None:
        raise AppError(401, "AUTH_REQUIRED", "登录后才能按完成状态筛选")
    items, total = list_problems(db, user, page, page_size, keyword, difficulty, tag, progress)
    user_id = user.id if user else None
    stats = problem_stats_bulk(db, [item.id for item in items], user_id)
    return make_page(
        [serialize_list_item(db, item, user_id, stats=stats[item.id]) for item in items], total, page, page_size
    )


@router.get("/random", response_model=ProblemDetail)
def random(db: Db, user: OptionalUser):
    return serialize_detail(db, random_problem(db), user.id if user else None)


@router.get("/{problem_id}", response_model=ProblemDetail)
def problem_detail(problem_id: int, db: Db, user: OptionalUser, contest_id: int | None = Query(None, ge=1)):
    include_unpublished = bool(user and user.role == UserRole.ADMIN)
    if contest_id:
        contest = get_contest(db, contest_id)
        if not any(entry.problem_id == problem_id for entry in contest.problems):
            raise AppError(404, "CONTEST_PROBLEM_NOT_FOUND", "该题不属于此竞赛")
        if not can_view_contest_problem(db, contest, user):
            raise AppError(403, "CONTEST_PROBLEM_LOCKED", "当前不能查看该赛题")
        include_unpublished = True
    problem = get_problem(db, problem_id, user, include_unpublished)
    return serialize_detail(db, problem, user.id if user else None)


@router.get("/{problem_id}/position", response_model=ProblemPosition)
def problem_position(problem_id: int, db: Db, user: OptionalUser, contest_id: int | None = Query(None, ge=1)):
    if contest_id:
        contest = get_contest(db, contest_id)
        is_admin = bool(user and user.role == UserRole.ADMIN)
        if not is_admin and not can_view_contest_problem(db, contest, user):
            raise AppError(403, "CONTEST_PROBLEM_LOCKED", "当前不能查看该赛题")
        ids = [entry.problem_id for entry in contest.problems]
    else:
        ids = list(
            db.scalars(select(Problem.id).where(Problem.status == ProblemStatus.PUBLISHED).order_by(Problem.id)).all()
        )
    if problem_id not in ids:
        raise AppError(404, "PROBLEM_NOT_FOUND", "题目不存在或尚未发布")
    index = ids.index(problem_id)
    return {
        "index": index,
        "total": len(ids),
        "prev_id": ids[index - 1] if index > 0 else None,
        "next_id": ids[index + 1] if index + 1 < len(ids) else None,
    }


@router.get("/{problem_id}/solution", response_model=SolutionResponse)
def problem_solution(problem_id: int, db: Db, user: OptionalUser):
    problem = get_problem(db, problem_id, user, include_unpublished=bool(user and user.role == UserRole.ADMIN))
    now = utcnow()
    ongoing = db.scalar(
        select(ContestProblem)
        .join(Contest, Contest.id == ContestProblem.contest_id)
        .where(
            ContestProblem.problem_id == problem.id,
            Contest.status == ContestPublication.PUBLISHED,
            Contest.start_time <= now,
            Contest.end_time > now,
        )
    )
    if ongoing:
        raise AppError(403, "SOLUTION_HIDDEN_DURING_CONTEST", "竞赛进行期间不开放官方题解")
    solution = problem.solution
    if solution is None or not solution.enabled:
        raise AppError(404, "SOLUTION_NOT_FOUND", "该题暂无官方题解")
    return {
        "title": solution.title,
        "summary": solution.summary,
        "steps": solution.steps,
        "time_complexity": solution.time_complexity,
        "space_complexity": solution.space_complexity,
        "codes": solution.code_variants,
    }


@router.post("/{problem_id}/sample-runs", response_model=SubmissionAccepted, status_code=status.HTTP_202_ACCEPTED)
def run_sample(problem_id: int, data: SampleRunCreate, db: Db, user: CurrentUser):
    return create_sample_run(db, problem_id, data, user)


@tags_router.get("", response_model=list[TagResponse])
def tags(db: Db):
    rows = db.execute(
        select(Tag, func.count(Problem.id))
        .outerjoin(Tag.problems.and_(Problem.status == ProblemStatus.PUBLISHED))
        .group_by(Tag.id)
        .order_by(Tag.name)
    ).all()
    return [{"id": tag.id, "name": tag.name, "problem_count": count} for tag, count in rows]
