import logging
from db.client import get_db

logger = logging.getLogger("devloop.db.repos")

DEMO_REPO = "rishikesh183/devloop-demo-app"


def add_repo(user_id: str, repo: str, base_branch: str = "main", sentry_secret: str = None) -> dict:
    db = get_db()
    result = db.table("user_repos").upsert({
        "user_id": user_id,
        "repo": repo,
        "base_branch": base_branch,
        "sentry_secret": sentry_secret,
    }, on_conflict="user_id,repo").execute()
    logger.info("Added repo %s for user %s", repo, user_id)
    return result.data[0]


def list_repos(user_id: str) -> list[dict]:
    db = get_db()
    result = db.table("user_repos") \
        .select("*") \
        .eq("user_id", user_id) \
        .order("created_at") \
        .execute()
    return result.data


def remove_repo(user_id: str, repo: str) -> None:
    db = get_db()
    db.table("user_repos").delete().eq("user_id", user_id).eq("repo", repo).execute()
    logger.info("Removed repo %s for user %s", repo, user_id)


def get_repo(user_id: str, repo: str) -> dict | None:
    db = get_db()
    result = db.table("user_repos") \
        .select("*") \
        .eq("user_id", user_id) \
        .eq("repo", repo) \
        .execute()
    return result.data[0] if result.data else None


def ensure_demo_repo(user_id: str) -> dict:
    """Add demo repo for user if not already present."""
    existing = get_repo(user_id, DEMO_REPO)
    if existing:
        return existing
    return add_repo(user_id, DEMO_REPO, base_branch="main")
