"""Read-only web viewer for the memories, served by the same process as the MCP server.

    https://<your-service>.onrender.com/viewer

Disabled unless VIEWER_TOKEN is set (at least 24 characters, e.g. `openssl rand -hex 24`).
You enter the token once on a login page; the browser then keeps a signed cookie for a year.
Changing VIEWER_TOKEN logs every browser out.

Read-only by construction: every route here only runs SELECTs (and the search function,
which is itself read-only). Nothing in this module inserts, updates or deletes.
"""
import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from collections import Counter
from collections.abc import Callable
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

log = logging.getLogger("viewer")

COOKIE = "sb_viewer"
MIN_TOKEN_LENGTH = 24
PAGE = 1000  # rows asked for per request; Supabase may return fewer ("Max rows" setting)
ID_CHUNK = 100  # memory ids per memory_history request, keeps the URL short
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
COLUMNS = "id, content, project, tags, source, kind, superseded_by, valid_until, created_at, updated_at"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def session_value(token: str) -> str:
    """What the cookie holds: derived from the token, so the token itself never sits in the browser."""
    return hmac.new(token.encode(), b"shared-brain-viewer-v1", hashlib.sha256).hexdigest()


def _secure(request: Request) -> bool:
    """Secure cookies everywhere except plain-http local development. Deliberately not based on
    X-Forwarded-Proto, which a proxy may omit or send as a list."""
    return (request.url.hostname or "") not in LOCAL_HOSTS


def _headers(nonce: str | None = None, secure: bool = False) -> dict:
    h = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
    }
    if secure:
        h["Strict-Transport-Security"] = "max-age=31536000"
    if nonce:
        h["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self' data:; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
    return h


def _page(request: Request, html: str, status: int = 200) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    return HTMLResponse(html.replace("__NONCE__", nonce), status_code=status,
                        headers=_headers(nonce, _secure(request)))


def _json(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=_headers())


def _not_found() -> Response:
    return Response("Not Found", status_code=404)


def _fetch_all(make_query: Callable) -> list[dict]:
    """Page through a query until an empty page. make_query() must return a fresh query ordered
    by a unique column, so pages neither overlap nor skip rows."""
    rows: list[dict] = []
    while True:
        batch = make_query().range(len(rows), len(rows) + PAGE - 1).execute().data
        if not batch:
            return rows
        rows.extend(batch)


def make_routes(db, token: str | None, embed: Callable[[str], list[float]] | None = None) -> list[Route]:
    """All viewer routes. With no usable token every route answers 404, as if the viewer did not exist."""
    enabled = bool(token) and len(token) >= MIN_TOKEN_LENGTH
    if token and not enabled:
        log.warning("VIEWER_TOKEN is shorter than %d characters; the viewer stays disabled", MIN_TOKEN_LENGTH)
    expected = session_value(token).encode() if enabled else b""

    def authed(request: Request) -> bool:
        given = request.cookies.get(COOKIE, "").encode("utf-8", errors="replace")
        return enabled and hmac.compare_digest(given, expected)

    def guard(work):
        """work(request) is plain blocking code (supabase-py and OpenAI are synchronous); it runs in
        a worker thread so a slow viewer request never stalls MCP clients on the same event loop."""
        async def wrapped(request: Request):
            if not enabled:
                return _not_found()
            if not authed(request):
                return _json({"error": "Not signed in."}, 401)
            try:
                return await run_in_threadpool(work, request)
            except Exception:  # keep database errors and their details out of the browser
                log.exception("viewer request failed: %s", request.url.path)
                return _json({"error": "The database request failed. Check the server logs."}, 502)
        return wrapped

    async def index(request: Request):
        if not enabled:
            return _not_found()
        return _page(request, APP_HTML if authed(request) else LOGIN_HTML.replace("__ERROR__", ""))

    async def login(request: Request):
        if not enabled:
            return _not_found()
        form = parse_qs((await request.body()).decode(errors="replace"))
        given = (form.get("token") or [""])[0].strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            await asyncio.sleep(1)  # slows down guessing; the token is long and random anyway
            return _page(request, LOGIN_HTML.replace(
                "__ERROR__", '<p class="error">That token is not right.</p>'), 401)
        resp = RedirectResponse("/viewer", status_code=303, headers=_headers(secure=_secure(request)))
        resp.set_cookie(COOKIE, expected.decode(), max_age=365 * 24 * 3600, path="/viewer",
                        httponly=True, samesite="strict", secure=_secure(request))
        return resp

    async def logout(request: Request):
        if not enabled:
            return _not_found()
        resp = RedirectResponse("/viewer", status_code=303, headers=_headers())
        resp.delete_cookie(COOKIE, path="/viewer", httponly=True, samesite="strict", secure=_secure(request))
        return resp

    def projects(_request: Request):
        rows = db.table("projects").select("*").order("name").execute().data
        return _json([{"name": r["name"], "description": r.get("description", ""),
                       "aliases": list(r.get("aliases") or [])} for r in rows])

    def project(request: Request):
        name = request.path_params["name"]
        # ordered by id only: unique, so paging is stable; the page sorts by time itself
        memories = _fetch_all(lambda: db.table("memories").select(COLUMNS).eq("project", name).order("id"))
        ids = [m["id"] for m in memories]
        versions: Counter = Counter()
        for i in range(0, len(ids), ID_CHUNK):
            chunk = ids[i:i + ID_CHUNK]
            for h in _fetch_all(lambda c=chunk: db.table("memory_history").select("memory_id")
                                .in_("memory_id", c).order("id")):
                versions[h["memory_id"]] += 1
        for m in memories:
            m["versions"] = versions.get(m["id"], 0)
        return _json({"project": name, "now": _now_iso(), "memories": memories})

    def history(request: Request):
        memory_id = request.path_params["id"]
        if not UUID.match(memory_id):
            return _json({"error": "Not a memory id."}, 400)
        rows = (db.table("memory_history").select("id, old_content, old_source, replaced_at")
                .eq("memory_id", memory_id).order("replaced_at", desc=True).execute().data)
        return _json(rows)

    def search(request: Request):
        if embed is None:
            return _json({"error": "Search is not available."}, 501)
        q = request.query_params.get("q", "").strip()
        name = request.query_params.get("project", "")
        if not q or not name:
            return _json({"error": "Give both project and q."}, 400)
        res = db.rpc("search_memories", {
            "query_embedding": embed(q[:2000]), "query_text": q[:2000],
            "filter_project": name, "match_count": 10, "filter_kinds": None,
            "include_inactive": request.query_params.get("history") == "1",
        }).execute()
        return _json([{"id": r["id"], "similarity": r.get("similarity"),
                       "keyword_score": r.get("keyword_score")} for r in res.data])

    return [
        Route("/viewer", index, methods=["GET"]),
        Route("/viewer/login", login, methods=["POST"]),
        Route("/viewer/logout", logout, methods=["POST"]),
        Route("/viewer/api/projects", guard(projects), methods=["GET"]),
        Route("/viewer/api/projects/{name}", guard(project), methods=["GET"]),
        Route("/viewer/api/memories/{id}/history", guard(history), methods=["GET"]),
        Route("/viewer/api/search", guard(search), methods=["GET"]),
    ]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def register(mcp, db, embed=None) -> None:
    """Attach the viewer routes to a FastMCP server (served next to the MCP endpoint),
    through FastMCP's public custom_route decorator."""
    for r in make_routes(db, os.environ.get("VIEWER_TOKEN"), embed):
        mcp.custom_route(r.path, methods=sorted(r.methods - {"HEAD"}))(r.endpoint)


# ---------------------------------------------------------------------------------------------
# Pages. Self-contained: no external fonts, scripts or styles, so the strict CSP above holds.
# ---------------------------------------------------------------------------------------------

_STYLE = """
:root{
  --bg:#eef1f4; --surface:#ffffff; --ink:#18212b; --muted:#5b6876; --faint:#8a96a3; --line:#d5dbe2;
  --focus:#2f5fd0;
  --k-state:#2a68b8; --k-decision:#6a48a8; --k-blocker:#b8322f; --k-context:#6c7986;
  --k-log:#2f8566; --k-session-summary:#a0661a;
  --radius:10px;
  --font:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Noto Sans SC","Microsoft YaHei",
         system-ui,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#121820; --surface:#1a222c; --ink:#e3e8ee; --muted:#9aa7b4; --faint:#6e7b88; --line:#2c3743;
    --focus:#7fa6ff;
    --k-state:#6fa6ec; --k-decision:#a98ae6; --k-blocker:#ef7570; --k-context:#93a1ae;
    --k-log:#5fc19b; --k-session-summary:#d9a253;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 var(--font)}
button,input{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.wrap{max-width:760px;margin:0 auto;padding:0 16px 64px}
.error{color:var(--k-blocker)}
"""

LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>shared-brain</title>
<style nonce="__NONCE__">""" + _STYLE + """
.login{max-width:380px;margin:18vh auto 0;padding:0 16px}
h1{font-size:22px;margin:0 0 4px}
p{color:var(--muted);margin:0 0 20px}
input{width:100%;padding:12px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}
button{margin-top:12px;width:100%;padding:12px;border:0;border-radius:var(--radius);
       background:var(--ink);color:var(--bg);font-weight:600;cursor:pointer}
.error{margin:12px 0 0}
</style></head>
<body><main class="login">
<h1>shared-brain</h1>
<p>输入 VIEWER_TOKEN 查看记忆。这个浏览器会记住一年。</p>
<form method="post" action="/viewer/login">
<input name="token" type="password" autocomplete="current-password" aria-label="Viewer token" required autofocus>
<button type="submit">登录</button>
__ERROR__
</form>
</main></body></html>
"""

APP_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>shared-brain</title>
<style nonce="__NONCE__">""" + _STYLE + """
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding-top:14px}
.bar h1{font-size:17px;margin:0;letter-spacing:.01em}
.bar form button{background:none;border:0;color:var(--muted);cursor:pointer;padding:4px 0}
.projects{display:flex;gap:6px;overflow-x:auto;padding:10px 0 12px;scrollbar-width:none}
.projects::-webkit-scrollbar{display:none}
.projects button{flex:none;border:1px solid var(--line);background:var(--surface);border-radius:999px;
                 padding:5px 14px;cursor:pointer;white-space:nowrap}
.projects button[aria-pressed=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}

.intro{margin:22px 0 6px}
.intro h2{font-size:28px;line-height:1.25;margin:0}
.intro p{margin:4px 0 0;color:var(--muted)}
.intro .aliases{color:var(--faint);font-size:13px}

.brief{margin-top:22px}
.brief h3{font-size:13px;font-weight:600;color:var(--muted);margin:26px 0 8px}
.now .entry .text{font-size:17px;line-height:1.7}
.empty{color:var(--faint);margin:0}

.entry{position:relative;background:var(--surface);border-radius:var(--radius);
       padding:12px 14px 10px 18px;margin:0 0 8px;border:1px solid var(--line)}
.entry::before{content:"";position:absolute;left:0;top:10px;bottom:10px;width:4px;border-radius:0 3px 3px 0;
               background:var(--kc)}
.entry .kind{font-size:12px;font-weight:600;color:var(--kc)}
.entry .text{margin:2px 0 6px;white-space:pre-wrap;overflow-wrap:anywhere}
.entry .meta{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12.5px;color:var(--faint)}
.entry .tag{color:var(--muted)}
.entry.inactive{background:transparent}
.entry.inactive .text{color:var(--muted)}
.entry .status{font-size:12.5px;color:var(--muted);margin-top:6px}
.entry .links{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:6px}
.linkbtn{background:none;border:0;padding:0;color:var(--focus);cursor:pointer;font-size:13px}
.versions{margin-top:8px;border-top:1px dashed var(--line);padding-top:8px}
.versions .old{font-size:13.5px;color:var(--muted);white-space:pre-wrap;margin:0 0 8px}
.versions .when{display:block;font-size:12px;color:var(--faint)}
.entry.flash{animation:flash 1.4s ease-out}
@keyframes flash{from{box-shadow:0 0 0 3px var(--focus)}to{box-shadow:0 0 0 3px transparent}}
@media (prefers-reduced-motion:reduce){.entry.flash{animation:none;outline:2px solid var(--focus)}}

.k-state{--kc:var(--k-state)} .k-decision{--kc:var(--k-decision)} .k-blocker{--kc:var(--k-blocker)}
.k-context{--kc:var(--k-context)} .k-log{--kc:var(--k-log)} .k-session-summary{--kc:var(--k-session-summary)}

.all{margin-top:40px}
.all>h3{font-size:20px;margin:0 0 12px}
.tools{display:flex;gap:8px}
.tools input{flex:1;min-width:0;padding:9px 12px;border:1px solid var(--line);border-radius:var(--radius);
             background:var(--surface)}
.tools button{flex:none;padding:9px 14px;border:1px solid var(--line);border-radius:var(--radius);
              background:var(--surface);cursor:pointer}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 4px;align-items:center}
.chip{border:1px solid var(--line);background:transparent;border-radius:999px;padding:3px 11px;cursor:pointer;
      font-size:13px}
.chip[aria-pressed=true]{border-color:var(--kc,var(--ink));color:var(--kc,var(--ink));font-weight:600}
.toggle{margin-left:auto;font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px}
.note{font-size:13px;color:var(--muted);margin:8px 0 0}
.note .linkbtn{margin-left:8px}
.day{font-size:13px;color:var(--faint);margin:22px 0 8px}
.score{font-size:12px;color:var(--faint)}
.loading{color:var(--faint);margin:40px 0}
</style></head>
<body>
<header><div class="wrap">
  <div class="bar"><h1>shared-brain</h1>
    <form method="post" action="/viewer/logout"><button type="submit">退出</button></form></div>
  <nav class="projects" id="projects" aria-label="项目"></nav>
</div></header>
<main class="wrap" id="main"><p class="loading">加载中…</p></main>

<script nonce="__NONCE__">
"use strict";
const KIND = {"state":"进展","decision":"决策","blocker":"卡点","context":"背景","log":"记录",
              "session-summary":"会话小结"};
const ORDER = ["state","blocker","decision","session-summary","log","context"];
const S = {projects:[], project:null, memories:[], byId:new Map(), now:"", kinds:new Set(),
           history:false, text:"", ranked:null};

function el(tag, attrs, ...kids){
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs||{})){
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false)
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
async function api(path){
  const r = await fetch(path, {credentials:"same-origin"});
  if (r.status === 401){ location.reload(); throw new Error("signed out"); }
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
  return body;
}
const fmt = new Intl.DateTimeFormat("zh-CN",{year:"numeric",month:"2-digit",day:"2-digit",
                                               hour:"2-digit",minute:"2-digit"});
const fmtDay = new Intl.DateTimeFormat("zh-CN",{year:"numeric",month:"long",day:"numeric",weekday:"short"});
const when = s => fmt.format(new Date(s));
function active(m){ return !m.superseded_by && (!m.valid_until || m.valid_until > S.now); }

function entry(m, opts){
  opts = opts || {};
  const live = active(m);
  const node = el("article", {class:"entry k-" + m.kind + (live ? "" : " inactive"), id:"m-" + m.id});
  node.append(el("div", {class:"kind"}, KIND[m.kind] || m.kind));
  node.append(el("div", {class:"text"}, m.content));
  const meta = el("div", {class:"meta"}, el("span", null, m.source), el("span", null, when(m.updated_at)));
  if (m.updated_at !== m.created_at) meta.append(el("span", null, "创建于 " + when(m.created_at)));
  for (const t of m.tags || []) meta.append(el("span", {class:"tag"}, "#" + t));
  if (opts.score) meta.append(el("span", {class:"score"}, opts.score));
  node.append(meta);
  if (m.superseded_by) node.append(el("div", {class:"status"}, "已被替代 " + when(m.valid_until || m.updated_at)));
  else if (!live) node.append(el("div", {class:"status"}, "已失效 " + when(m.valid_until)));

  const links = el("div", {class:"links"});
  if (m.superseded_by) links.append(el("button", {class:"linkbtn", type:"button",
    onclick:() => jump(m.superseded_by)}, "看替代它的那条"));
  for (const p of S.memories.filter(x => x.superseded_by === m.id)) links.append(el("button",
    {class:"linkbtn", type:"button", onclick:() => jump(p.id)}, "看它替代的那条（" + when(p.created_at) + "）"));
  if (m.versions) {
    const box = el("div", {class:"versions", hidden:true});
    const btn = el("button", {class:"linkbtn", type:"button", "aria-expanded":"false", onclick: async () => {
      const open = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", String(!open)); box.hidden = open;
      if (!open && !box.childElementCount){
        box.append(el("p", {class:"note"}, "加载中…"));
        try {
          const rows = await api("/viewer/api/memories/" + m.id + "/history");
          box.replaceChildren(...rows.map(h => el("p", {class:"old"},
            el("span", {class:"when"}, "改写前（" + h.old_source + "，" + when(h.replaced_at) + " 被改）"),
            h.old_content)));
        } catch (e) { box.replaceChildren(el("p", {class:"note error"}, e.message)); }
      }
    }}, "改写过 " + m.versions + " 次");
    links.append(btn); node.append(links, box);
  } else if (links.childElementCount) node.append(links);
  return node;
}

function jump(id){
  if (!document.getElementById("m-" + id)){
    S.history = true; S.kinds.clear(); S.text = ""; S.ranked = null; renderAll();
  }
  const t = document.getElementById("m-" + id);
  if (!t) return;
  t.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
                    block:"center"});
  t.classList.remove("flash"); void t.offsetWidth; t.classList.add("flash");
}

function brief(){
  const live = S.memories.filter(active);
  const of = k => live.filter(m => m.kind === k);
  const sect = (title, rows, empty, cls) => el("section", {class:cls || null}, el("h3", null, title),
    rows.length ? rows.map(m => entry(m)) : el("p", {class:"empty"}, empty));
  const summary = of("session-summary").slice(0, 1);
  return el("div", {class:"brief"},
    sect("现在在哪", of("state"), "还没有记录进展。", "now"),
    sect("卡点", of("blocker"), "没有未解决的卡点。"),
    sect("最近的决策", of("decision").slice(0, 5), "还没有记录决策。"),
    sect("上次会话", summary, "还没有会话小结。"));
}

function listView(){
  const box = el("div", {id:"list"});
  let rows = S.memories.filter(m => S.history || active(m));
  if (S.kinds.size) rows = rows.filter(m => S.kinds.has(m.kind));
  if (S.ranked){
    const pos = new Map(S.ranked.map((r, i) => [r.id, i]));
    rows = rows.filter(m => pos.has(m.id)).sort((a, b) => pos.get(a.id) - pos.get(b.id));
    box.append(el("p", {class:"note"}, "按相关度排序（向量 + 关键词混合检索，和助手调用 search_memory 一样）。",
      el("button", {class:"linkbtn", type:"button", onclick: () => { S.ranked = null; renderList(); }}, "回到时间线")));
    for (const m of rows){
      const r = S.ranked[pos.get(m.id)];
      box.append(entry(m, {score:"语义 " + r.similarity.toFixed(2) + "，关键词 " + r.keyword_score.toFixed(2)}));
    }
    if (!rows.length) box.append(el("p", {class:"empty"}, "没有找到。换个说法，或者打开“含历史”。"));
    return box;
  }
  if (S.text){
    const q = S.text.toLowerCase();
    rows = rows.filter(m => (m.content + " " + (m.tags || []).join(" ")).toLowerCase().includes(q));
  }
  if (!rows.length){
    box.append(el("p", {class:"empty"}, S.text ? "没有包含这段文字的记忆。试试“语义搜索”。" : "这里还没有记忆。"));
    return box;
  }
  let day = "";
  for (const m of rows){
    const d = fmtDay.format(new Date(m.updated_at));
    if (d !== day){ day = d; box.append(el("h4", {class:"day"}, d)); }
    box.append(entry(m));
  }
  return box;
}

function renderList(){ const old = document.getElementById("list"); if (old) old.replaceWith(listView()); }

function renderAll(){
  const p = S.projects.find(x => x.name === S.project) || {name:S.project, description:"", aliases:[]};
  const input = el("input", {type:"search", placeholder:"筛选文字，或输入问题",
    "aria-label":"搜索记忆", value:S.text, oninput: e => { S.text = e.target.value; S.ranked = null; renderList(); },
    onkeydown: e => { if (e.key === "Enter") semantic(); }});
  const searchBtn = el("button", {type:"button", onclick: () => semantic()}, "语义搜索");
  async function semantic(){
    const q = input.value.trim(); if (!q) return;
    searchBtn.disabled = true; searchBtn.textContent = "搜索中…";
    try {
      S.ranked = await api("/viewer/api/search?project=" + encodeURIComponent(S.project) +
                           "&q=" + encodeURIComponent(q) + (S.history ? "&history=1" : ""));
      renderList();
    } catch (e) { alert(e.message); }
    finally { searchBtn.disabled = false; searchBtn.textContent = "语义搜索"; }
  }
  const chips = ORDER.map(k => el("button", {type:"button", class:"chip k-" + k,
    "aria-pressed":String(S.kinds.has(k)), onclick: e => {
      S.kinds.has(k) ? S.kinds.delete(k) : S.kinds.add(k);
      e.currentTarget.setAttribute("aria-pressed", String(S.kinds.has(k))); renderList(); }},
    KIND[k] + " " + S.memories.filter(m => m.kind === k && (S.history || active(m))).length));
  const hist = el("input", {type:"checkbox", id:"hist", checked:S.history, onchange: e => {
    S.history = e.target.checked; S.ranked = null; renderAll(); }});
  const liveCount = S.memories.filter(active).length;
  document.getElementById("main").replaceChildren(
    el("section", {class:"intro"}, el("h2", null, p.name),
      p.description ? el("p", null, p.description) : null,
      p.aliases.length ? el("p", {class:"aliases"}, "也叫 " + p.aliases.join("、")) : null),
    brief(),
    el("section", {class:"all"}, el("h3", null, "全部记忆"),
      el("p", {class:"note"}, liveCount + " 条有效，" + (S.memories.length - liveCount) + " 条已被替代或失效"),
      el("div", {class:"tools"}, input, searchBtn),
      el("div", {class:"filters"}, chips,
        el("label", {class:"toggle", for:"hist"}, hist, "含历史")),
      listView()));
}

async function openProject(name){
  S.project = name; S.kinds.clear(); S.text = ""; S.ranked = null; S.history = false;
  try { localStorage.setItem("sb-project", name); } catch (e) {}
  for (const b of document.querySelectorAll("#projects button"))
    b.setAttribute("aria-pressed", String(b.dataset.name === name));
  document.getElementById("main").replaceChildren(el("p", {class:"loading"}, "加载中…"));
  try {
    const data = await api("/viewer/api/projects/" + encodeURIComponent(name));
    if (S.project !== name) return;
    S.memories = data.memories.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    S.now = data.now; S.byId = new Map(data.memories.map(m => [m.id, m]));
    renderAll();
  } catch (e) {
    document.getElementById("main").replaceChildren(el("p", {class:"error"}, e.message));
  }
}

(async () => {
  try {
    S.projects = await api("/viewer/api/projects");
  } catch (e) {
    document.getElementById("main").replaceChildren(el("p", {class:"error"}, e.message)); return;
  }
  document.getElementById("projects").replaceChildren(...S.projects.map(p => el("button",
    {type:"button", "data-name":p.name, "aria-pressed":"false", onclick: () => openProject(p.name)}, p.name)));
  let start = null;
  try { start = localStorage.getItem("sb-project"); } catch (e) {}
  if (!S.projects.some(p => p.name === start)) start = S.projects.length ? S.projects[0].name : null;
  if (start) openProject(start);
  else document.getElementById("main").replaceChildren(el("p", {class:"empty"},
    "还没有登记任何项目。让助手用 add_project 登记一个。"));
})();
</script>
</body></html>
"""
