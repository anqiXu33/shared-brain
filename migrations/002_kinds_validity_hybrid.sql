-- =====================================================================
-- 迁移 002：记忆类型 + 时间有效性 + 混合检索
-- 用法：Supabase > SQL Editor 整段粘贴运行一次。整段在一个事务里，
--       中途任何一步出错都会整体回滚，数据库保持运行前的样子。
--
-- 只加不删：不删除任何行、不改任何已有内容，旧版 server.py 在迁移后照常能用。
--
-- 做了什么：
--   0. 备份 memories 和 memory_history 到两张新表（*_backup_002）
--   1. 放开 source 的三值限制，以后接新客户端不用改数据库
--   2. 新增 kind 列（记忆类型），按已有 tags 自动回填
--   3. 新增 superseded_by / valid_until（被谁替代、何时失效）
--   4. 开启 pg_trgm，给 content 建三元组索引（关键词匹配，专有名词更准）
--   5. match_memories 只返回仍有效的记忆（去重检查用）
--   6. 新函数 search_memories：向量 + 关键词，用 RRF 融合排序
--
-- 回滚（如果需要，在确认新版 server.py 已撤回之后）：
--   drop function if exists search_memories;
--   alter table memories drop column if exists kind,
--                        drop column if exists superseded_by,
--                        drop column if exists valid_until;
--   然后重新运行 schema.sql 第 5 步恢复旧的 match_memories。
--   备份表在确认一切正常后可以手动 drop。
-- =====================================================================

begin;

-- 0. 备份（整表复制，含向量）。RLS 打开，和主表一样对外不可见。
create table if not exists memories_backup_002 as table memories;
create table if not exists memory_history_backup_002 as table memory_history;
alter table memories_backup_002 enable row level security;
alter table memory_history_backup_002 enable row level security;

-- 1. 去掉 source 只能是三个值的限制，改为"不能为空"
do $$
declare c text;
begin
  for c in
    select conname from pg_constraint
    where conrelid = 'memories'::regclass and contype = 'c'
      and pg_get_constraintdef(oid) like '%claude-web%'
  loop
    execute format('alter table memories drop constraint %I', c);
  end loop;
end $$;
alter table memories drop constraint if exists memories_source_nonempty;
alter table memories add constraint memories_source_nonempty
  check (length(btrim(source)) > 0);

-- 2. 记忆类型
--    state           当前状态（"X 进行到哪"），新状态出现时替代旧的
--    decision        决策及原因
--    blocker         卡点 / 待解决的问题，解决后标记失效
--    context         背景事实（默认值）
--    log             一次性的进展记录
--    session-summary 一次工作会话的小结
alter table memories add column if not exists kind text not null default 'context';
alter table memories drop constraint if exists memories_kind_check;
alter table memories add constraint memories_kind_check
  check (kind in ('state', 'decision', 'blocker', 'context', 'log', 'session-summary'));

-- 按已有 tags 回填（只改 kind，不动 content，不会触发历史存档）
update memories set kind = case
    when 'blocker'  = any(tags) then 'blocker'
    when 'decision' = any(tags) then 'decision'
    when 'status'   = any(tags) then 'state'
    else kind
  end
where kind = 'context';

-- 3. 时间有效性：旧记录不覆盖，只标记"被谁替代 / 何时失效"
alter table memories add column if not exists superseded_by uuid
  references memories(id) on delete set null;
alter table memories add column if not exists valid_until timestamptz;
create index if not exists memories_kind_idx on memories (project, kind);

-- 4. 关键词匹配
create extension if not exists pg_trgm;
create index if not exists memories_content_trgm_idx
  on memories using gin (content gin_trgm_ops);

-- 5. 去重检查只和仍有效的记忆比（签名不变，旧 server.py 照常调用）
create or replace function match_memories(
  query_embedding vector(1536),
  filter_project  text,
  match_count     int default 5
)
returns table (
  id uuid, content text, project text, tags text[], source text,
  created_at timestamptz, updated_at timestamptz, similarity float
)
language sql stable
set search_path = public, extensions
as $$
  select m.id, m.content, m.project, m.tags, m.source,
         m.created_at, m.updated_at,
         1 - (m.embedding <=> query_embedding) as similarity
  from memories m
  where m.project = filter_project
    and m.superseded_by is null
    and (m.valid_until is null or m.valid_until > now())
  order by m.embedding <=> query_embedding
  limit match_count;
$$;

-- 6. 混合检索：向量前 30 + 关键词前 30，用 RRF（1/(60+名次)）相加排序。
--    similarity     向量余弦相似度（0~1）
--    keyword_score  pg_trgm word_similarity（0~1），查询词在内容里出现得越完整越高
--    score          融合分，只用于排序
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
    select m.*
    from memories m
    where m.project = filter_project
      and (filter_kinds is null or m.kind = any(filter_kinds))
      and (include_inactive
           or (m.superseded_by is null
               and (m.valid_until is null or m.valid_until > now())))
  ),
  vec as (
    select p.id,
           row_number() over (order by p.embedding <=> query_embedding) as r
    from pool p
    order by p.embedding <=> query_embedding
    limit 30
  ),
  kw as (
    select p.id,
           row_number() over (order by word_similarity(query_text, p.content) desc) as r
    from pool p
    where word_similarity(query_text, p.content) >= 0.3
    order by word_similarity(query_text, p.content) desc
    limit 30
  ),
  fused as (
    select coalesce(v.id, k.id) as fid,
           coalesce(1.0 / (60 + v.r), 0) + coalesce(1.0 / (60 + k.r), 0) as fscore
    from vec v
    full outer join kw k on v.id = k.id
  )
  select m.id, m.content, m.project, m.tags, m.source, m.kind,
         m.superseded_by, m.valid_until, m.created_at, m.updated_at,
         (1 - (m.embedding <=> query_embedding))::float,
         word_similarity(query_text, m.content)::float,
         f.fscore::float
  from fused f
  join memories m on m.id = f.fid
  order by f.fscore desc, m.updated_at desc
  limit match_count;
$$;

commit;

-- 跑完后可以用这两句检查：
--   select kind, count(*) from memories group by kind;
--   select (select count(*) from memories) as now, (select count(*) from memories_backup_002) as backup;
