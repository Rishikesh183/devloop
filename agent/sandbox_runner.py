import logging
import os
import tempfile
import time
from pathlib import Path

import docker
from docker.errors import DockerException

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("devloop.sandbox_runner")

DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "python:3.11-slim")
TEST_COMMAND = os.getenv("TEST_COMMAND", "pytest")
TIMEOUT_SECONDS = 120


def run_tests(patched_filepath: str, patched_content: str) -> dict:
    """
    Write patched file into a temp dir, spin up Docker container,
    run tests, return results dict.
    """
    logger.info("Starting sandbox test run for: %s", patched_filepath)

    start_time = time.monotonic()
    container = None

    try:
        client = docker.from_env()
    except DockerException as e:
        logger.error("Failed to connect to Docker daemon: %s", e)
        return {
            "passed": False,
            "output": f"Docker connection error: {e}",
            "duration": 0.0,
        }

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write the patched file preserving relative path structure
        relative_path = patched_filepath.lstrip("/").lstrip("\\")
        dest_path = Path(tmpdir) / relative_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            dest_path.write_text(patched_content, encoding="utf-8")
            logger.info("Wrote patched file to sandbox: %s", dest_path)
        except OSError as e:
            logger.error("Failed to write patched file to temp dir: %s", e)
            return {
                "passed": False,
                "output": f"File write error: {e}",
                "duration": 0.0,
            }

        # Install pytest in container then run tests
        cmd = f"pip install pytest --quiet && {TEST_COMMAND} /workspace --tb=short -q"

        try:
            logger.info("Pulling image %s if needed...", DOCKER_IMAGE)
            try:
                client.images.get(DOCKER_IMAGE)
            except docker.errors.ImageNotFound:
                client.images.pull(DOCKER_IMAGE)
                logger.info("Pulled image %s", DOCKER_IMAGE)

            logger.info("Running container with command: %s", cmd)
            container = client.containers.run(
                image=DOCKER_IMAGE,
                command=f"bash -c '{cmd}'",
                volumes={tmpdir: {"bind": "/workspace", "mode": "ro"}},
                working_dir="/workspace",
                detach=True,
                remove=False,
                network_disabled=False,
                mem_limit="512m",
                cpu_period=100000,
                cpu_quota=50000,
            )

            try:
                exit_result = container.wait(timeout=TIMEOUT_SECONDS)
                exit_code = exit_result.get("StatusCode", -1)
            except Exception as e:
                logger.warning("Container timed out or wait failed: %s", e)
                try:
                    container.kill()
                except Exception:
                    pass
                duration = time.monotonic() - start_time
                return {
                    "passed": False,
                    "output": f"Test run timed out after {TIMEOUT_SECONDS}s",
                    "duration": duration,
                }

            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            duration = time.monotonic() - start_time
            passed = exit_code == 0

            logger.info(
                "Tests %s in %.2fs (exit code %d)",
                "PASSED" if passed else "FAILED",
                duration,
                exit_code,
            )

            return {
                "passed": passed,
                "output": logs,
                "duration": duration,
            }

        except DockerException as e:
            duration = time.monotonic() - start_time
            logger.error("Docker error during test run: %s", e)
            return {
                "passed": False,
                "output": f"Docker error: {e}",
                "duration": duration,
            }
        except Exception as e:
            duration = time.monotonic() - start_time
            logger.error("Unexpected error during sandbox run: %s", e)
            return {
                "passed": False,
                "output": f"Unexpected error: {e}",
                "duration": duration,
            }
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                    logger.info("Container cleaned up")
                except Exception as cleanup_err:
                    logger.warning("Failed to clean up container: %s", cleanup_err)
