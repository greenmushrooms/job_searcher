"""
LLM Queue Python client.

Connects directly to PostgreSQL — no intermediate service required for push/get.
Control API (pause/resume/status) calls the worker HTTP API if worker_url is provided,
otherwise falls back to direct DB control table manipulation.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg2
import psycopg2.extras


class LLMQueueClient:
    """Client for the llm-queue task queue.

    Args:
        dsn: PostgreSQL DSN (e.g. "postgresql://user:pass@host:5432/db")
        worker_url: Optional HTTP base URL of the worker API (e.g. "http://localhost:8080")
                    Required for pause/resume/status via HTTP.
                    Falls back to direct DB control table if not provided.
    """

    def __init__(self, dsn: str, worker_url: str | None = None):
        self._dsn = dsn
        self._worker_url = worker_url.rstrip("/") if worker_url else None
        self._conn: psycopg2.extensions.connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    def __enter__(self) -> "LLMQueueClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Task push
    # ------------------------------------------------------------------

    def push_task(self, topic: str, payload: dict, priority: int = 0) -> int:
        """Push a single task onto the queue. Returns task_id."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO llm_queue.tasks (topic, payload, priority)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (topic, psycopg2.extras.Json(payload), priority),
            )
            row = cur.fetchone()
        return row[0]

    def push_batch(self, topic: str, payloads: list[dict], priority: int = 0) -> list[int]:
        """Push multiple tasks. Returns list of task_ids in the same order."""
        conn = self._get_conn()
        ids = []
        with conn.cursor() as cur:
            for payload in payloads:
                cur.execute(
                    """
                    INSERT INTO llm_queue.tasks (topic, payload, priority)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (topic, psycopg2.extras.Json(payload), priority),
                )
                row = cur.fetchone()
                ids.append(row[0])
        return ids

    # ------------------------------------------------------------------
    # Result polling
    # ------------------------------------------------------------------

    def get_result(self, task_id: int) -> dict | None:
        """Non-blocking. Returns result dict if done, None otherwise.

        Raises RuntimeError if the task failed permanently.
        """
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, result, error FROM llm_queue.tasks WHERE id = %s",
                (task_id,),
            )
            row = cur.fetchone()

        if row is None:
            raise KeyError(f"Task {task_id} not found")

        status, result, error = row
        if status == "done":
            return result
        if status == "failed":
            raise RuntimeError(f"Task {task_id} permanently failed: {error}")
        if status == "cancelled":
            raise RuntimeError(f"Task {task_id} was cancelled")
        return None  # pending or processing

    def wait_for_result(self, task_id: int, timeout: int = 300, poll_interval: float = 2.0) -> dict:
        """Blocking poll until the task is done or timeout expires.

        Args:
            task_id: Task ID returned by push_task.
            timeout: Max seconds to wait (default 300).
            poll_interval: Seconds between polls (default 2).

        Returns:
            Result dict from the LLM.

        Raises:
            TimeoutError: If the task does not complete within timeout seconds.
            RuntimeError: If the task failed or was cancelled.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.get_result(task_id)
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            time.sleep(min(poll_interval, max(0, remaining)))
        raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

    def wait_for_batch(
        self,
        task_ids: list[int],
        timeout: int = 300,
        poll_interval: float = 2.0,
    ) -> list[dict]:
        """Wait for all tasks in a batch. Returns results in the same order as task_ids.

        Individual task failures raise RuntimeError immediately.
        """
        deadline = time.monotonic() + timeout
        pending = set(task_ids)
        results: dict[int, dict] = {}

        while pending and time.monotonic() < deadline:
            for tid in list(pending):
                result = self.get_result(tid)
                if result is not None:
                    results[tid] = result
                    pending.discard(tid)
            if pending:
                remaining = deadline - time.monotonic()
                time.sleep(min(poll_interval, max(0, remaining)))

        if pending:
            raise TimeoutError(f"Tasks {pending} did not complete within {timeout}s")

        return [results[tid] for tid in task_ids]

    # ------------------------------------------------------------------
    # Control API (pause/resume/status/cancel)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Get worker status. Uses HTTP API if worker_url is set, otherwise queries DB."""
        if self._worker_url:
            return self._http_get("/status")
        return self._db_status()

    def pause(self, topic: str, note: str = "") -> None:
        """Pause a specific topic (or '*' for all)."""
        if self._worker_url:
            endpoint = "/pause" if topic == "*" else f"/topics/{topic}/pause"
            self._http_post(endpoint)
        else:
            self._db_pause(topic, note)

    def resume(self, topic: str) -> None:
        """Resume a specific topic (or '*' for all)."""
        if self._worker_url:
            endpoint = "/resume" if topic == "*" else f"/topics/{topic}/resume"
            self._http_post(endpoint)
        else:
            self._db_resume(topic)

    def cancel_pending(self, topic: str) -> int:
        """Cancel all pending tasks for a topic. Returns count cancelled."""
        if self._worker_url:
            resp = self._http_delete(f"/topics/{topic}/pending")
            return resp.get("cancelled", 0)
        return self._db_cancel_pending(topic)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _http_get(self, path: str) -> dict:
        import urllib.request
        import json

        url = self._worker_url + path
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())

    def _http_post(self, path: str) -> dict:
        import urllib.request
        import json

        url = self._worker_url + path
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _http_delete(self, path: str) -> dict:
        import urllib.request
        import json

        url = self._worker_url + path
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    # ------------------------------------------------------------------
    # Direct DB control (no HTTP)
    # ------------------------------------------------------------------

    def _db_status(self) -> dict:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT topic, status, COUNT(*)
                FROM llm_queue.tasks
                WHERE status IN ('pending','processing','failed')
                GROUP BY topic, status
                """
            )
            rows = cur.fetchall()

        depths: dict[str, dict[str, int]] = {}
        for topic, status, count in rows:
            depths.setdefault(topic, {})[status] = count

        with conn.cursor() as cur:
            cur.execute("SELECT topic, paused FROM llm_queue.control")
            paused = {row[0]: row[1] for row in cur.fetchall()}

        return {"topics": depths, "paused": paused}

    def _db_pause(self, topic: str, note: str = "") -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO llm_queue.control (topic, paused, paused_at, paused_by, note)
                VALUES (%s, TRUE, NOW(), 'python-client', %s)
                ON CONFLICT (topic) DO UPDATE
                  SET paused=TRUE, paused_at=NOW(), paused_by='python-client', note=%s
                """,
                (topic, note, note),
            )

    def _db_resume(self, topic: str) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM llm_queue.control WHERE topic=%s", (topic,))

    def _db_cancel_pending(self, topic: str) -> int:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE llm_queue.tasks SET status='cancelled'
                WHERE topic=%s AND status='pending'
                """,
                (topic,),
            )
            return cur.rowcount
