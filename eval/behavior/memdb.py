"""An in-memory stand-in for the Supabase client, just enough for server.py's tools.

The behaviour evaluation runs the real tool functions from server.py (same validation, same
supersede guards, same project_brief with its staleness flags), only against this instead of
Supabase. No network, no API keys, and every scenario starts from a known set of memories.

Search here is NOT the real hybrid search: the "embedding" of a text is the text itself, and
memories are ranked by how many character pairs they share with the query. With the handful of
memories a scenario has, that is enough for the assistant to find what it looks for. The
evaluation measures what the assistant does with the tools, not ranking (see eval/README.md for
the retrieval evaluation).
"""
import difflib
import uuid


def embed(text: str) -> str:
    """Stand-in for server.embed: keep the text, so match/search below can compare texts."""
    return text


def _pairs(text: str) -> set[str]:
    t = "".join(text.lower().split())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _overlap(query: str, content: str) -> float:
    q = _pairs(query)
    return len(q & _pairs(content)) / len(q) if q else 0.0


class Result:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class Query:
    def __init__(self, db, table, op, payload=None):
        self.db, self.table, self.op, self.payload = db, table, op, payload
        self.filters, self.sort, self.n = [], None, None

    def select(self, _columns):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def is_(self, col, _null):
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def or_(self, expr):
        # server.py uses exactly one: 'valid_until.is.null,valid_until.gt.<timestamp>'
        prefix = "valid_until.is.null,valid_until.gt."
        assert expr.startswith(prefix), expr
        ts = expr[len(prefix):]
        self.filters.append(lambda r: r.get("valid_until") is None or r["valid_until"] > ts)
        return self

    def order(self, col, desc=False):
        self.sort = (col, desc)
        return self

    def limit(self, n):
        self.n = n
        return self

    def execute(self):
        rows = self.db.tables[self.table]
        hits = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "insert":
            row = dict(self.payload)
            if self.table == "memories":
                assert row["project"] in {p["name"] for p in self.db.tables["projects"]}, "foreign key"
                now = self.db.now()
                row = {"id": str(uuid.uuid4()), "tags": [], "superseded_by": None, "valid_until": None,
                       "created_at": now, "updated_at": now, **row}
            else:
                row = {"aliases": [], "created_at": self.db.now(), **row}
            rows.append(row)
            return Result([dict(row)])
        if self.op == "update":
            for r in hits:
                r.update(self.payload)
            return Result([dict(r) for r in hits])
        if self.op == "delete":
            rows[:] = [r for r in rows if r not in hits]
            return Result(hits)
        if self.sort:
            hits.sort(key=lambda r: r[self.sort[0]], reverse=self.sort[1])
        return Result([dict(r) for r in hits[:self.n]])


class Table:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def select(self, _columns):
        return Query(self.db, self.name, "select")

    def insert(self, payload):
        return Query(self.db, self.name, "insert", payload)

    def update(self, payload):
        return Query(self.db, self.name, "update", payload)

    def delete(self):
        return Query(self.db, self.name, "delete")


class MemDB:
    def __init__(self, now):
        """now: callable returning the current time as an ISO string, the server's _now."""
        self.now = now
        self.tables = {"memories": [], "projects": []}

    def table(self, name):
        return Table(self, name)

    def _active(self, r):
        return r["superseded_by"] is None and (r["valid_until"] is None or r["valid_until"] > self.now())

    def rpc(self, name, params):
        rows = [r for r in self.tables["memories"] if r["project"] == params["filter_project"]]
        if name == "match_memories":  # duplicate check on save
            text = params["query_embedding"]
            out = [{**r, "similarity": difflib.SequenceMatcher(None, text, r["content"]).ratio()}
                   for r in rows if self._active(r)]
            out.sort(key=lambda r: -r["similarity"])
            return Result(out[:params["match_count"]])
        assert name == "search_memories", name
        kinds = params["filter_kinds"]
        pool = [r for r in rows
                if (params["include_inactive"] or self._active(r)) and (not kinds or r["kind"] in kinds)]
        scored = [{**r, "similarity": _overlap(params["query_text"], r["content"]), "keyword_score": None}
                  for r in pool]
        scored.sort(key=lambda r: (-r["similarity"], r["content"]))  # ties in a fixed order
        return Result(scored[:params["match_count"]])
