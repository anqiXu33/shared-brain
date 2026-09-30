-- =====================================================================
-- 迁移 001：记忆修改历史
-- 效果：每次 update_memory 覆盖内容之前，数据库自动把旧版本存进 memory_history。
-- 用法：Supabase > SQL Editor 粘贴运行一次。server.py 无需改动。
-- =====================================================================

-- 1. 历史表：一行 = 某条记忆被覆盖前的一个旧版本
create table if not exists memory_history (
  id           uuid primary key default gen_random_uuid(),
  memory_id    uuid not null references memories(id) on delete cascade,
  old_content  text not null,
  old_source   text not null,
  replaced_at  timestamptz not null default now()   -- 旧版本被替换的时间
);

create index if not exists memory_history_memory_idx on memory_history (memory_id);

alter table memory_history enable row level security;

-- 2. 触发器函数：只有 content 真的变了才存档
create or replace function archive_memory_version()
returns trigger language plpgsql as $$
begin
  if new.content is distinct from old.content then
    insert into memory_history (memory_id, old_content, old_source)
    values (old.id, old.content, old.source);
  end if;
  return new;
end;
$$;

-- 3. 挂到 memories 表上：每次 update 之前先跑上面的函数
drop trigger if exists memories_archive_before_update on memories;
create trigger memories_archive_before_update
  before update on memories
  for each row execute function archive_memory_version();
