import asyncio
import aiosqlite
from config import DB_PATH, log

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT,
    location TEXT,
    url TEXT,
    seen INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Storage:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._lock = asyncio.Lock()

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(CREATE_TABLE_SQL)
            await db.commit()
        log.info("✅ Database initialized")

    async def save_job(self, job: dict) -> bool:
        """
        Save job if not exists.
        Returns True if inserted, False if duplicate.
        """
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute(
                        "INSERT INTO jobs (id, title, location, url) VALUES (?, ?, ?, ?)",
                        (
                            job.get("id"),
                            job.get("title"),
                            job.get("location"),
                            job.get("url"),
                        ),
                    )
                    await db.commit()
                    return True
                except Exception:
                    return False

    async def get_new_jobs(self):
        """
        Get jobs not yet sent to Telegram
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id, title, location, url FROM jobs WHERE seen = 0"
            )
            rows = await cursor.fetchall()
            return rows

    async def mark_seen(self, job_id: str):
        """
        Mark job as sent
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET seen = 1 WHERE id = ?",
                (job_id,),
            )
            await db.commit()

    async def count_jobs(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM jobs")
            row = await cursor.fetchone()
            return row[0] if row else 0
