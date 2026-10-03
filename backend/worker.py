"""后台工人：用 SKIP LOCKED 认领 pending 应变读数并写入合格/越界结论。"""

import os
import time

from db import connect_sync, ensure_schema_sync, seed_if_empty_sync
from rules import judge_microstrain

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "1.0"))


def process_one(conn) -> bool:
    """认领一条待处理读数，并在同一事务内写入最终结论。

    认领、判定、落库在同一个事务中完成：任一步失败都会整体回滚，
    行保持 pending 等待下轮重试，不会留下改了一半的脏行。
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
        if not row:
            return False
        verdict, reason = judge_microstrain(float(row["microstrain"]))
        conn.execute(
            """
            UPDATE strain_readings
            SET status = 'done', verdict = %s, reason = %s, processed_at = now()
            WHERE id = %s
            """,
            (verdict, reason, row["id"]),
        )
    return True


def main() -> None:
    with connect_sync() as conn:
        ensure_schema_sync(conn)
        seed_if_empty_sync(conn)
        conn.commit()

    while True:
        try:
            with connect_sync() as conn:
                processed = process_one(conn)
        except Exception as exc:
            print(f"worker error: {exc}", flush=True)
            processed = False
        if not processed:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
