import json
import os

import pandas as pd
import psycopg2
import psycopg2.extras
import requests
from jobspy import scrape_jobs
from pandas import DataFrame
from prefect import flow, runtime, task
from prefect_dbt import PrefectDbtRunner, PrefectDbtSettings
from sqlalchemy import create_engine, text

from helper import format_job_message_telegram, format_summary_message_telegram
from llm_queue import LLMQueueClient

LLM_QUEUE_DSN = os.getenv(
    "LLM_QUEUE_DSN",
    "postgresql://llm_queue_worker:lq-a437d02922eae4ba942d7299@hub_db:5432/llm_queue?sslmode=disable",
)
LLM_QUEUE_TOPIC = os.getenv("LLM_QUEUE_TOPIC", "job_eval")
LLM_QUEUE_WORKER_URL = os.getenv("LLM_QUEUE_WORKER_URL")


# ---------------------------------------------------------------------------
# DB / shared helpers
# ---------------------------------------------------------------------------

def get_db_engine():
    connection_string = (
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    return create_engine(connection_string)


def write_to_db(df: DataFrame, schema: str, table_name: str) -> None:
    df.to_sql(
        name=table_name,
        con=get_db_engine(),
        schema=schema,
        if_exists="append",
        index=False,
        method="multi",
    )


def load_resume(profile: str) -> tuple[str, str]:
    query = text("""
        SELECT resume_body, telegram_chat_id FROM adm.resume
        WHERE profile = :profile AND is_active = TRUE
        ORDER BY updated_at DESC LIMIT 1
    """)
    with get_db_engine().connect() as conn:
        result = conn.execute(query, {"profile": profile}).fetchone()
    if not result:
        raise ValueError(f"No active resume for profile: {profile}")
    return result[0], result[1]


def load_telegram_chat_id(profile: str) -> str:
    query = text("""
        SELECT telegram_chat_id FROM adm.resume
        WHERE profile = :profile AND is_active = TRUE
        ORDER BY updated_at DESC LIMIT 1
    """)
    with get_db_engine().connect() as conn:
        result = conn.execute(query, {"profile": profile}).fetchone()
    if not result or not result[0]:
        raise ValueError(f"No telegram_chat_id for profile: {profile}")
    return result[0]


def send_telegram_message(message_text: str, chat_id: str) -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": chat_id,
            "text": message_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        return resp.json()["ok"]
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ---------------------------------------------------------------------------
# dbt
# ---------------------------------------------------------------------------

@task()
def run_dbt(run_name: str):
    PrefectDbtRunner(
        settings=PrefectDbtSettings(
            project_dir="data__job_searcher",
            profiles_dir="data__job_searcher",
        )
    ).invoke(["build", "--vars", json.dumps({"run_name": run_name})])


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _get_queued_job_ids() -> set[str]:
    """Job IDs already in llm_queue (pending/processing/done) — don't re-queue."""
    with psycopg2.connect(LLM_QUEUE_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload->>'job_id' FROM llm_queue.tasks
                WHERE topic = %s AND status IN ('pending', 'processing', 'done')
                """,
                (LLM_QUEUE_TOPIC,),
            )
            return {row[0] for row in cur.fetchall() if row[0]}


def _get_evaluated_job_ids() -> set[str]:
    """Job IDs already written to evaluated_jobs."""
    with get_db_engine().connect() as conn:
        result = conn.execute(text("SELECT CAST(job_id AS TEXT) FROM public.evaluated_jobs"))
        return {row[0] for row in result}


def _push_jobs_to_queue(profile: str, sys_run_name: str, resume: str) -> int:
    """Push unevaluated, un-queued jobs to llm-queue. Returns count pushed."""
    exclude_ids = _get_evaluated_job_ids() | _get_queued_job_ids()

    query = text("""
        SELECT id, description, title, company
        FROM public.jobspy_jobs
        WHERE sys_profile = :profile
        ORDER BY id DESC
    """)
    with get_db_engine().connect() as conn:
        jobs_df = pd.read_sql_query(query, conn, params={"profile": profile})

    jobs_df = jobs_df[~jobs_df["id"].astype(str).isin(exclude_ids)]

    if jobs_df.empty:
        print("No new jobs to queue")
        return 0

    client = LLMQueueClient(dsn=LLM_QUEUE_DSN, worker_url=LLM_QUEUE_WORKER_URL)
    payloads = [
        {
            "job_id": str(row["id"]),
            "company": row.get("company") or "",
            "title": row.get("title") or "",
            "description": row.get("description") or "",
            "resume": resume,
            "sys_profile": profile,
            "sys_run_name": sys_run_name,
        }
        for _, row in jobs_df.iterrows()
    ]

    task_ids = client.push_batch(LLM_QUEUE_TOPIC, payloads)
    print(f"Pushed {len(task_ids)} jobs to queue (topic={LLM_QUEUE_TOPIC})")
    return len(task_ids)


def _drain_queue_results() -> int:
    """Copy done queue tasks into evaluated_jobs. Returns count drained."""
    existing_ids = _get_evaluated_job_ids()

    with psycopg2.connect(LLM_QUEUE_DSN) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT payload, result FROM llm_queue.tasks WHERE topic = %s AND status = 'done'",
                (LLM_QUEUE_TOPIC,),
            )
            tasks = cur.fetchall()

    rows = []
    for task in tasks:
        job_id = (task["payload"] or {}).get("job_id", "")
        if job_id in existing_ids:
            continue
        result = task["result"] or {}
        scores = result.get("match_scores", {})
        avg_score = sum(scores.values()) / len(scores) if scores else 0
        rows.append({
            "job_id": job_id,
            "avg_score": avg_score,
            "match_scores": json.dumps(scores),
            "reasoning": json.dumps({
                "verdict": result.get("verdict"),
                "tech_stack": result.get("tech_stack_analysis"),
                "summary": result.get("one_line_summary"),
                "comparisons": [],
            }),
            "sys_run_name": task["payload"].get("sys_run_name", ""),
            "sys_profile": task["payload"].get("sys_profile", ""),
        })

    if rows:
        write_to_db(pd.DataFrame(rows), "public", "evaluated_jobs")
        print(f"Drained {len(rows)} results into evaluated_jobs")
    else:
        print("No new queue results to drain")

    return len(rows)


# ---------------------------------------------------------------------------
# MIDNIGHT FLOW — scrape + push to queue
# ---------------------------------------------------------------------------

@flow()
def load_jobs_flow(
    title: str = "Data Engineer",
    location: str = "Toronto, ON",
    profile: str = "default",
    searches: int = 100,
):
    """Scrape jobs from 5 boards, dedup via dbt, push unevaluated to llm-queue."""
    sys_run_name = runtime.flow_run.name

    jobs = scrape_jobs(
        site_name=["indeed", "linkedin", "glassdoor", "google"],
        search_term=title,
        google_search_term=f"{title} jobs near {location} since yesterday",
        location=location,
        results_wanted=searches,
        hours_old=48,
        country_indeed="canada",
        linkedin_fetch_description=True,
    )
    jobs["sys_run_name"] = sys_run_name
    jobs["sys_profile"] = profile
    write_to_db(jobs, "jobspy", "import_jobs")
    print(f"Scraped {len(jobs)} jobs from 5 boards")

    run_dbt(sys_run_name)

    resume, _ = load_resume(profile)
    _push_jobs_to_queue(profile, sys_run_name, resume)


# ---------------------------------------------------------------------------
# MORNING FLOW — drain queue results + notify top 10
# ---------------------------------------------------------------------------

@task()
def get_top_jobs(profile: str, min_score: float = 6.9, limit: int = 10):
    """Top jobs from the most recent evaluated batch for this profile."""
    query = text("""
        SELECT
            j.title, j.company, j.location,
            e.avg_score, e.match_scores, e.reasoning,
            COALESCE(j.job_url_direct, j.job_url) AS job_url
        FROM public.evaluated_jobs e
        INNER JOIN public.jobspy_jobs j ON e.job_id = j.id
        WHERE e.sys_profile = :profile
          AND e.avg_score >= :min_score
          AND e.sys_run_name = (
              SELECT sys_run_name FROM public.evaluated_jobs
              WHERE sys_profile = :profile
              ORDER BY job_id DESC LIMIT 1
          )
        ORDER BY e.avg_score DESC
        LIMIT :limit
    """)
    with get_db_engine().connect() as conn:
        result = conn.execute(query, {"profile": profile, "min_score": min_score, "limit": limit})
        jobs = result.fetchall()
    print(f"Found {len(jobs)} jobs with score >= {min_score}")
    return jobs


@task()
def send_telegram_notifications(jobs, run_name: str, chat_id: str):
    if not jobs:
        print("No jobs to send")
        return
    send_telegram_message(format_summary_message_telegram(jobs, run_name), chat_id)
    for i, job in enumerate(jobs, 1):
        send_telegram_message(format_job_message_telegram(job, i, len(jobs)), chat_id)
    print(f"Sent {len(jobs) + 1} Telegram messages")


@flow()
def notify_matches_flow(profile: str = "default", min_score: float = 6.9):
    """Drain done queue results into evaluated_jobs, then notify top 10."""
    _drain_queue_results()

    chat_id = load_telegram_chat_id(profile)
    jobs = get_top_jobs(profile, min_score)

    if jobs:
        # Run name from the most recent evaluated batch (set during drain)
        run_name = jobs[0][4] if jobs else runtime.flow_run.name  # fallback
        with get_db_engine().connect() as conn:
            row = conn.execute(text(
                "SELECT sys_run_name FROM public.evaluated_jobs "
                "WHERE sys_profile = :p ORDER BY job_id DESC LIMIT 1"
            ), {"p": profile}).fetchone()
        run_name = row[0] if row else runtime.flow_run.name
        send_telegram_notifications(jobs, run_name, chat_id)
    else:
        print(f"No jobs found with score >= {min_score}")


if __name__ == "__main__":
    load_jobs_flow()
