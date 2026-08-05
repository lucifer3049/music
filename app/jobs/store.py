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

    def recover_interrupted_tracks(self, message: str) -> int:
        """啟動時把上次行程留下的「進行中」曲目收斂成 FAILED。

        `MATCHING` / `DOWNLOADING` / `TAGGING` 都代表「上次進程還在跑到一半」——
        這三個狀態只會由背景執行緒在單次 submit()/finalize() 呼叫中設置，一旦
        process 中途死掉，資料庫裡就會永遠停在這裡：不是終態（見
        `app/api/routes.py` 的 `TERMINAL_STATUSES`），SSE 迴圈永遠不會結束，
        confirm／skip 又因為狀態不是 AWAITING_CONFIRM 而回 409，使用者無法
        重新確認也無法重試，只能看著前端永遠顯示「下載中」。

        啟動時把這些行收斂成 FAILED，訊息說明原因；使用者接著能重新送出同一個
        網址——dedup 只把 DONE 視為重複（見 Pipeline._process_source()），
        FAILED 不算，所以這樣就能重新走一次完整流程。

        `PENDING` 不在掃描範圍內：那是 `add_track()` 剛插入、還沒被任何背景
        執行緒摸過的初始狀態，`_process_source()` 全程同步執行到
        `_match_one()`（沒有跨執行緒交棒），因此 `PENDING` 只會在單一次
        `submit()` 呼叫「內部」短暫存在。process 死掉時，要嘛整個
        `submit()` 呼叫沒開始跑（該 track 列根本不存在），要嘛已經跑過
        `add_track()` 所在的那個當下執行緒還活著、會繼續往下跑到
        `MATCHING`——不會有一筆卡在 `PENDING` 卻沒人打算再處理它的紀錄。
        把它排除在掃描外，是為了不誤傷這個瞬間、無害的中繼狀態。
        """
        stuck = (
            TrackStatus.MATCHING.value,
            TrackStatus.DOWNLOADING.value,
            TrackStatus.TAGGING.value,
        )
        placeholders = ", ".join("?" for _ in stuck)
        with self._write_lock:
            cursor = self._conn.execute(
                f"UPDATE tracks SET status = ?, error = ? WHERE status IN ({placeholders})",
                (TrackStatus.FAILED.value, message, *stuck),
            )
            return cursor.rowcount

    def recover_interrupted_jobs(self, message: str) -> int:
        """啟動時把上次行程留下的「探測到一半」job 收斂成帶錯誤訊息。

        `recover_interrupted_tracks()` 只掃 tracks，掃不到這種 job：探測是在
        第一筆 `add_track()` 之前，process 若在那段期間死掉，資料庫裡留下的是
        一筆「沒有任何曲目、也沒有錯誤」的 job，底下一筆 track 都沒有。

        這種 job 是死的，但畫面上看起來跟「正在探測」一模一樣：SSE 的結束條件
        （見 `app/api/routes.py` 的 `job_events()`）刻意不把空 tracks 當結束，
        因為探測還在跑時也是空的。結果就是一張永遠不會有下文的卡片，使用者只會
        以為它還在找。

        啟動時做這件事是安全的：新行程剛起來，不可能有任何探測正在進行中，
        所以「沒有曲目又沒有錯誤」在這個時間點只可能是上次留下的殘骸。
        """
        with self._write_lock:
            cursor = self._conn.execute(
                """
                UPDATE jobs SET error = ?
                WHERE error IS NULL
                  AND id NOT IN (SELECT DISTINCT job_id FROM tracks)
                """,
                (message,),
            )
            return cursor.rowcount

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

    def delete_job(self, job_id: int) -> bool:
        """刪除一筆 job。回傳是否真的刪到一列（job_id 不存在時回傳 False）。

        底下的 track 列不用另外手動刪：schema 宣告 `tracks.job_id` 帶
        `ON DELETE CASCADE`（見 `_SCHEMA`），而這個連線在 `__init__` 已經
        執行 `PRAGMA foreign_keys = ON`——sqlite3 這個 pragma 是「連線層級」
        設定、預設關閉，且必須由呼叫端自己開，不會因為 schema 寫了
        `ON DELETE CASCADE` 就自動生效。這裡不是假設它有效，而是已經用
        `tests/test_store.py::test_delete_job_cascades_to_tracks` 實際驗證
        過：刪 job 之後直接查 tracks 表，底下的 track 列確實一併消失，不是
        只有透過 join 撈不到而已。
        """
        with self._write_lock:
            cursor = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return cursor.rowcount > 0

    _TERMINAL_TRACK_STATUSES = (TrackStatus.DONE, TrackStatus.FAILED, TrackStatus.SKIPPED)

    def delete_finished_jobs(self) -> int:
        """刪掉所有「已經沒有事情要做」的 job，回傳刪除筆數。

        符合條件的 job：
          - 底下所有曲目都到達終態（done/failed/skipped），或
          - job 本身帶著 job 層級錯誤（探測整批失敗，`job.tracks` 必然是
            空陣列，見 `set_job_error`）。

        沒有曲目、也沒有 job 層級錯誤的 job，代表 `submit()` 還在跑、稍後
        會補上曲目——絕不能刪。這裡刻意寫成 `job.tracks and all(...)` 而不是
        單純 `all(...)`：對空陣列呼叫 `all()` 會回傳 True（vacuous truth），
        若不加 `job.tracks` 這個前置判斷，一個曲目還沒補齊的 job 會被誤判成
        「全部曲目都終結」而被刪掉——這跟 `app/api/routes.py` 的
        `job_events()` 要防的是同一個陷阱，該處的說明有更完整的推導。
        """
        jobs = self.list_jobs()
        ids = [
            job.id
            for job in jobs
            if job.error is not None
            or (job.tracks and all(t.status in self._TERMINAL_TRACK_STATUSES for t in job.tracks))
        ]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._write_lock:
            cursor = self._conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", ids)
            return cursor.rowcount

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
