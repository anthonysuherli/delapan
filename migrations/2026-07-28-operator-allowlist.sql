-- 2026-07-28-operator-allowlist.sql — replace the email-literal operator check
-- on tracking_* with a table-driven allowlist.
--
-- 2026-07-20-rls-audit-gaps.sql gated tracking_* reads by comparing auth.uid()
-- to a hardcoded address inside the policy body. That embeds an operator's
-- email in a public migration and names the privileged account outright.
-- public.operators holds the allowlist instead; grant = one INSERT by the
-- service role, out of band and never committed:
--
--   insert into public.operators (user_id, note)
--   select id, 'founder' from auth.users where email = '<your-address>'
--   on conflict do nothing;
--
-- Idempotent: drop-then-create so re-apply is safe.
create table if not exists public.operators (
  user_id uuid primary key references auth.users (id) on delete cascade,
  granted_at timestamptz not null default now(),
  note text
);

alter table public.operators enable row level security;

-- Operators read their own row (so the EXISTS below resolves for them); there
-- are deliberately NO insert/update/delete policies — only the service role
-- can extend the allowlist.
drop policy if exists "operators read own row" on public.operators;
create policy "operators read own row"
  on public.operators for select to authenticated
  using ((select auth.uid()) = user_id);

-- ── tracking_*: operator-only reads (was: an email literal in the policy) ────
drop policy if exists tracking_initiatives_select_owner on public.tracking_initiatives;
drop policy if exists tracking_initiatives_select_operator on public.tracking_initiatives;
create policy tracking_initiatives_select_operator on public.tracking_initiatives
  for select to authenticated
  using (exists (select 1 from public.operators where user_id = (select auth.uid())));

drop policy if exists tracking_backlog_select_owner on public.tracking_backlog;
drop policy if exists tracking_backlog_select_operator on public.tracking_backlog;
create policy tracking_backlog_select_operator on public.tracking_backlog
  for select to authenticated
  using (exists (select 1 from public.operators where user_id = (select auth.uid())));
