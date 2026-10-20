-- =====================================================================
-- 迁移 004：混合检索的并列排名改为确定性的
-- 用法：Supabase > SQL Editor 整段粘贴运行一次。函数签名不变，server.py 不用改。
--
-- 问题（由 eval/diagnose_repro.py 发现）：短查询的 word_similarity 只有少数几个取值，
-- 经常几十条记忆分数完全相同。row_number() 遇到并列时顺序由数据库随意决定，
-- 于是并列的记忆谁排进关键词前 30、在 RRF 里拿到多少分，每次都可能不同。
--
-- 修法：并列时按向量距离排（关键词一样就让语义更近的在前），最后按 id 兜底。
--   向量列表：距离升序，再 id
--   关键词列表：word_similarity 降序，再距离升序，再 id
--   最终排序：融合分降序，再 updated_at 降序，再 id
-- eval/run_eval.py 用同一套规则复现。
--
-- 回滚：重新运行 migrations/002_kinds_validity_hybrid.sql 第 6 步的 search_memories。
-- =====================================================================

create or replace function search_memories(
  query_embedding  vector(1536),
  query_text       text,
  filter_project   text,
  match_count      int default 5,
  filter_kinds     text[] default null,
  include_inactive boolean default false
)
returns table (
  id uuid, content text, project text, tags text[], source text, kind text,
  superseded_by uuid, valid_until timestamptz,
  created_at timestamptz, updated_at timestamptz,
  similarity float, keyword_score float, score float
)
language sql stable
set search_path = public, extensions
as $$
  with pool as (
    select m.*,
           m.embedding <=> query_embedding         as dist,
           word_similarity(query_text, m.content)  as kws
    from memories m
    where m.project = filter_project
      and (filter_kinds is null or m.kind = any(filter_kinds))
      and (include_inactive
           or (m.superseded_by is null
               and (m.valid_until is null or m.valid_until > now())))
  ),
  vec as (
    select p.id, row_number() over (order by p.dist, p.id) as r
    from pool p
    order by p.dist, p.id
    limit 30
  ),
  kw as (
    select p.id, row_number() over (order by p.kws desc, p.dist, p.id) as r
    from pool p
    where p.kws >= 0.3
    order by p.kws desc, p.dist, p.id
    limit 30
  ),
  fused as (
    select coalesce(v.id, k.id) as fid,
           coalesce(1.0 / (60 + v.r), 0) + coalesce(1.0 / (60 + k.r), 0) as fscore
    from vec v
    full outer join kw k on v.id = k.id
  )
  select p.id, p.content, p.project, p.tags, p.source, p.kind,
         p.superseded_by, p.valid_until, p.created_at, p.updated_at,
         (1 - p.dist)::float,
         p.kws::float,
         f.fscore::float
  from fused f
  join pool p on p.id = f.fid
  order by f.fscore desc, p.updated_at desc, p.id
  limit match_count;
$$;
