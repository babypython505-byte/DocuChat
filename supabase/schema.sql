-- DocuChat per-user data. Run once in the Supabase SQL editor (safe to re-run).
-- The browser reads and writes these tables directly with the signed-in user's session;
-- Row Level Security makes every row visible only to its owner (auth.uid() = user_id).

create table if not exists public.exam_results (
  id          text        not null,                       -- the attempt id generated in the browser
  user_id     uuid        not null default auth.uid() references auth.users (id) on delete cascade,
  created_at  timestamptz not null default now(),
  score       numeric     not null default 0,
  total       numeric     not null default 0,
  style       text,
  meta        jsonb       not null default '{}'::jsonb,   -- summary: filename, pct, qtype, weak topics, full attempt (when small)
  primary key (user_id, id)
);
create index if not exists exam_results_user_created on public.exam_results (user_id, created_at desc);

create table if not exists public.saved_chats (
  id          text        not null,                       -- the conversation id generated in the browser
  user_id     uuid        not null default auth.uid() references auth.users (id) on delete cascade,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  title       text        not null default 'New chat',
  messages    jsonb       not null default '[]'::jsonb,
  primary key (user_id, id)
);
create index if not exists saved_chats_user_updated on public.saved_chats (user_id, updated_at desc);

alter table public.exam_results enable row level security;
alter table public.saved_chats  enable row level security;

drop policy if exists "exam_results: owner select" on public.exam_results;
drop policy if exists "exam_results: owner insert" on public.exam_results;
drop policy if exists "exam_results: owner update" on public.exam_results;
drop policy if exists "exam_results: owner delete" on public.exam_results;
create policy "exam_results: owner select" on public.exam_results for select to authenticated using (auth.uid() = user_id);
create policy "exam_results: owner insert" on public.exam_results for insert to authenticated with check (auth.uid() = user_id);
create policy "exam_results: owner update" on public.exam_results for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "exam_results: owner delete" on public.exam_results for delete to authenticated using (auth.uid() = user_id);

drop policy if exists "saved_chats: owner select" on public.saved_chats;
drop policy if exists "saved_chats: owner insert" on public.saved_chats;
drop policy if exists "saved_chats: owner update" on public.saved_chats;
drop policy if exists "saved_chats: owner delete" on public.saved_chats;
create policy "saved_chats: owner select" on public.saved_chats for select to authenticated using (auth.uid() = user_id);
create policy "saved_chats: owner insert" on public.saved_chats for insert to authenticated with check (auth.uid() = user_id);
create policy "saved_chats: owner update" on public.saved_chats for update to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "saved_chats: owner delete" on public.saved_chats for delete to authenticated using (auth.uid() = user_id);

grant select, insert, update, delete on public.exam_results to authenticated;
grant select, insert, update, delete on public.saved_chats  to authenticated;
revoke all on public.exam_results from anon;
revoke all on public.saved_chats  from anon;
