-- =====================================================================
-- 迁移 006：项目别名
-- 用法：Supabase > SQL Editor 整段粘贴运行一次。
--
-- 只加不删。旧版 server.py 在迁移后照常能用（它不读 aliases 列）。
--
-- 做了什么：
--   projects 表加 aliases 列（文本数组）：一个项目可以有多个叫法，
--   比如 ai-pendant 的别名 '心力球'、'AI挂件'。save_memory / search_memory /
--   recent_memories 收到别名时，server 会先解析成正式项目名再操作。
--   别名在所有项目之间不能重复，这一点由 server 保证（add_project / add_alias）。
--
-- 回滚：
--   alter table projects drop column if exists aliases;
-- =====================================================================

begin;

alter table projects
  add column if not exists aliases text[] not null default '{}';

-- 已知项目的别名（按需增删）
update projects set aliases = array['心力球', 'AI挂件', '挂件', 'pendant', 'AI wearable']
  where name = 'ai-pendant';
update projects set aliases = array['共享记忆', 'shared memory', '记忆库']
  where name = 'shared-brain';
update projects set aliases = array['SCAI Lab', '实习', 'clinical LLM']
  where name = 'scai';
update projects set aliases = array['作品集', 'anqixu.ch', '个人网站']
  where name = 'portfolio';

commit;
