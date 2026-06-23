"""Minimal stateful stand-in for the supabase-py client used in unit tests.

Supports the call shapes SupabaseStore uses: table CRUD with eq/neq/in_/order/
limit/count filters, and rpc(name, params) dispatched to registered callables.
Filters/rows are plain dicts; enough PostgREST behavior to test the store's
transforms and control flow without a network.
"""

from __future__ import annotations

import uuid


class _Resp:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, table, op, payload=None):
        self._t = table
        self._op = op           # "select" | "insert" | "update" | "delete" | "upsert"
        self._payload = payload
        self._filters = []       # list[(kind, col, val)]
        self._order = None
        self._limit = None
        self._count = None
        self._on_conflict = None

    # filter builders (return self for chaining)
    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        for kind, col, val in self._filters:
            if kind == "eq" and row.get(col) != val:
                return False
            if kind == "neq" and row.get(col) == val:
                return False
            if kind == "in" and row.get(col) not in val:
                return False
        return True

    def execute(self):
        rows = self._t._rows
        if self._op == "select":
            sel = [dict(r) for r in rows if self._match(r)]
            if self._order:
                col, desc = self._order
                sel.sort(key=lambda r: (r.get(col) is not None, r.get(col)), reverse=desc)
            total = len(sel)
            if self._limit is not None:
                sel = sel[: self._limit]
            return _Resp(sel, count=total if self._count == "exact" else None)
        if self._op in ("insert", "upsert"):
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for r in payload:
                r = dict(r)
                r.setdefault("id", uuid.uuid4().hex)
                if self._op == "upsert" and self._on_conflict:
                    key = self._on_conflict
                    existing = next((x for x in rows if x.get(key) == r.get(key)), None)
                    if existing:
                        existing.update(r)
                        out.append(dict(existing))
                        continue
                rows.append(r)
                out.append(dict(r))
            return _Resp(out)
        if self._op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._payload)
            return _Resp([dict(r) for r in hit])
        if self._op == "delete":
            keep, removed = [], []
            for r in rows:
                (removed if self._match(r) else keep).append(r)
            self._t._rows[:] = keep
            return _Resp([dict(r) for r in removed])
        raise AssertionError(self._op)


class _Table:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_cols, count=None):
        q = _Query(self, "select")
        q._count = count
        return q

    def insert(self, payload):
        return _Query(self, "insert", payload)

    def upsert(self, payload, on_conflict=None):
        q = _Query(self, "upsert", payload)
        q._on_conflict = on_conflict
        return q

    def update(self, payload):
        return _Query(self, "update", payload)

    def delete(self):
        return _Query(self, "delete")


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self._rpcs = {}

    def table(self, name):
        return _Table(self.tables.setdefault(name, []))

    def register_rpc(self, name, fn):
        self._rpcs[name] = fn

    def rpc(self, name, params):
        fn = self._rpcs[name]
        return _RpcResp(fn(params))


class _RpcResp:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return _Resp(self._data)
