"""后台工人：用 SKIP LOCKED 认领 pending 应变读数并写入合格/越界结论。

认领（行锁）与写结论在**同一个事务**内完成：
- 成功才提交；
- 任何异常整体回滚，行保持 pending 可被再次认领，
  不会留下卡在 processing 或只写了一半 verdict/reason 的脏行。
"""

import os
import time

from db import connect_sync, ensure_schema_sync, seed_if_empty_sync
from rules import judge_microstrain

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "1.0"))


def process_one(conn) -> bool:
    """认领一条 pending 读数并在同一事务里写入最终结论。

    返回 True 表示处理并提交了一条；False 表示当前没有待处理读数。
    若判定或写库抛错，事务整体回滚，调用方会看到异常，行仍为 pending。
    """
    with conn.transaction():
        row = conn.execute(
            """
            SELECT id, microstrain
            FROM strain_readings
            WHERE status = 'pending'
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return False

        verdict, reason = judge_microstrain(float(row["microstrain"]))
        conn.execute(
            """
            UPDATE strain_readings
            SET status = 'done',
                verdict = %s,
                reason = %s,
                processed_at = now()
            WHERE id = %s
            """,
            (verdict, reason, row["id"]),
        )
        return True


def run_once(conn) -> bool:
    # 单事务：异常时 with conn.transaction() 已整体回滚，行保持 pending。
    return process_one(conn)


def main() -> None:
    with connect_sync() as conn:
        ensure_schema_sync(conn)
        seed_if_empty_sync(conn)
        conn.commit()

    while True:
        try:
            with connect_sync() as conn:
                processed = run_once(conn)
        except Exception as exc:
            print(f"worker error: {exc}", flush=True)
            processed = False
        if not processed:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
