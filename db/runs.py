import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from db.client import get_db

logger = logging.getLogger("devloop.db.runs")


def create_run(user_id: str, error_message: str, filename: str, environment: str) -> str:
    db = get_db()
    result = db.table("runs").insert({
        "user_id": user_id,
        "error_message": error_message,
        "filename": filename,
        "environment": environment,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    run_id = result.data[0]["id"]
    logger.info("Created run %s for user %s", run_id, user_id)
    return run_id


def update_run(run_id: str, **kwargs) -> None:
    db = get_db()
    if "completed_at" not in kwargs:
        kwargs["completed_at"] = datetime.now(timezone.utc).isoformat()
    db.table("runs").update(kwargs).eq("id", run_id).execute()
    logger.info("Updated run %s: %s", run_id, kwargs)


def get_runs_for_user(user_id: str) -> list[dict]:
    db = get_db()
    result = db.table("runs") \
        .select("*") \
        .eq("user_id", user_id) \
        .order("started_at", desc=True) \
        .execute()
    return result.data


def append_log(run_id: str, user_id: str, level: str, message: str) -> None:
    db = get_db()
    try:
        db.table("log_lines").insert({
            "run_id": run_id,
            "user_id": user_id,
            "level": level,
            "message": message,
        }).execute()
    except Exception as e:
        logger.warning("Failed to write log line to Supabase: %s", e)


def get_logs_for_run(run_id: str) -> list[dict]:
    db = get_db()
    result = db.table("log_lines") \
        .select("level, message, created_at") \
        .eq("run_id", run_id) \
        .order("id") \
        .execute()
    return result.data


def get_recent_logs_for_user(user_id: str, limit: int = 200) -> list[dict]:
    db = get_db()
    result = db.table("log_lines") \
        .select("level, message, created_at, run_id") \
        .eq("user_id", user_id) \
        .order("id", desc=True) \
        .limit(limit) \
        .execute()
    return list(reversed(result.data))
