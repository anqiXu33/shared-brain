-- =====================================================================
-- 迁移 003：检索评测用的只读函数
-- 用法：Supabase > SQL Editor 整段粘贴运行一次。
--
-- 只新建一个函数，不建表、不改表、不改 search_memories，线上搜索不受影响。
-- 函数和 search_memories 一样以调用者身份运行（不是 security definer），
-- 所以只有 server 用的 service_role 能读到数据，anon key 只会拿到空结果。
--
-- eval_rank_memories 返回一个项目里每条仍有效记忆的两个原始分数：
--   similarity     向量余弦相似度，和 search_memories 里的计算一致
--   keyword_score  pg_trgm word_similarity，和 search_memories 里的计算一致
-- eval/run_eval.py 用这两个分数在本地复现三种排序：
--   纯向量、纯关键词（word_similarity >= 0.3）、RRF 混合（各取前 30，k = 60）
-- 并用 search_memories 的实际返回结果核对混合排序复现得对不对。
--
-- 回滚：drop function if exists eval_rank_memories;
-- =====================================================================

create or replace function eval_rank_memories(
  query_embedding vector(1536),
  query_text      text,
  filter_project  text
)
returns table (
  id uuid, updated_at timestamptz, similarity float, keyword_score float
)
language sql stable
set search_path = public, extensions
as $$
  select m.id, m.updated_at,
         (1 - (m.embedding <=> query_embedding))::float,
         word_similarity(query_text, m.content)::float
  from memories m
  where m.project = filter_project
    and m.superseded_by is null
    and (m.valid_until is null or m.valid_until > now());
$$;
