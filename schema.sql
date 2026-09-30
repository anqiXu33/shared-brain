-- =====================================================================
-- 共享记忆库：数据库建表脚本（阶段 1 版本）
-- 用法：Supabase 左侧菜单 > SQL Editor > 新建查询 > 整段粘贴 > Run
-- 只需要运行一次。
-- =====================================================================

-- 1. 开启 pgvector 插件，让数据库能存"向量"并按相似度搜索
create extension if not exists vector;

-- 2. 建表：一行 = 一条记忆
create table if not exists memories (
  id              uuid primary key default gen_random_uuid(), -- 自动生成的编号
  content         text not null,                              -- 记忆本身，一句自包含的话
  project         text not null,                              -- 属于哪个项目（必填）
  tags            text[] not null default '{}',               -- 更细的分类，可选
  source          text not null                               -- 谁写的，只允许三个值
                  check (source in ('claude-web', 'chatgpt', 'claude-code')),
  created_at      timestamptz not null default now(),         -- 创建时间
  updated_at      timestamptz not null default now(),         -- 最后修改时间
  embedding       vector(1536),                               -- 向量（text-embedding-3-small 是 1536 维）
  embedding_model text not null                               -- 这条向量是哪个模型算的，以后换模型时用
);

-- 3. 索引：让向量搜索和按项目筛选更快。数据少时感觉不到，建好放着即可。
create index if not exists memories_embedding_idx
  on memories using hnsw (embedding vector_cosine_ops);
create index if not exists memories_project_idx on memories (project);

-- 4. 行级安全（RLS）：打开但不加任何公开规则。
--    效果：拿公开 anon key 的人看不到这张表；
--    只有你的 server（用 service_role key）能读写。
alter table memories enable row level security;

-- 5. 搜索函数：给一个查询向量，返回同一项目内最相似的若干条。
--    server.py 里的 search_memory 和 save_memory 的去重检查都调用它。
--    similarity 为 1 表示完全相同，越小越不相似。
create or replace function match_memories(
  query_embedding vector(1536),
  filter_project  text,
  match_count     int default 5
)
returns table (
  id uuid, content text, project text, tags text[], source text,
  created_at timestamptz, updated_at timestamptz, similarity float
)
language sql stable as $$
  select m.id, m.content, m.project, m.tags, m.source,
         m.created_at, m.updated_at,
         1 - (m.embedding <=> query_embedding) as similarity
  from memories m
  where m.project = filter_project
  order by m.embedding <=> query_embedding
  limit match_count;
$$;
