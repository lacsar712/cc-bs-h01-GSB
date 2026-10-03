"""正确性回归：不依赖真实 PostgreSQL。

用内存假连接驱动 worker 的事务函数与 Sanic 处理器，锁住以下承诺：
- 边界不漂移：80/220 压线合格，79/221 越界；
- 页面/API 看到的结论与库里最终结论完全一致，不翻转；
- worker 写库失败时整体回滚，行保持 pending，不留半写脏行；
- 复核员(reader)不能报送(403)，登录会话可连续分页。
"""

import asyncio
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import jwt

from api import app as appmod
from api.app import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SECRET,
    authorize_submit,
    create_reading,
    list_readings,
    parse_pagination,
    serialize_reading,
)
from rules import judge_microstrain
from worker import process_one


# ---------- 判定规则：边界不漂移 ----------

def test_boundary_values():
    assert judge_microstrain(80.0) == (
        "合格",
        "微应变处于 80～220 με 设计允许范围内",
    )
    assert judge_microstrain(220.0)[0] == "合格"  # 压线合格
    assert judge_microstrain(150.0)[0] == "合格"
    assert judge_microstrain(79.9)[0] == "越界"  # 明显越界不得漂成合格
    assert judge_microstrain(220.1)[0] == "越界"
    assert judge_microstrain(40.0)[0] == "越界"


# ---------- worker：单事务原子落库 ----------

class _FakeTx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        # 进入事务时对整表做快照，供回滚恢复。
        self.conn._snapshot = copy.deepcopy(self.conn.rows)
        self.conn.committed = False
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.rollback()
        else:
            self.conn.commit_tx()
        return False


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeSyncConn:
    """模拟 psycopg 同步连接的事务/execute 语义。"""

    def __init__(self, rows, fail_done=False):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.fail_done = fail_done
        self._snapshot = None
        self.committed = False
        self.done_updates = []

    def transaction(self):
        return _FakeTx(self)

    def rollback(self):
        if self._snapshot is not None:
            self.rows = self._snapshot
            self._snapshot = None

    def commit_tx(self):
        self._snapshot = None
        self.committed = True

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).upper()
        if "FOR UPDATE SKIP LOCKED" in s:
            pending = sorted(
                (r for r in self.rows.values() if r["status"] == "pending"),
                key=lambda r: r["id"],
            )
            return _FakeResult(dict(pending[0]) if pending else None)
        if "SET STATUS = 'DONE'" in s:
            if self._snapshot is None:
                raise RuntimeError("在事务外写结论")
            if self.fail_done:
                raise RuntimeError("写库失败")
            verdict, reason, rid = params
            self.done_updates.append(params)
            self.rows[rid].update(
                status="done",
                verdict=verdict,
                reason=reason,
                processed_at="NOW",
            )
            return _FakeResult(None)
        raise AssertionError(f"未预期的 SQL: {s}")


def _pending(rid, value):
    return {
        "id": rid,
        "span_code": f"S{rid}",
        "microstrain": value,
        "verdict": None,
        "reason": None,
        "status": "pending",
        "created_by": "surveyor",
        "created_at": None,
        "processed_at": None,
    }


def test_worker_no_pending_is_noop():
    conn = FakeSyncConn([_pending(1, 150)])
    conn.rows[1]["status"] = "done"
    assert process_one(conn) is False
    assert conn.done_updates == []


def test_worker_writes_true_verdict_and_commits():
    conn = FakeSyncConn([_pending(1, 80.0), _pending(2, 221.0)])
    assert process_one(conn) is True  # 认领 id 最小的 1
    assert conn.committed is True
    row = conn.rows[1]
    assert row["status"] == "done"
    assert row["verdict"] == "合格"  # 压线合格，未被旁路翻成越界
    assert "80" in row["reason"]
    # 另一条未被触碰
    assert conn.rows[2]["status"] == "pending"


def test_worker_failure_rolls_back_without_dirty_row():
    conn = FakeSyncConn([_pending(1, 150.0)], fail_done=True)
    try:
        process_one(conn)
    except RuntimeError:
        pass
    else:
        raise AssertionError("写库失败应当抛出")
    # 整体回滚：未提交、行仍是 pending、没有半写 verdict。
    assert conn.committed is False
    row = conn.rows[1]
    assert row["status"] == "pending"
    assert row["verdict"] is None
    assert row["reason"] is None
    assert row["processed_at"] is None


# ---------- 纯函数：权限 / 分页 / 序列化 ----------

def test_authorize_submit():
    assert authorize_submit(None) == "未登录"
    assert authorize_submit({"role": "reader"}) is not None  # 复核员禁报
    assert "复核" in authorize_submit({"role": "reader"})
    assert authorize_submit({"role": "writer"}) is None


def test_parse_pagination():
    assert parse_pagination({}) == (1, DEFAULT_PAGE_SIZE)
    assert parse_pagination({"page": "3", "page_size": "5"}) == (3, 5)
    assert parse_pagination({"page": "0"})[0] == 1
    assert parse_pagination({"page": "-2"})[0] == 1
    assert parse_pagination({"page": "abc"})[0] == 1
    assert parse_pagination({"page_size": "0"})[1] == 1
    assert parse_pagination({"page_size": "999"})[1] == MAX_PAGE_SIZE
    assert parse_pagination({"page_size": "xyz"})[1] == DEFAULT_PAGE_SIZE


def test_serialize_keeps_true_verdict():
    base = {
        "id": 7,
        "span_code": "跨中S7",
        "microstrain": 80.0,
        "verdict": "合格",
        "reason": "微应变处于 80～220 με 设计允许范围内",
        "status": "done",
        "created_by": "surveyor",
        "created_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
        "processed_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
    }
    out = serialize_reading(base)
    assert out["verdict"] == "合格"  # 库里合格，表面必须还是合格
    assert "80" in out["reason"]

    bad = dict(base, verdict="越界", reason="微应变低于 80 με 设计下限")
    assert serialize_reading(bad)["verdict"] == "越界"


# ---------- 异步假连接，驱动真实 Sanic 处理器 ----------

class _FakeAsyncCursor:
    def __init__(self, conn):
        self.conn = conn
        self.mode = None
        self.params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        s = " ".join(sql.split()).upper()
        self.params = params
        if "COUNT(*)" in s:
            self.mode = "count"
        elif "ORDER BY ID DESC" in s:
            self.mode = "select"
        elif "INSERT INTO STRAIN_READINGS" in s:
            self.mode = "insert"
            span_code, microstrain, created_by = params
            self.conn.last_inserted = {
                "id": len(self.conn.store) + 1,
                "span_code": span_code,
                "microstrain": float(microstrain),
                "verdict": None,
                "reason": None,
                "status": "pending",
                "created_by": created_by,
                "created_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
                "processed_at": None,
            }
        else:
            raise AssertionError(f"未预期的 SQL: {s}")

    async def fetchone(self):
        if self.mode == "count":
            return {"n": len(self.conn.store)}
        if self.mode == "insert":
            return self.conn.last_inserted
        return None

    async def fetchall(self):
        if self.mode != "select":
            return []
        page_size, offset = self.params
        ordered = sorted(self.conn.store, key=lambda r: r["id"], reverse=True)
        return [dict(r) for r in ordered[offset:offset + page_size]]


class FakeAsyncConn:
    def __init__(self, store):
        self.store = store
        self.last_inserted = None
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def cursor(self):
        return _FakeAsyncCursor(self)

    async def commit(self):
        self.commits += 1
        if self.last_inserted is not None:
            self.store.append(self.last_inserted)
            self.last_inserted = None


class FakePool:
    def __init__(self, store):
        self._conn = FakeAsyncConn(store)

    def connection(self):
        return self._conn


class FakeRequest:
    def __init__(self, token=None, json_body=None, args=None, store=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.headers = headers
        self._json = json_body if json_body is not None else {}
        self.args = args or {}
        pool = FakePool(store if store is not None else [])
        self.app = SimpleNamespace(ctx=SimpleNamespace(pool=pool))

    @property
    def json(self):
        return self._json


def _token(username, role):
    return jwt.encode({"sub": username, "role": role}, SECRET, algorithm="HS256")


def _stored(rid, value, verdict, reason):
    return {
        "id": rid,
        "span_code": f"S{rid}",
        "microstrain": value,
        "verdict": verdict,
        "reason": reason,
        "status": "done",
        "created_by": "surveyor",
        "created_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
        "processed_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
    }


def _run(coro):
    return asyncio.run(coro)


def _decode(resp):
    import json

    return resp.status, json.loads(resp.body)


def test_list_requires_login():
    req = FakeRequest(store=[])
    status, body = _decode(_run(list_readings(req)))
    assert status == 401


def test_list_paginates_and_surfaces_true_verdict():
    store = []
    for i in range(1, 24):  # 23 行
        verdict, reason = judge_microstrain(float(80 + i))  # 部分合格部分越界
        store.append(_stored(i, float(80 + i), verdict, reason))
    # 明确放入一条压线合格
    store.append(_stored(24, 80.0, "合格", "微应变处于 80～220 με 设计允许范围内"))

    req = FakeRequest(
        token=_token("reviewer", "reader"),
        args={"page": "2", "page_size": "10"},
        store=store,
    )
    status, body = _decode(_run(list_readings(req)))
    assert status == 200
    assert body["total"] == 24
    assert body["page"] == 2
    assert body["page_size"] == 10
    assert len(body["items"]) == 10
    # id DESC：第2页首条应为 id=14（第1页是 24..15）
    assert body["items"][0]["id"] == 14
    # 表面结论必须与库一致：压线合格行就是合格
    by_id = {r["id"]: r for r in body["items"]}
    if 14 in by_id:
        assert by_id[14]["verdict"] == judge_microstrain(by_id[14]["microstrain"])[0]

    # 同一登录会话再翻到含 id=24 的第1页
    req2 = FakeRequest(
        token=_token("reviewer", "reader"),
        args={"page": "1", "page_size": "10"},
        store=store,
    )
    _, body1 = _decode(_run(list_readings(req2)))
    top = body1["items"][0]
    assert top["id"] == 24 and top["verdict"] == "合格"


def test_reader_cannot_submit():
    store = []
    req = FakeRequest(
        token=_token("reviewer", "reader"),
        json_body={"span_code": "跨中S9", "microstrain": 150},
        store=store,
    )
    status, body = _decode(_run(create_reading(req)))
    assert status == 403  # 复核身份继续不能报送
    assert store == []     # 且没有写入任何行


def test_anonymous_cannot_submit():
    req = FakeRequest(json_body={"span_code": "x", "microstrain": 1}, store=[])
    status, _ = _decode(_run(create_reading(req)))
    assert status == 401


def test_writer_submit_enqueues_pending():
    store = []
    req = FakeRequest(
        token=_token("surveyor", "writer"),
        json_body={"span_code": "跨中S3", "microstrain": 220.0},
        store=store,
    )
    status, body = _decode(_run(create_reading(req)))
    assert status == 201
    assert store and store[0]["status"] == "pending"
    assert store[0]["verdict"] is None  # 结论由 worker 落库，接口不预判
    assert body["verdict"] is None
