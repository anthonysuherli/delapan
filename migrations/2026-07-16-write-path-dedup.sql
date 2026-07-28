-- Write-path dedup: bi-temporal findings + resolution audit log.
-- Apply to the cloud project BEFORE removing the pure-ADD guard in persist.py.
-- Spec: docs/superpowers/specs/2026-07-16-write-path-dedup-design.md (§C6, §Migration)
--
-- Both apply-time TODOs (RLS policies for resolution_events; the amended
-- match_findings RPC) were filled in 2026-07-16 from live dumps against project
-- <project-ref>, immediately before applying this migration to that project.

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
returns uuid language plpgsql security invoker
set search_path = public, extensions
as $$
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

-- RLS policies for resolution_events, mirroring findings' live org-scoped policies
-- (dumped 2026-07-16 from `select policyname, cmd, roles, qual, with_check from
-- pg_policies where tablename = 'findings'`: all four are `roles={public}`,
-- predicate `org_id in (select org_members.org_id from org_members where
-- org_members.user_id = auth.uid())`). resolution_events is audit-only — select
-- and insert are the operations the app performs; no update/delete policy is
-- added since nothing in the codebase updates or deletes an audit row.
create policy "resolution_events_select_own_org" on resolution_events
  for select using (
    org_id in (select org_members.org_id from org_members where org_members.user_id = auth.uid())
  );
create policy "resolution_events_insert_own_org" on resolution_events
  for insert with check (
    org_id in (select org_members.org_id from org_members where org_members.user_id = auth.uid())
  );

-- match_findings RPC, amended with the live-only filter. Body dumped 2026-07-16 via
-- `select pg_get_functiondef(oid) from pg_proc where proname = 'match_findings'`;
-- the only change from the live version is the added `and f.invalidated_at is null`
-- line. SupabaseStore.match_findings needs no client-side change — the exclusion is
-- entirely server-side here.
create or replace function public.match_findings(query_embedding vector, match_kb_id uuid, match_count integer default 10, min_similarity real default 0.0)
 returns table(id uuid, title text, content text, category text, confidence real, tags text[], provenance jsonb, similarity real)
 language sql
 stable
as $function$
  select
    f.id, f.title, f.content, f.category, f.confidence, f.tags, f.provenance,
    1 - (f.embedding <=> query_embedding) as similarity
  from findings f
  where f.kb_id = match_kb_id
    and f.embedding is not null
    and f.status <> 'discarded'
    and f.invalidated_at is null
    and 1 - (f.embedding <=> query_embedding) >= min_similarity
  order by f.embedding <=> query_embedding
  limit match_count;
$function$;
