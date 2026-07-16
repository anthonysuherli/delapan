-- Write-path dedup: bi-temporal findings + resolution audit log.
-- Apply to the cloud project BEFORE removing the pure-ADD guard in persist.py.
-- Spec: docs/superpowers/specs/2026-07-16-write-path-dedup-design.md (§C6, §Migration)
--
-- NOT YET APPLIED. This file was authored offline against the spec + the local
-- SQLiteStore's working implementation of the same bi-temporal shape
-- (delapan/store/sqlite.py migrations 0009/0010) — it has not been run against
-- the live Supabase project. Two sections below (marked TODO) need a live dump
-- before this file is complete; apply everything only after those are filled in
-- and a human has reviewed the whole file.

-- valid_from: add WITHOUT a default first — Postgres backfills a column default
-- into every existing row at ALTER time, which would clobber the created_at seed.
alter table findings add column if not exists valid_from timestamptz;
update findings set valid_from = created_at where valid_from is null;
alter table findings alter column valid_from set default now();

alter table findings
  add column if not exists invalidated_at timestamptz,
  add column if not exists superseded_by uuid references findings(id) on delete set null;
create index if not exists findings_live_idx on findings (kb_id) where invalidated_at is null;

create table if not exists resolution_events (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null,
  kb_id uuid not null,
  op text not null,
  candidate_title text not null,
  target_finding_id uuid,
  new_finding_id uuid,
  details jsonb,
  reason text default '',
  created_at timestamptz default now()
);
alter table resolution_events enable row level security;

-- New RPC: insert the superseding row + retire the target atomically. Client-side
-- enrichment (org_id, status='approved', created_at, valid_from, embedding-as-vector-
-- text) happens in SupabaseStore._row_payload before this is called; this function
-- only does the explicit casts and the two-statement transaction.
create or replace function supersede_finding(p_kb_id uuid, p_target_id uuid, p_row jsonb)
returns uuid language plpgsql security invoker as $$
declare v_new_id uuid;
begin
  insert into findings (id, org_id, kb_id, title, content, category, confidence,
                        tags, provenance, status, created_at, valid_from, embedding)
  values (coalesce((p_row->>'id')::uuid, gen_random_uuid()),
          (p_row->>'org_id')::uuid, (p_row->>'kb_id')::uuid,
          p_row->>'title', p_row->>'content', p_row->>'category',
          (p_row->>'confidence')::real,
          coalesce(p_row->'tags', '[]'::jsonb), coalesce(p_row->'provenance', '[]'::jsonb),
          coalesce(p_row->>'status', 'approved'),
          coalesce((p_row->>'created_at')::timestamptz, now()),
          coalesce((p_row->>'valid_from')::timestamptz, now()),
          (p_row->>'embedding')::vector)
  returning id into v_new_id;

  update findings set invalidated_at = now(), superseded_by = v_new_id
   where id = p_target_id and kb_id = p_kb_id and invalidated_at is null;
  if not found then
    raise exception 'supersede target % not live in kb %', p_target_id, p_kb_id;
  end if;
  return v_new_id;
end $$;

-- =====================================================================================
-- TODO 1 (apply-time, needs a live dump) — RLS policies for resolution_events.
--
-- resolution_events has RLS enabled above but no policies yet, which means it is
-- unreadable/unwritable until policies exist. Before applying this migration, run
-- against the live project:
--
--   select policyname, permissive, roles, cmd, qual, with_check
--   from pg_policies where tablename = 'findings';
--
-- then copy each of findings' org-scoped select/insert policies here, renamed for
-- resolution_events (same org_id-scoping predicate — resolution_events carries its
-- own org_id column). Example shape (fill in the real predicate from the dump):
--
--   create policy "resolution_events_select_own_org" on resolution_events
--     for select using (org_id = <same predicate findings uses>);
--   create policy "resolution_events_insert_own_org" on resolution_events
--     for insert with check (org_id = <same predicate findings uses>);
--
-- =====================================================================================

-- =====================================================================================
-- TODO 2 (apply-time, needs a live dump) — match_findings RPC gains the live-only filter.
--
-- match_findings must stop returning superseded/retired rows. Before applying, run
-- against the live project:
--
--   select pg_get_functiondef(oid) from pg_proc where proname = 'match_findings';
--
-- take the returned body, add `and f.invalidated_at is null` to its WHERE clause
-- (SupabaseStore.match_findings itself needs no client-side change — the exclusion
-- is entirely server-side in this RPC), and paste the resulting full
-- `create or replace function match_findings(...) ...` statement here.
-- =====================================================================================
