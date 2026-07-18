-- Solo project tracker: markdown-synced initiatives + backlog.
-- Spec: docs/superpowers/specs/2026-07-17-solo-project-tracker-design.md (§C3)
-- Writes: service role only (bypasses RLS). Reads: authenticated SELECT.

create table if not exists tracking_initiatives (
  slug text primary key,
  title text not null,
  status text not null check (status in ('proposed','active','blocked','paused','done','dropped')),
  repo text not null check (repo in ('backend','frontend','both')),
  blocked_by text[] not null default '{}',
  spec text,
  plan text,
  branch text,
  body_md text not null default '',
  updated date not null,
  synced_at timestamptz not null default now()
);

create table if not exists tracking_backlog (
  position int primary key,
  text text not null,
  repo text not null check (repo in ('backend','frontend','both')),
  initiative_slug text,
  synced_at timestamptz not null default now()
);

alter table tracking_initiatives enable row level security;
alter table tracking_backlog enable row level security;

-- Drop-if-exists so re-apply is idempotent in local stacks.
drop policy if exists tracking_initiatives_select_authenticated on tracking_initiatives;
create policy tracking_initiatives_select_authenticated
  on tracking_initiatives for select
  to authenticated
  using (true);

drop policy if exists tracking_backlog_select_authenticated on tracking_backlog;
create policy tracking_backlog_select_authenticated
  on tracking_backlog for select
  to authenticated
  using (true);

-- Intentionally no insert/update/delete policies for authenticated/anon.
