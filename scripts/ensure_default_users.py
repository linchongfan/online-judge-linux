"""Idempotently create the default admin and test accounts after deployment.

Runs automatically at the end of ``deploy/linux/deploy.sh`` (disable with
``CREATE_DEFAULT_USERS=0``). Usernames and emails can be overridden via
environment variables before first deploy; afterwards the script only reports
that the accounts already exist.

Passwords are NEVER hardcoded or printed. When no password override is given,
a random policy-compliant password is generated per account and written to a
root-only credentials file (default /etc/xinbaoyan/initial-credentials,
fallback <storage_root>/initial-credentials). The operator must read that file
once and change the passwords after first login.
"""

from __future__ import annotations

import os
import secrets
import string
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import USERNAME_PATTERN, hash_password
from app.db import SessionLocal
from app.models import User, UserRole, UserStatus

DEFAULT_CREDENTIALS_FILE = "/etc/xinbaoyan/initial-credentials"


@dataclass(frozen=True, slots=True)
class _AccountSpec:
    nickname: str
    role: UserRole


DEFAULTS: dict[str, _AccountSpec] = {
    "admin": _AccountSpec(nickname="管理员", role=UserRole.ADMIN),
    "test": _AccountSpec(nickname="测试用户", role=UserRole.USER),
}

DEFAULT_USERNAMES = {"admin": "admin@123321", "test": "test@123456"}
DEFAULT_EMAILS = {"admin": "admin@xinbaoyan.local", "test": "test@xinbaoyan.local"}

ENV_KEYS = {
    "admin": ("DEFAULT_ADMIN_USERNAME", "DEFAULT_ADMIN_PASSWORD", "DEFAULT_ADMIN_EMAIL"),
    "test": ("DEFAULT_TEST_USERNAME", "DEFAULT_TEST_PASSWORD", "DEFAULT_TEST_EMAIL"),
}


def generate_password(length: int = 14) -> str:
    """Random password guaranteed to satisfy the letter+digit policy."""
    if length < 8:
        raise ValueError("Generated passwords must be at least 8 characters")
    alphabet = string.ascii_letters + string.digits
    rng = secrets.SystemRandom()
    while True:
        candidate = "".join(rng.choice(alphabet) for _ in range(length))
        if any(c.isalpha() for c in candidate) and any(c.isdigit() for c in candidate):
            return candidate


def _validate_username(username: str) -> None:
    if not USERNAME_PATTERN.fullmatch(username):
        raise SystemExit(f"Default username does not match the required pattern: {username!r}")


def _validate_password(password: str) -> None:
    if len(password) < 8 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise SystemExit("Default passwords must contain letters and digits, min 8 characters")


def write_credentials_file(generated: dict[str, tuple[str, str]]) -> Path:
    """Persist generated credentials to a private file; return its path.

    ``generated`` maps account kind to (username, password). The file is
    created with 0600 permissions and never overwritten silently: an existing
    file keeps its content and the new entries are appended only if absent.
    """
    candidates = [Path(os.environ.get("INITIAL_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_FILE))]
    from app.core.config import get_settings

    candidates.append(Path(get_settings().storage_root) / "initial-credentials")
    lines = [
        "# Xinbaoyan OJ initial accounts (generated once at first deployment).",
        "# Change these passwords immediately after first login, then delete this file.",
    ]
    for kind, (username, password) in generated.items():
        lines.append(f"{kind}: username={username} password={password}")
    content = "\n".join(lines) + "\n"
    last_error: OSError | None = None
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            path.chmod(0o600)
            return path
        except OSError as exc:
            last_error = exc
    raise SystemExit(f"Cannot write initial credentials file: {last_error}")


def ensure_default_users(
    db: Session,
    *,
    admin_username: str = DEFAULT_USERNAMES["admin"],
    admin_password: str | None = None,
    admin_email: str = DEFAULT_EMAILS["admin"],
    test_username: str = DEFAULT_USERNAMES["test"],
    test_password: str | None = None,
    test_email: str = DEFAULT_EMAILS["test"],
    generated_out: dict[str, tuple[str, str]] | None = None,
) -> list[str]:
    """Create missing default accounts; return one human-readable status per account.

    A ``None`` password means "generate a random one"; generated credentials
    are recorded in ``generated_out`` (kind -> (username, password)) so the
    caller can persist them to a private file. Explicit overrides are never
    recorded or printed.
    """
    accounts = (
        ("admin", admin_username, admin_password, admin_email),
        ("test", test_username, test_password, test_email),
    )
    for _kind, username, password, _email in accounts:
        _validate_username(username)
        if password is not None:
            _validate_password(password)

    messages: list[str] = []
    for kind, username, password, email in accounts:
        if db.scalar(select(User).where(User.username == username)) is not None:
            messages.append(f"{kind}: account {username!r} already exists, skipped")
            continue
        if password is None:
            password = generate_password()
            if generated_out is not None:
                generated_out[kind] = (username, password)
        spec = DEFAULTS[kind]
        db.add(
            User(
                username=username,
                nickname=spec.nickname,
                email=email.lower(),
                password_hash=hash_password(password),
                role=spec.role,
                status=UserStatus.ACTIVE,
            )
        )
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise SystemExit(
                f"Cannot create default {kind} account {username!r}: username or email already used"
            ) from exc
        messages.append(f"{kind}: account {username!r} created")
    db.commit()
    return messages


def main() -> None:
    overrides = {}
    for kind, keys in ENV_KEYS.items():
        username = os.environ.get(keys[0])
        password = os.environ.get(keys[1])
        email = os.environ.get(keys[2])
        if username:
            overrides[f"{kind}_username"] = username
        if password:
            overrides[f"{kind}_password"] = password
        if email:
            overrides[f"{kind}_email"] = email
    generated: dict[str, tuple[str, str]] = {}
    db = SessionLocal()
    try:
        for message in ensure_default_users(db, generated_out=generated, **overrides):
            print(message)
    finally:
        db.close()
    if generated:
        path = write_credentials_file(generated)
        print(f"Generated initial passwords written to {path} (mode 0600). Read once, change after first login.")


if __name__ == "__main__":
    main()