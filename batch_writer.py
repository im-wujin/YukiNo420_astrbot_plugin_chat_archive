from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from astrbot.api import logger

try:
    from .db_config import get_db_connection
except ImportError:
    from db_config import get_db_connection


class ArchiveBatchWriter:
    def __init__(
        self,
        *,
        batch_size: int,
        flush_interval: float,
        data_dir: Path,
        queue_max_size: int = 5000,
    ):
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.data_dir = data_dir
        self._write_queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max_size)
        self._writer_task: asyncio.Task | None = None
        self._writer_lock = asyncio.Lock()
        self._shutting_down = False

    @property
    def queue(self) -> asyncio.Queue:
        return self._write_queue

    async def start(self):
        """Lazily start the background batch writer task."""
        async with self._writer_lock:
            if not self._shutting_down and (
                self._writer_task is None or self._writer_task.done()
            ):
                self._writer_task = asyncio.create_task(self.run())

    async def enqueue(self, record: tuple):
        """Enqueue a record with backpressure to avoid unbounded memory growth."""
        if self._shutting_down:
            return
        try:
            await asyncio.wait_for(self._write_queue.put(record), timeout=1.0)
        except asyncio.TimeoutError:
            logger.error("Chat Archive: 写入队列已满，丢弃一条归档记录以保护主进程内存。")

    async def stop(self):
        self._shutting_down = True

        if self._writer_task and not self._writer_task.done():
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        remaining = []
        while not self._write_queue.empty():
            try:
                remaining.append(self._write_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining:
            self.flush_batches_sync(remaining)

    async def run(self):
        """Background coroutine: drain queue and batch-insert to DB."""
        buffer: list[tuple] = []
        while True:
            try:
                try:
                    item = await asyncio.wait_for(
                        self._write_queue.get(), timeout=self.flush_interval
                    )
                    buffer.append(item)
                except asyncio.TimeoutError:
                    pass

                while not self._write_queue.empty() and len(buffer) < self.batch_size:
                    try:
                        buffer.append(self._write_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if buffer:
                    batch = buffer.copy()
                    buffer.clear()
                    await asyncio.to_thread(self.flush_batch_sync, batch)
            except asyncio.CancelledError:
                while not self._write_queue.empty():
                    try:
                        buffer.append(self._write_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if buffer:
                    try:
                        self.flush_batches_sync(buffer)
                    except Exception as e:
                        logger.error(f"Chat Archive: final flush error: {e}")
                break
            except Exception as e:
                logger.error(
                    f"Chat Archive: batch writer error, discarding {len(buffer)} messages: {e}"
                )
                buffer.clear()

    def write_failed_batch(self, batch: list[tuple], reason: str):
        """Persist failed DB writes for later manual recovery instead of silently losing them."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            failed_path = self.data_dir / "chat_archive_failed_writes.jsonl"
            with open(failed_path, "a", encoding="utf-8") as f:
                for item in batch:
                    f.write(
                        json.dumps(
                            {
                                "reason": reason,
                                "record": item,
                                "failed_at": int(time.time()),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            logger.error(f"Chat Archive: {len(batch)} 条写入失败记录已保存到 {failed_path}")
        except Exception as e:
            logger.error(f"Chat Archive: 保存失败写入记录也失败了: {e}")

    def flush_batches_sync(self, records: list[tuple]):
        """Flush records in bounded chunks to avoid long SQLite write locks."""
        for start in range(0, len(records), self.batch_size):
            self.flush_batch_sync(records[start : start + self.batch_size])

    def flush_batch_sync(self, batch: list[tuple]):
        """Flush a batch of messages to the database."""
        if not batch:
            return
        try:
            batch = [self.normalize_record_tuple(record) for record in batch]
        except Exception as e:
            logger.error(
                f"Chat Archive: invalid archive batch, dropping {len(batch)} messages: {e}"
            )
            self.write_failed_batch(batch, str(e))
            return

        conn = None
        retries = 3
        delays = [0.5, 1.0, 2.0]

        for attempt in range(retries):
            try:
                conn = get_db_connection()
                conn.executemany(
                    "INSERT INTO chat_history ("
                    "user_id, sender_name, message, timestamp, session_id, "
                    "message_type, session_name, msg_id, platform_id, platform_name, avatar_url, guild_avatar_url"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()
                break
            except sqlite3.OperationalError as e:
                if conn:
                    try:
                        conn.rollback()
                    except Exception as rollback_error:
                        logger.error(f"Chat Archive: DB rollback failed: {rollback_error}")
                if attempt < retries - 1:
                    logger.warning(
                        f"Chat Archive: DB busy, retrying flush in {delays[attempt]}s: {e}"
                    )
                    time.sleep(delays[attempt])
                else:
                    logger.error(
                        f"Chat Archive: Failed to flush batch after retries ({len(batch)} msgs): {e}"
                    )
                    self.write_failed_batch(batch, str(e))
            except Exception as e:
                if conn:
                    try:
                        conn.rollback()
                    except Exception as rollback_error:
                        logger.error(f"Chat Archive: DB rollback failed: {rollback_error}")
                logger.error(f"Chat Archive: Failed to flush batch ({len(batch)} msgs): {e}")
                self.write_failed_batch(batch, str(e))
                break
            finally:
                if conn:
                    conn.close()
                    conn = None

    @staticmethod
    def normalize_record_tuple(record: tuple) -> tuple:
        """Normalize archive write tuples across old and new in-memory callers."""
        if len(record) == 12:
            return record
        if len(record) == 11:
            return (*record, "")
        if len(record) == 10:
            return (*record, "", "")
        if len(record) == 8:
            return (*record, "", "", "", "")
        raise ValueError(f"unexpected archive record length: {len(record)}")
