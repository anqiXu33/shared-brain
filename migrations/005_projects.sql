-- =====================================================================
-- 迁移 005：项目注册表（取代 ALLOWED_PROJECTS 环境变量）
-- 用法：Supabase > SQL Editor 整段粘贴运行一次。整段在一个事务里，
--       中途出错会整体回滚。
--
-- 只加不删：不删除、不修改任何记忆。旧版 server.py 在迁移后照常能用
-- （它不读 projects 表，仍按 ALLOWED_PROJECTS 校验）。
--
-- 做了什么：
--   1. 新建 projects 表：一行 = 一个项目（名字 + 一句说明）
--   2. 用 memories 里已经出现过的项目名回填，所以现有记忆一条不落
--   3. memories.project 加外键指向 projects.name：
--      数据库层面保证不会出现没登记的项目名（比如 'wikilens' 这种拼写）。
--      on update cascade：以后改项目名时，记忆会跟着改。
--
-- 之后：新项目通过 MCP 工具 add_project 登记，不用再改 Render 环境变量。
--
-- 回滚：
--   alter table memories drop constraint if exists memories_project_fkey;
--   drop table if exists projects;
-- =====================================================================

begin;

-- 1. 项目表
create table if not exists projects (
  name        text primary key,
  description text not null default '',          -- 一句话说明这个项目是什么，助手判断该存哪里时用
  created_by  text not null default 'migration', -- 谁登记的：claude-web / chatgpt / claude-code / migration
  created_at  timestamptz not null default now(),
  -- 小写字母、数字和连字符，2 到 40 个字符，以字母或数字开头
  constraint projects_name_format check (name ~ '^[a-z0-9][a-z0-9-]{1,39}$')
);

alter table projects enable row level security;

-- 2. 回填：已有记忆里出现过的每个项目名
insert into projects (name)
select distinct project from memories
on conflict (name) do nothing;

-- 3. 外键
alter table memories drop constraint if exists memories_project_fkey;
alter table memories add constraint memories_project_fkey
  foreign key (project) references projects (name) on update cascade;

commit;

-- 运行后可以给回填的项目补上说明，例如：
--   update projects set description = '...' where name = 'scai';
