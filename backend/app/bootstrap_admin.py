"""Optional initial administrator provisioning for hosts without an interactive shell.

Credentials come only from the runtime environment and are never printed.
Remove the bootstrap environment values after recording the initial password.
"""
import os

from sqlalchemy import select, text

from app.cli import create_admin
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.models import User


def bootstrap_admin(settings: Settings, email: str, name: str, password: str) -> bool:
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as db:
            # Serialize first-user creation across simultaneous starts.
            db.execute(text("SELECT pg_advisory_xact_lock(482017193)"))
            if db.scalar(select(User.id).limit(1)) is not None:
                return False
            create_admin(db, email=email, name=name, password=password)
            return True
    finally:
        engine.dispose()


def main() -> int:
    email = os.environ.get("ICP_BOOTSTRAP_ADMIN_EMAIL", "")
    password = os.environ.get("ICP_BOOTSTRAP_ADMIN_PASSWORD", "")
    name = os.environ.get("ICP_BOOTSTRAP_ADMIN_NAME", "Test administrator")
    if not email and not password:
        return 0
    if not email or not password:
        raise RuntimeError("Both administrator bootstrap settings must be configured")
    bootstrap_admin(get_settings(), email, name, password)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

