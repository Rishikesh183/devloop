import logging
from db.client import get_db

logger = logging.getLogger("devloop.db.users")


def upsert_user(github_id: str, github_login: str, github_token: str) -> dict:
    db = get_db()
    result = db.table("users").upsert({
        "github_id": github_id,
        "github_login": github_login,
        "github_token": github_token,
    }, on_conflict="github_id").execute()
    user = result.data[0]
    logger.info("Upserted user %s (github: %s)", user["id"], github_login)
    return user


def get_user(user_id: str) -> dict | None:
    db = get_db()
    result = db.table("users").select("*").eq("id", user_id).execute()
    return result.data[0] if result.data else None


def get_user_by_github_id(github_id: str) -> dict | None:
    db = get_db()
    result = db.table("users").select("*").eq("github_id", github_id).execute()
    return result.data[0] if result.data else None


def update_user(user_id: str, **kwargs) -> None:
    db = get_db()
    db.table("users").update(kwargs).eq("id", user_id).execute()
    logger.info("Updated user %s: %s", user_id, list(kwargs.keys()))
