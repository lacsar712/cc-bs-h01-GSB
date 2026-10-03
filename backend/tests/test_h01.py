"""H01 回归：压线判定不漂移，工人落库与库中最终结论一致，失败不留脏行。"""

import pytest

from rules import judge_microstrain
from worker import process_one


class _FakeTx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.committed = True
        else:
            self.conn.rolled_back = True
        return False


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row, fail_on_update=False):
        self.row = row
        self.fail_on_update = fail_on_update
        self.updates = []
        self.committed = False
        self.rolled_back = False

    def transaction(self):
        return _FakeTx(self)

    def execute(self, sql, params=None):
        if "FOR UPDATE" in sql:
            return _FakeResult(self.row)
        if self.fail_on_update:
            raise RuntimeError("db write failed")
        self.updates.append((sql, params))
        return _FakeResult(None)


@pytest.mark.parametrize("microstrain", [80, 150, 220])
def test_pass_values_stay_pass(microstrain):
    # 压线合格（80/220 含边界）不得漂成越界
    verdict, _ = judge_microstrain(microstrain)
    assert verdict == "合格"


@pytest.mark.parametrize("microstrain", [79.9, 40, 220.1, 300])
def test_fail_values_stay_fail(microstrain):
    # 明显越界不得漂成合格
    verdict, _ = judge_microstrain(microstrain)
    assert verdict == "越界"


def test_worker_saves_judged_verdict_verbatim():
    conn = _FakeConn({"id": 1, "microstrain": 150.0})
    assert process_one(conn) is True
    ((_sql, params),) = conn.updates
    assert params[0] == "合格"
    assert "80～220" in params[1]
    assert conn.committed and not conn.rolled_back


def test_worker_failed_write_leaves_no_dirty_row():
    conn = _FakeConn({"id": 2, "microstrain": 150.0}, fail_on_update=True)
    with pytest.raises(RuntimeError):
        process_one(conn)
    # 写入失败整体回滚：行保持 pending，不留改了一半的脏行
    assert conn.rolled_back and not conn.committed
