from sqlalchemy import func, select

from app.bootstrap_admin import bootstrap_admin, main
from app.models import User
from app.security import verify_password


def test_bootstrap_creates_admin_and_never_resets_existing_account(settings, db):
    password = "initial-test-password-123"
    assert bootstrap_admin(settings, "owner@example.com", "Test owner", password)
    user = db.scalar(select(User).where(User.email == "owner@example.com"))
    assert user is not None and user.role == "admin"
    assert user.password_hash != password
    assert verify_password(user.password_hash, password)
    assert not bootstrap_admin(settings, "owner@example.com", "Changed name", "different-password-123")
    db.expire_all()
    assert verify_password(user.password_hash, password)
    assert user.full_name == "Test owner"
    assert db.scalar(select(func.count()).select_from(User)) == 1


def test_bootstrap_does_not_add_admin_to_a_database_with_existing_users(settings, make_user, db):
    make_user(email="existing@example.com")
    assert not bootstrap_admin(settings, "owner@example.com", "Test owner", "initial-test-password-123")
    assert db.scalar(select(func.count()).select_from(User)) == 1
    assert db.scalar(select(User).where(User.email == "owner@example.com")) is None


def test_unconfigured_bootstrap_does_nothing(monkeypatch):
    monkeypatch.delenv("ICP_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ICP_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    assert main() == 0

