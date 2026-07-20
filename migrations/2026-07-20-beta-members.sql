-- 2026-07-20-beta-members.sql — invite allowlist for the hosted beta.
-- Grant = one INSERT (service role). Users can read their own row (the SPA
-- waitlist-gate UX); there are deliberately NO insert/update/delete policies.
create table if not exists public.beta_members (
  user_id uuid primary key references auth.users (id) on delete cascade,
  granted_at timestamptz not null default now(),
  note text
);

alter table public.beta_members enable row level security;

drop policy if exists "beta members read own row" on public.beta_members;
create policy "beta members read own row"
  on public.beta_members for select to authenticated
  using ((select auth.uid()) = user_id);
