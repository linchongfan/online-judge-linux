from __future__ import annotations

from sqlalchemy import select

from app.core.security import verify_password
from app.models import User, UserRole
from scripts.ensure_default_users import ensure_default_users, generate_password


def test_default_accounts_are_created_with_expected_roles(db_session):
    messages = ensure_default_users(
        db_session, admin_password="Admin12345", test_password="Test12345"
    )

    assert "created" in messages[0]
    assert "created" in messages[1]
    admin = db_session.scalar(select(User).where(User.username == "admin@123321"))
    test = db_session.scalar(select(User).where(User.username == "test@123456"))
    assert admin is not None and admin.role is UserRole.ADMIN
    assert test is not None and test.role is UserRole.USER
    assert verify_password(admin.password_hash, "Admin12345")
    assert verify_password(test.password_hash, "Test12345")


def test_generated_passwords_are_random_and_never_equal_username(db_session):
    generated: dict[str, tuple[str, str]] = {}
    ensure_default_users(db_session, generated_out=generated)

    assert set(generated) == {"admin", "test"}
    admin = db_session.scalar(select(User).where(User.username == "admin@123321"))
    test = db_session.scalar(select(User).where(User.username == "test@123456"))
    assert admin is not None and test is not None
    for kind, user in (("admin", admin), ("test", test)):
        username, password = generated[kind]
        assert username == user.username
        assert password != username
        assert len(password) >= 8
        assert any(c.isalpha() for c in password) and any(c.isdigit() for c in password)
        assert verify_password(user.password_hash, password)
    assert generated["admin"][1] != generated["test"][1]


def test_generate_password_satisfies_policy():
    for _ in range(20):
        password = generate_password()
        assert len(password) >= 8
        assert any(c.isalpha() for c in password)
        assert any(c.isdigit() for c in password)


def test_default_account_creation_is_idempotent(db_session):
    ensure_default_users(db_session, admin_password="Admin12345", test_password="Test12345")
    second_run = ensure_default_users(db_session)

    assert second_run == [
        "admin: account 'admin@123321' already exists, skipped",
        "test: account 'test@123456' already exists, skipped",
    ]
    assert len(db_session.scalars(select(User)).all()) == 2


def test_credentials_file_is_private(tmp_path, monkeypatch):
    import os

    from scripts.ensure_default_users import write_credentials_file

    target = tmp_path / "initial-credentials"
    monkeypatch.setenv("INITIAL_CREDENTIALS_FILE", str(target))
    path = write_credentials_file({"admin": ("admin@123321", "RandomPass123")})
    assert path == target
    content = target.read_text(encoding="utf-8")
    assert "admin@123321" in content and "RandomPass123" in content
    if os.name != "nt":
        # Windows cannot express 0600; production targets are Linux.
        assert target.stat().st_mode & 0o077 == 0