"""任務與曲目狀態的 SQLite 持久層。

只有兩張表，用 stdlib sqlite3 就夠 —— 不引入 ORM。
候選與已確認的 metadata 以 JSON 存欄位，因為它們是整包讀寫、從不被查詢。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.models import Candidate, SourceTrack, TrackMeta

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    created_at TEXT NOT NULL,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    video_id   TEXT NOT NULL,
    source     TEXT NOT NULL,
    status     TEXT NOT NULL,
    candidates TEXT NOT NULL DEFAULT '[]',
    chosen     TEXT,
    error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_job ON tracks(job_id);
CREATE INDEX IF NOT EXISTS idx_tracks_video ON tracks(video_id);
"""


class TrackStatus(StrEnum):
    PENDING = "pending"
    MATCHING = "matching"
    AWAITING_CONFIRM = "awaiting_confirm"
    DOWNLOADING = "downloading"
    TAGGING = "tagging"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class TrackRow:
    id: int
    job_id: int
    video_id: str
    source: SourceTrack
    status: TrackStatus
    candidates: list[Candidate]
    chosen: TrackMeta | None
    error: str | None


@dataclass(frozen=True, slots=True)
class JobRow:
    id: int
    url: str
    created_at: str
    tracks: list[TrackRow]
    error: str | None


def _meta_from_dict(data: dict) -> TrackMeta:
    data = dict(data)
    data["artists"] = tuple(data.get("artists", ()))
    return TrackMeta(**data)


def _meta_to_dict(meta: TrackMeta) -> dict:
    payload = asdict(meta)
    payload["artists"] = list(meta.artists)
    return payload


def _dump_meta(meta: TrackMeta) -> str:
    return json.dumps(_meta_to_dict(meta), ensure_ascii=False)


class JobStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 背景 thread pool 會共用同一個連線，故關閉 thread 檢查。
        # sqlite3 底層以 SQLITE_THREADSAFE=1（serialized）編譯，單一陳述式呼叫不會
        # 造成資料庫損毀，但 sqlite3_last_insert_rowid() 是「連線層級」而非「陳述式
        # 層級」的值 —— 若兩個執行緒交錯呼叫 INSERT，cursor.lastrowid 可能讀到另一
        # 執行緒剛寫入的 rowid。因此所有寫入方法一律用 `INSERT ... RETURNING id`
        # 直接從陳述式結果取得 id，並以下方的 _write_lock 序列化寫入路徑，避免任何
        # 跨執行緒交錯的驚喜（例如兩個 UPDATE 的先後順序被打亂）。
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._write_lock = threading.Lock()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- 寫入 ---

    def create_job(self, url: str) -> int:
        with self._write_lock:
            cursor = self._conn.execute(
                "INSERT INTO jobs (url, created_at) VALUES (?, ?) RETURNING id",
                (url, datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.fetchone()["id"])

    def add_track(self, job_id: int, source: SourceTrack) -> int:
        with self._write_lock:
            cursor = self._conn.execute(
                "INSERT INTO tracks (job_id, video_id, source, status) "
                "VALUES (?, ?, ?, ?) RETURNING id",
                (
                    job_id,
                    source.video_id,
                    json.dumps(asdict(source), ensure_ascii=False),
                    TrackStatus.PENDING.value,
                ),
            )
            return int(cursor.fetchone()["id"])

    def set_candidates(self, track_id: int, candidates: list[Candidate]) -> None:
        payload = json.dumps(
            [{"meta": _meta_to_dict(c.meta), "score": c.score} for c in candidates],
            ensure_ascii=False,
        )
        with self._write_lock:
            self._conn.execute(
                "UPDATE tracks SET candidates = ?, status = ?, error = NULL WHERE id = ?",
                (payload, TrackStatus.AWAITING_CONFIRM.value, track_id),
            )

    def confirm(self, track_id: int, meta: TrackMeta) -> None:
        with self._write_lock:
            self._conn.execute(
                "UPDATE tracks SET chosen = ?, status = ?, error = NULL WHERE id = ?",
                (_dump_meta(meta), TrackStatus.DOWNLOADING.value, track_id),
            )

    def set_status(self, track_id: int, status: TrackStatus, error: str | None = None) -> None:
        with self._write_lock:
            self._conn.execute(
                "UPDATE tracks SET status = ?, error = ? WHERE id = ?",
                (status.value, error, track_id),
            )

    def set_job_error(self, job_id: int, message: str) -> None:
        """記錄 job 層級失敗（探測整批拿不到任何曲目時）。

        取代舊有的佔位 track 做法：這是 job 本身的失敗，不對應任何真實曲目，
        `job.tracks` 因此維持空陣列，不會混進一筆 video_id="" 的假記錄。
        """
        with self._write_lock:
            self._conn.execute(
                "UPDATE jobs SET error = ? WHERE id = ?",
                (message, job_id),
            )

    # --- 讀取 ---

    def _row_to_track(self, row: sqlite3.Row) -> TrackRow:
        candidates = [
            Candidate(meta=_meta_from_dict(item["meta"]), score=float(item["score"]))
            for item in json.loads(row["candidates"])
        ]
        chosen = _meta_from_dict(json.loads(row["chosen"])) if row["chosen"] else None
        return TrackRow(
            id=row["id"],
            job_id=row["job_id"],
            video_id=row["video_id"],
            source=SourceTrack(**json.loads(row["source"])),
            status=TrackStatus(row["status"]),
            candidates=candidates,
            chosen=chosen,
            error=row["error"],
        )

    def get_track(self, track_id: int) -> TrackRow | None:
        row = self._conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return self._row_to_track(row) if row else None

    def get_job(self, job_id: int) -> JobRow | None:
        job = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return None
        rows = self._conn.execute(
            "SELECT * FROM tracks WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return JobRow(
            id=job["id"],
            url=job["url"],
            created_at=job["created_at"],
            tracks=[self._row_to_track(r) for r in rows],
            error=job["error"],
        )

    def list_jobs(self) -> list[JobRow]:
        ids = [r["id"] for r in self._conn.execute("SELECT id FROM jobs ORDER BY id DESC")]
        return [job for job in (self.get_job(i) for i in ids) if job is not None]

    def find_by_video_id(self, video_id: str) -> list[TrackRow]:
        rows = self._conn.execute(
            "SELECT * FROM tracks WHERE video_id = ? ORDER BY id", (video_id,)
        ).fetchall()
        return [self._row_to_track(r) for r in rows]
