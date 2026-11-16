from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from devpilot.core.memory.models import MemoryFact
from devpilot.core.memory.schema import SCHEMA_SQL, SCHEMA_VERSION

Row = dict[str, Any]


# 将整个数据库事务放到工作线程，兼容 Windows 且不阻塞主事件循环
def _in_worker[**P, T](operation: Callable[P, T]) -> Callable[P, Awaitable[T]]:
    @functools.wraps(operation)
    # 保持异步接口并在独立线程执行同步连接池操作
    async def run(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(operation, *args, **kwargs)

    return run


class PostgresMemoryStore:
    # 配置有界连接池，连接串不进入日志或异常说明
    def __init__(self, dsn: str, max_size: int = 4) -> None:
        self._pool: ConnectionPool[Connection[Row]] = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            open=False,
            timeout=5,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 5,
                "options": "-c statement_timeout=5000 -c lock_timeout=3000",
            },
        )

    # 建立连接并在事务锁保护下应用数据库结构版本
    @_in_worker
    def open(self) -> None:
        try:
            self._pool.open(wait=True, timeout=8)
            with self._pool.connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(746238194)")
                conn.execute(SCHEMA_SQL)
                cursor = conn.execute(
                    "SELECT max(version) AS version FROM devpilot_memory.migrations"
                )
                row = cursor.fetchone()
                if row and row["version"] and row["version"] > SCHEMA_VERSION:
                    raise RuntimeError("memory database requires a newer DevPilot version")
                conn.execute(
                    "INSERT INTO devpilot_memory.migrations(version) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (SCHEMA_VERSION,),
                )
        except Exception:
            self._pool.close()
            raise RuntimeError(
                "PostgreSQL memory initialization failed; check database settings"
            ) from None

    # 关闭连接池并释放所有数据库连接
    @_in_worker
    def close(self) -> None:
        self._pool.close()

    # 对单个用户画像字段加事务锁，覆盖首次插入和已有记录更新
    def _lock(self, conn: Connection[Row], user_id: str, key: str) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (json.dumps([user_id, key]),)
        )

    # 原子更新画像当前版本并追加不可变的变更历史
    def _write(
        self,
        conn: Connection[Row],
        user_id: str,
        key: str,
        value: str,
        session_id: str,
        message_id: str,
        quote: str,
        action: str,
        *,
        status: str = "active",
        locked: bool = False,
    ) -> Row:
        cursor = conn.execute(
            "INSERT INTO devpilot_memory.profiles "
            "(user_id,key,value,status,locked,source_session,source_message,quote) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id,key) DO UPDATE SET value=EXCLUDED.value, "
            "status=EXCLUDED.status,locked=EXCLUDED.locked, "
            "source_session=EXCLUDED.source_session,source_message=EXCLUDED.source_message, "
            "quote=EXCLUDED.quote,version=profiles.version+1,updated_at=now() RETURNING *",
            (user_id, key, value, status, locked, session_id, message_id, quote),
        )
        row = cursor.fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO devpilot_memory.profile_history "
            "(user_id,key,value,version,action,source_session,source_message,quote) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, key, value, row["version"], action, session_id, message_id, quote),
        )
        return row

    # 幂等记录用户证据，按稳定性和锁定规则决定更新画像或保留候选项
    @_in_worker
    def observe(
        self,
        user_id: str,
        session_id: str,
        message_id: str,
        fact: MemoryFact,
    ) -> str:
        fingerprint = hashlib.sha256(
            json.dumps(
                [session_id, message_id, fact.key, fact.value],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        with self._pool.connection() as conn:
            self._lock(conn, user_id, fact.key)
            cursor = conn.execute(
                "INSERT INTO devpilot_memory.observations "
                "(id,user_id,key,value,quote,intent,source_session,source_message,"
                "fingerprint,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') "
                "ON CONFLICT (user_id,fingerprint) DO NOTHING RETURNING id",
                (
                    uuid.uuid4(),
                    user_id,
                    fact.key,
                    fact.value,
                    fact.quote,
                    fact.intent,
                    session_id,
                    message_id,
                    fingerprint,
                ),
            )
            observation = cursor.fetchone()
            if observation is None:
                return "duplicate"
            cursor = conn.execute(
                "SELECT * FROM devpilot_memory.profiles WHERE user_id=%s AND key=%s",
                (user_id, fact.key),
            )
            current = cursor.fetchone()
            accepted = fact.intent in ("stable", "correction") and (
                current is None
                or (
                    current["status"] == "active"
                    and (
                        current["value"] == fact.value
                        or (not current["locked"] and fact.intent == "correction")
                    )
                )
            )
            if accepted:
                if current is None or current["value"] != fact.value:
                    self._write(
                        conn,
                        user_id,
                        fact.key,
                        fact.value,
                        session_id,
                        message_id,
                        fact.quote,
                        "observed",
                    )
                conn.execute(
                    "UPDATE devpilot_memory.observations SET status='accepted' WHERE id=%s",
                    (observation["id"],),
                )
                conn.execute(
                    "UPDATE devpilot_memory.observations SET status='rejected' "
                    "WHERE user_id=%s AND key=%s AND status='pending'",
                    (user_id, fact.key),
                )
                return "accepted"
            cursor = conn.execute(
                "SELECT id FROM devpilot_memory.observations "
                "WHERE user_id=%s AND key=%s AND value=%s AND status='pending' AND id<>%s LIMIT 1",
                (user_id, fact.key, fact.value, observation["id"]),
            )
            if cursor.fetchone() is not None:
                conn.execute(
                    "UPDATE devpilot_memory.observations SET status='rejected' WHERE id=%s",
                    (observation["id"],),
                )
                return "duplicate"
            return "pending"

    # 在事务中执行用户主动编辑、删除或锁定，删除保留墓碑防止旧证据复活
    @_in_worker
    def edit(
        self,
        user_id: str,
        session_id: str,
        key: str,
        action: str,
        value: str = "",
    ) -> Row:
        with self._pool.connection() as conn:
            self._lock(conn, user_id, key)
            cursor = conn.execute(
                "SELECT * FROM devpilot_memory.profiles WHERE user_id=%s AND key=%s",
                (user_id, key),
            )
            current = cursor.fetchone()
            if action in ("lock", "unlock") and (not current or current["status"] != "active"):
                raise ValueError("active profile field not found")
            if action == "forget":
                conn.execute(
                    "UPDATE devpilot_memory.observations SET status='rejected',value='',quote='' "
                    "WHERE user_id=%s AND key=%s",
                    (user_id, key),
                )
                conn.execute(
                    "UPDATE devpilot_memory.profile_history SET value='',quote='' "
                    "WHERE user_id=%s AND key=%s",
                    (user_id, key),
                )
            if action == "set":
                conn.execute(
                    "UPDATE devpilot_memory.observations SET status='rejected' "
                    "WHERE user_id=%s AND key=%s AND status='pending'",
                    (user_id, key),
                )
            if action in ("lock", "unlock"):
                assert current is not None
                value = str(current["value"])
            return self._write(
                conn,
                user_id,
                key,
                value,
                session_id,
                "manual",
                "",
                action,
                status="deleted" if action == "forget" else "active",
                locked=action == "lock" or bool(current and current["locked"] and action == "set"),
            )

    # 确认候选事实并将同字段其余候选作废，避免旧候选覆盖最新用户决策
    @_in_worker
    def confirm(self, user_id: str, session_id: str, candidate_id: str) -> Row:
        candidate_uuid = uuid.UUID(candidate_id)
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "SELECT key FROM devpilot_memory.observations WHERE user_id=%s AND id=%s",
                (user_id, candidate_uuid),
            )
            candidate = cursor.fetchone()
            if candidate is None:
                raise ValueError("candidate not found")
            self._lock(conn, user_id, candidate["key"])
            cursor = conn.execute(
                "SELECT * FROM devpilot_memory.observations "
                "WHERE user_id=%s AND id=%s AND status='pending' FOR UPDATE",
                (user_id, candidate_uuid),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("candidate is no longer pending")
            cursor = conn.execute(
                "SELECT locked FROM devpilot_memory.profiles WHERE user_id=%s AND key=%s",
                (user_id, row["key"]),
            )
            current = cursor.fetchone()
            result = self._write(
                conn,
                user_id,
                row["key"],
                row["value"],
                row["source_session"],
                row["source_message"],
                row["quote"],
                "confirmed",
                locked=bool(current and current["locked"]),
            )
            conn.execute(
                "UPDATE devpilot_memory.observations SET status=CASE WHEN id=%s "
                "THEN 'accepted' ELSE 'rejected' END WHERE user_id=%s AND key=%s "
                "AND status='pending'",
                (candidate_uuid, user_id, row["key"]),
            )
            return result

    # 读取指定用户的正式画像或待确认候选，按稳定顺序返回有界结果
    @_in_worker
    def profiles(self, user_id: str, *, pending: bool = False) -> list[Row]:
        with self._pool.connection() as conn:
            if pending:
                cursor = conn.execute(
                    "SELECT * FROM devpilot_memory.observations WHERE user_id=%s "
                    "AND status='pending' ORDER BY created_at,id LIMIT 200",
                    (user_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM devpilot_memory.profiles WHERE user_id=%s "
                    "AND status='active' ORDER BY key LIMIT 200",
                    (user_id,),
                )
            return list(cursor.fetchall())

    # 读取一个画像字段最近的变更历史
    @_in_worker
    def history(self, user_id: str, key: str) -> list[Row]:
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM devpilot_memory.profile_history WHERE user_id=%s AND key=%s "
                "ORDER BY version DESC LIMIT 50",
                (user_id, key),
            )
            return list(cursor.fetchall())

    # 幂等写入有有效期的会话笔记，重放相同内容不会延长过期时间
    @_in_worker
    def save_short(
        self,
        user_id: str,
        project_id: str,
        session_id: str,
        content: str,
        message_id: str,
        ttl_hours: int,
        *,
        turn_only: bool = False,
    ) -> None:
        key = hashlib.sha256(json.dumps([message_id, content]).encode()).hexdigest()
        if turn_only:
            key = "turn:" + key
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO devpilot_memory.short_term "
                "(user_id,project_id,session_id,key,content,source_message,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,now() + %s * interval '1 hour') "
                "ON CONFLICT DO NOTHING",
                (user_id, project_id, session_id, key, content, message_id, ttl_hours),
            )

    # 按用户、项目和会话读取尚未过期的短期记忆
    @_in_worker
    def short_term(self, user_id: str, project_id: str, session_id: str) -> list[Row]:
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM devpilot_memory.short_term "
                "WHERE user_id=%s AND project_id=%s AND session_id=%s "
                "AND NOT deleted AND expires_at>now() ORDER BY created_at DESC,key LIMIT 100",
                (user_id, project_id, session_id),
            )
            return list(cursor.fetchall())

    # 清空当前会话笔记并保留内容指纹，防止旧文件或重放立即恢复记忆
    @_in_worker
    def clear_short(self, user_id: str, project_id: str, session_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE devpilot_memory.short_term SET deleted=true,content='' "
                "WHERE user_id=%s AND project_id=%s AND session_id=%s",
                (user_id, project_id, session_id),
            )
