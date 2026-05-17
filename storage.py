import aiosqlite
from typing import List, Tuple

DB_PATH = "jobs.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            location TEXT,
            url TEXT
        )
        """)
        await db.commit()


async def job_exists(job_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM jobs WHERE id = ?",
            (job_id,)
        )
        row = await cursor.fetchone()
        return row is not None


async def save_job(job_id: str, title: str, location: str, url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO jobs (id, title, location, url) VALUES (?, ?, ?, ?)",
            (job_id, title, location, url)
        )
        await db.commit()


async def get_all_jobs() -> List[Tuple[str, str, str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT * FROM jobs")
        rows = await cursor.fetchall()
        return rows


async def debug_print_jobs():
    try:
        jobs = await get_all_jobs()
        print(f"✅ Total jobs stored: {len(jobs)}")
    except Exception as e:
        print(f"❌ Error reading DB: {e}")
