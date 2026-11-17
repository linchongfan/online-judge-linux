"""Tests for the reliability core: outbox dispatch, result consumption,
expired-task sweeping and nonce verification. These modules carry the
"no submission is ever lost" guarantee and previously had no coverage.
"""

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import AppError
from app.core.signing import sign_result
from app.models import (
    Difficulty,
    JudgeStatus,
    JudgeTask,
    Language,
    OutboxMessage,
    OutboxStatus,
    Problem,
    ProblemStatus,
    Submission,
    User,
    UserRole,
    UserStatus,
    utcnow,
)
from app.models import TestCase as DbTestCase
from app.models import TestDataStatus as DbTestDataStatus
from app.models import TestDataVersion as DbTestDataVersion
from app.tasks import consumer, dispatcher, results, sweeper


def _hex() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def pipeline_db(db_session, tmp_path, monkeypatch):
    """A submission/task/outbox row set plus real test-data files on disk."""
    user = User(
        username=f"u{_hex()[:10]}",
        nickname="管道测试",
        email=f"{_hex()[:10]}@example.com",
        password_hash="not-used",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    problem = Problem(
        title="A+B",
        difficulty=Difficulty.EASY,
        input_description="两个整数",
        output_description="和",
        status=ProblemStatus.PUBLISHED,
    )
    db_session.add_all([user, problem])
    db_session.flush()
    (tmp_path / "1.in").write_text("1 2\n", encoding="utf-8")
    (tmp_path / "1.out").write_text("3\n", encoding="utf-8")
    version = DbTestDataVersion(
        problem_id=problem.id,
        version=1,
        status=DbTestDataStatus.VALID,
        is_current=True,
        storage_path=str(tmp_path),
        original_filename="data.zip",
        compressed_size=10,
    )
    db_session.add(version)
    db_session.flush()
    db_session.add(
        DbTestCase(
            version_id=version.id,
            case_number=1,
            input_key="1.in",
            output_key="1.out",
            is_sample=True,
            input_size=4,
            output_size=2,
        )
    )
    submission = Submission(
        user_id=user.id,
        problem_id=problem.id,
        language=Language.PYTHON,
        source_code="print(1)",
        idempotency_key=_hex(),
        test_data_version_id=version.id,
    )
    db_session.add(submission)
    db_session.flush()
    task = JudgeTask(submission_id=submission.id)
    db_session.add(task)
    db_session.flush()
    outbox = OutboxMessage(task_id=task.id)
    db_session.add(outbox)
    db_session.commit()

    TestingSession = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(dispatcher, "SessionLocal", TestingSession)
    monkeypatch.setattr(results, "SessionLocal", TestingSession)
    monkeypatch.setattr(sweeper, "SessionLocal", TestingSession)
    return {
        "db": db_session,
        "user": user,
        "problem": problem,
        "version": version,
        "submission": submission,
        "task": task,
        "outbox": outbox,
    }


def _result_payload(env: dict, status: str = "AC", nonce: str | None = None) -> dict:
    return {
        "task_id": env["task"].id,
        "submission_id": env["submission"].id,
        "status": status,
        "runtime_ms": 5,
        "memory_kb": 1024,
        "diagnostic": "",
        "attempt": env["task"].attempt,
        "test_data_version_id": env["submission"].test_data_version_id,
        "nonce": nonce if nonce is not None else (env["task"].nonce or "test-nonce-123"),
    }


# ---------------------------------------------------------------- dispatcher


def test_dispatch_outbox_sends_payload_and_persists_nonce(pipeline_db, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(dispatcher.celery_app, "send_task", lambda name, **kw: sent.append({"name": name, **kw}))

    assert dispatcher.dispatch_outbox(pipeline_db["task"].id) is True
    assert len(sent) == 1
    payload = sent[0]["kwargs"]["payload"]
    assert sent[0]["name"] == "judge.execute"
    assert payload["task_id"] == pipeline_db["task"].id
    assert payload["cases"][0]["input_data"] == "1 2\n"
    assert payload["nonce"]

    db = pipeline_db["db"]
    db.expire_all()
    task = db.get(JudgeTask, pipeline_db["task"].id)
    outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.task_id == task.id))
    assert task.nonce == payload["nonce"]
    assert task.status == JudgeStatus.PENDING
    assert outbox.status == OutboxStatus.DISPATCHED

    # Idempotent: a dispatched outbox is never sent twice.
    assert dispatcher.dispatch_outbox(task.id) is True
    assert len(sent) == 1


def test_dispatch_outbox_failure_keeps_outbox_pending(pipeline_db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(dispatcher.celery_app, "send_task", boom)
    assert dispatcher.dispatch_outbox(pipeline_db["task"].id) is False
    db = pipeline_db["db"]
    db.expire_all()
    outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.task_id == pipeline_db["task"].id))
    assert outbox.status == OutboxStatus.PENDING
    assert outbox.attempts == 1
    assert "broker down" in outbox.last_error


# ------------------------------------------------------------------ consumer


class FakeRedis:
    def __init__(self):
        self.queues: dict[str, list] = {}

    def rpush(self, key, value):
        self.queues.setdefault(key, []).append(value)

    def lrem(self, key, count, value):
        items = self.queues.get(key, [])
        if value in items:
            items.remove(value)
            return 1
        return 0


def _envelope(payload: dict, attempts: int = 0) -> bytes:
    return json.dumps({"payload": payload, "signature": sign_result(payload), "attempts": attempts}).encode()


def test_consumer_dead_letters_invalid_json_and_bad_signature():
    client = FakeRedis()
    consumer._handle_message(client, b"not json")
    assert len(client.queues[consumer.DEAD_LETTER_QUEUE]) == 1
    assert client.queues.get(consumer.PROCESSING_QUEUE, []) == []

    client = FakeRedis()
    payload = {"event": "started"}
    bad = json.dumps({"payload": payload, "signature": "forged"}).encode()
    consumer._handle_message(client, bad)
    assert len(client.queues[consumer.DEAD_LETTER_QUEUE]) == 1


def test_consumer_processes_valid_signed_result(pipeline_db):
    client = FakeRedis()
    raw = _envelope(_result_payload(pipeline_db))
    client.rpush(consumer.PROCESSING_QUEUE, raw)
    consumer._handle_message(client, raw)
    assert client.queues.get(consumer.PROCESSING_QUEUE, []) == []
    assert consumer.DEAD_LETTER_QUEUE not in client.queues
    db = pipeline_db["db"]
    db.expire_all()
    assert db.get(Submission, pipeline_db["submission"].id).status == JudgeStatus.AC


def test_consumer_requeues_transient_failure_with_bounded_attempts(pipeline_db, monkeypatch):
    def transient(_data):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(consumer, "process_judge_result", transient)
    client = FakeRedis()
    raw = _envelope(_result_payload(pipeline_db))
    client.rpush(consumer.PROCESSING_QUEUE, raw)
    consumer._handle_message(client, raw)
    requeued = client.queues[consumer.QUEUE]
    assert len(requeued) == 1
    assert json.loads(requeued[0])["attempts"] == 1
    assert client.queues.get(consumer.PROCESSING_QUEUE, []) == []
    assert consumer.DEAD_LETTER_QUEUE not in client.queues


def test_consumer_dead_letters_after_max_transient_attempts(pipeline_db, monkeypatch):
    def transient(_data):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(consumer, "process_judge_result", transient)
    client = FakeRedis()
    raw = _envelope(_result_payload(pipeline_db), attempts=consumer.MAX_REQUEUE_ATTEMPTS - 1)
    client.rpush(consumer.PROCESSING_QUEUE, raw)
    consumer._handle_message(client, raw)
    assert len(client.queues[consumer.DEAD_LETTER_QUEUE]) == 1
    assert consumer.QUEUE not in client.queues


def test_consumer_dead_letters_permanent_state_conflict(pipeline_db):
    client = FakeRedis()
    payload = _result_payload(pipeline_db)
    payload["attempt"] = 99  # identity mismatch -> permanent AppError
    raw = _envelope(payload)
    client.rpush(consumer.PROCESSING_QUEUE, raw)
    consumer._handle_message(client, raw)
    assert len(client.queues[consumer.DEAD_LETTER_QUEUE]) == 1
    assert consumer.QUEUE not in client.queues


# ------------------------------------------------------------------- sweeper


def test_sweeper_retries_task_lost_in_queue(pipeline_db, monkeypatch):
    dispatched: list[str] = []
    monkeypatch.setattr(dispatcher, "dispatch_outbox", lambda task_id: dispatched.append(task_id) or True)
    db = pipeline_db["db"]
    outbox = pipeline_db["outbox"]
    outbox.status = OutboxStatus.DISPATCHED
    outbox.dispatched_at = utcnow() - timedelta(seconds=3600)
    db.commit()

    stats = sweeper.sweep_expired_tasks()
    assert stats["recovered"] == 1
    db.expire_all()
    old = db.get(JudgeTask, pipeline_db["task"].id)
    assert old.status == JudgeStatus.SE
    retry = db.scalar(select(JudgeTask).where(JudgeTask.submission_id == old.submission_id, JudgeTask.attempt == 2))
    assert retry is not None and retry.status == JudgeStatus.PENDING
    assert db.get(Submission, pipeline_db["submission"].id).status == JudgeStatus.PENDING
    assert dispatched == [retry.id]


def test_sweeper_retries_expired_execution_lease(pipeline_db, monkeypatch):
    dispatched: list[str] = []
    monkeypatch.setattr(dispatcher, "dispatch_outbox", lambda task_id: dispatched.append(task_id) or True)
    db = pipeline_db["db"]
    task = pipeline_db["task"]
    task.status = JudgeStatus.JUDGING
    task.lease_expires_at = utcnow() - timedelta(seconds=10)
    db.commit()

    stats = sweeper.sweep_expired_tasks()
    assert stats["recovered"] == 1
    db.expire_all()
    assert db.get(JudgeTask, task.id).status == JudgeStatus.SE
    assert len(dispatched) == 1


def test_sweeper_fails_submission_after_max_attempts(pipeline_db, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(dispatcher, "dispatch_outbox", lambda task_id: True)
    db = pipeline_db["db"]
    task = pipeline_db["task"]
    task.attempt = get_settings().judge_task_max_attempts
    task.status = JudgeStatus.JUDGING
    task.lease_expires_at = utcnow() - timedelta(seconds=10)
    db.commit()

    stats = sweeper.sweep_expired_tasks()
    assert stats["failed"] == 1
    db.expire_all()
    submission = db.get(Submission, pipeline_db["submission"].id)
    assert submission.status == JudgeStatus.SE
    assert submission.finished_at is not None


# --------------------------------------------------------------------- nonce


def test_result_with_wrong_nonce_is_rejected(pipeline_db):
    db = pipeline_db["db"]
    task = pipeline_db["task"]
    task.nonce = "expected-nonce-value"
    db.commit()
    with pytest.raises(AppError) as exc:
        results.process_judge_result(_result_payload(pipeline_db, nonce="wrong-nonce-value"))
    assert exc.value.code == "RESULT_IDENTITY_MISMATCH"
    db.expire_all()
    assert db.get(Submission, pipeline_db["submission"].id).status == JudgeStatus.PENDING


def test_result_with_matching_nonce_is_accepted(pipeline_db):
    db = pipeline_db["db"]
    task = pipeline_db["task"]
    task.nonce = "expected-nonce-value"
    db.commit()
    assert results.process_judge_result(_result_payload(pipeline_db, nonce="expected-nonce-value")) is True
    db.expire_all()
    assert db.get(Submission, pipeline_db["submission"].id).status == JudgeStatus.AC


# ------------------------------------------------- recompute query budget


def test_contest_recompute_uses_bounded_query_count(client, admin_headers, user_headers, db_session):
    from app.models import Contest
    from app.services.contests import recompute_contest_results
    from tests.test_contest_api import contest_payload
    from tests.test_problem_api import create_published_problem

    problem_id = create_published_problem(client, admin_headers)
    created = client.post("/api/v1/admin/contests", json=contest_payload(problem_id), headers=admin_headers)
    assert created.status_code == 201, created.text
    contest_id = created.json()["id"]
    assert client.post(f"/api/v1/admin/contests/{contest_id}/publish", headers=admin_headers).status_code == 200
    assert client.put(f"/api/v1/contests/{contest_id}/registration", headers=user_headers).status_code == 200
    contest = db_session.get(Contest, contest_id)
    contest.start_time = utcnow() - timedelta(minutes=5)
    contest.end_time = utcnow() + timedelta(hours=1)
    db_session.commit()

    counter = {"selects": 0}

    def count_selects(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["selects"] += 1

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        recompute_contest_results(db_session, contest)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
    # The old implementation issued O(users x problems) queries; the rewrite
    # must stay bounded regardless of contest size.
    assert counter["selects"] <= 12, counter