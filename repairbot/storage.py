"""SQLite persistence for the prototype workflow."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import (
    AgentEvent,
    ErrorCluster,
    FixAttempt,
    JiraTicketRecord,
    LogSummary,
    ParsedLogEntry,
    PullRequestRecord,
)


DEFAULT_DB_PATH = Path(".repairbot") / "repairbot.db"


class RepairBotStorage:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repair_runs (
              run_id TEXT PRIMARY KEY,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              mode TEXT
            );
            CREATE TABLE IF NOT EXISTS parsed_log_entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              service_name TEXT NOT NULL,
              level TEXT NOT NULL,
              trace_id TEXT,
              request_id TEXT,
              endpoint TEXT,
              status_code INTEGER,
              error_type TEXT,
              class_name TEXT,
              method_name TEXT,
              line_number INTEGER,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS log_summaries (
              run_id TEXT NOT NULL,
              service_name TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              PRIMARY KEY (run_id, service_name)
            );
            CREATE TABLE IF NOT EXISTS error_clusters (
              cluster_id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              service_name TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hypotheses (
              cluster_id TEXT NOT NULL,
              rank INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              PRIMARY KEY (cluster_id, rank)
            );
            CREATE TABLE IF NOT EXISTS jira_tickets (
              jira_key TEXT PRIMARY KEY,
              cluster_id TEXT UNIQUE NOT NULL,
              service_name TEXT NOT NULL,
              jira_url TEXT,
              summary TEXT NOT NULL,
              status TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fix_attempts (
              jira_key TEXT NOT NULL,
              attempt INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              PRIMARY KEY (jira_key, attempt)
            );
            CREATE TABLE IF NOT EXISTS pull_requests (
              jira_key TEXT PRIMARY KEY,
              service_name TEXT NOT NULL,
              branch_name TEXT NOT NULL,
              pr_url TEXT NOT NULL,
              commit_sha TEXT,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              agent_name TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()

    def create_run(self, run_id: str, mode: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO repair_runs(run_id, mode) VALUES (?, ?)",
            (run_id, mode),
        )
        self.conn.commit()

    def store_log_entries(self, entries: Iterable[ParsedLogEntry]) -> None:
        rows = [
            (
                entry.run_id,
                entry.service_name,
                entry.level,
                entry.trace_id,
                entry.request_id,
                entry.endpoint,
                entry.status_code,
                entry.error_type,
                entry.class_name,
                entry.method_name,
                entry.line_number,
                entry.model_dump_json(),
            )
            for entry in entries
        ]
        self.conn.executemany(
            """
            INSERT INTO parsed_log_entries(
              run_id, service_name, level, trace_id, request_id, endpoint,
              status_code, error_type, class_name, method_name, line_number,
              payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()

    def store_summary(self, summary: LogSummary) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO log_summaries(run_id, service_name, payload_json)
            VALUES (?, ?, ?)
            """,
            (summary.run_id, summary.service_name, summary.model_dump_json()),
        )
        self.conn.commit()

    def store_cluster(self, run_id: str, cluster: ErrorCluster) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO error_clusters(cluster_id, run_id, service_name, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (cluster.cluster_id, run_id, cluster.service_name, cluster.model_dump_json()),
        )
        for hypothesis in cluster.hypotheses:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO hypotheses(cluster_id, rank, payload_json)
                VALUES (?, ?, ?)
                """,
                (cluster.cluster_id, hypothesis.rank, hypothesis.model_dump_json()),
            )
        self.conn.commit()

    def get_ticket_by_cluster(self, cluster_id: str) -> JiraTicketRecord | None:
        row = self.conn.execute(
            "SELECT payload_json FROM jira_tickets WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()
        return JiraTicketRecord.model_validate_json(row["payload_json"]) if row else None

    def get_ticket(self, jira_key: str) -> JiraTicketRecord | None:
        row = self.conn.execute(
            "SELECT payload_json FROM jira_tickets WHERE jira_key = ?",
            (jira_key,),
        ).fetchone()
        return JiraTicketRecord.model_validate_json(row["payload_json"]) if row else None

    def get_cluster(self, cluster_id: str) -> ErrorCluster | None:
        row = self.conn.execute(
            "SELECT payload_json FROM error_clusters WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()
        return ErrorCluster.model_validate_json(row["payload_json"]) if row else None

    def store_jira_ticket(self, ticket: JiraTicketRecord) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO jira_tickets(
              jira_key, cluster_id, service_name, jira_url, summary, status, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket.jira_key,
                ticket.cluster_id,
                ticket.service_name,
                ticket.jira_url,
                ticket.summary,
                ticket.status,
                ticket.model_dump_json(),
            ),
        )
        self.conn.commit()

    def store_fix_attempt(self, attempt: FixAttempt) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO fix_attempts(jira_key, attempt, payload_json)
            VALUES (?, ?, ?)
            """,
            (attempt.jira_key, attempt.attempt, attempt.model_dump_json()),
        )
        self.conn.commit()

    def store_pull_request(self, record: PullRequestRecord) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO pull_requests(
              jira_key, service_name, branch_name, pr_url, commit_sha, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.jira_key,
                record.service_name,
                record.branch_name,
                record.pr_url,
                record.commit_sha,
                record.model_dump_json(),
            ),
        )
        self.conn.commit()

    def store_agent_event(self, event: AgentEvent) -> None:
        self.conn.execute(
            """
            INSERT INTO agent_events(run_id, agent_name, event_type, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (event.run_id, event.agent_name, event.event_type, json.dumps(event.payload)),
        )
        self.conn.commit()

    def list_tickets(self) -> list[JiraTicketRecord]:
        rows = self.conn.execute("SELECT payload_json FROM jira_tickets").fetchall()
        return [JiraTicketRecord.model_validate_json(row["payload_json"]) for row in rows]
