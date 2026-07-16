-- E4 curation flywheel. Apply to the cloud instance before enabling on cloud.
-- Verified against the live schema: access_events is (id bigint, org_id uuid,
-- kb_id uuid, target_type text NOT NULL, target_id uuid, surface text NOT NULL,
-- api_key_id uuid, query_text text, ts timestamptz NOT NULL default now()).

-- 1. access_events: verdict columns (additive; rollup_access_events selects
--    explicit columns and is unaffected).
alter table access_events
  add column if not exists coverage text,
  add column if not exists band_counts jsonb;
create index if not exists idx_access_events_kb_ts on access_events (kb_id, ts);

-- 2. access_events RLS is SELECT-only today — writes and prunes are silently
--    rejected. Mirror the findings table's membership-based policies.
create policy access_events_insert on access_events for insert
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));
create policy access_events_delete on access_events for delete
  using (org_id in (select org_id from org_members where user_id = auth.uid()));

-- 3. curation_topics.
create table if not exists curation_topics (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  kb_id uuid not null references kbs(id),
  query_text text not null,
  query_norm text not null,
  embedding vector(1536),
  coverage text not null,
  recurrence int not null default 1,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  consumed_at timestamptz,
  resolved_at timestamptz);
create unique index if not exists uq_curation_topics_norm on curation_topics (kb_id, query_norm);
create index if not exists idx_curation_topics_open on curation_topics (kb_id)
  where consumed_at is null and resolved_at is null;

alter table curation_topics enable row level security;
create policy curation_topics_org on curation_topics for all
  using      (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

-- 4. RPCs. Param naming follows the deployed convention (match_findings uses
--    query_embedding/match_kb_id/match_count/min_similarity — not p_-prefixed).
create or replace function match_curation_topics(
  query_embedding vector(1536), match_kb_id uuid, match_count int, min_similarity real)
returns table (id uuid, query_text text, query_norm text, coverage text, recurrence int,
               first_seen timestamptz, last_seen timestamptz,
               consumed_at timestamptz, resolved_at timestamptz, similarity real)
language sql stable as $$
  select t.id, t.query_text, t.query_norm, t.coverage, t.recurrence, t.first_seen,
         t.last_seen, t.consumed_at, t.resolved_at,
         (1 - (t.embedding <=> query_embedding))::real
  from curation_topics t
  where t.kb_id = match_kb_id
    and t.embedding is not null
    and (1 - (t.embedding <=> query_embedding)) >= min_similarity
  order by t.embedding <=> query_embedding
  limit match_count;
$$;

-- Atomic insert-or-increment: PostgREST cannot express `recurrence = recurrence + 1`,
-- so the upsert must be an RPC. SECURITY INVOKER (default) => runs under the
-- caller's JWT => the policies above apply.
create or replace function upsert_curation_topic(
  p_org_id uuid, p_kb_id uuid, p_query_text text, p_query_norm text,
  p_embedding vector(1536), p_coverage text, p_seen timestamptz)
returns uuid language plpgsql as $$
declare v_id uuid;
begin
  insert into curation_topics (org_id, kb_id, query_text, query_norm, embedding,
                               coverage, first_seen, last_seen)
  values (p_org_id, p_kb_id, p_query_text, p_query_norm, p_embedding, p_coverage,
          p_seen, p_seen)
  on conflict (kb_id, query_norm) do update
    set recurrence  = curation_topics.recurrence + 1,
        last_seen   = excluded.last_seen,
        coverage    = excluded.coverage,
        consumed_at = null,
        resolved_at = null
  returning id into v_id;
  return v_id;
end; $$;

-- Atomic increment on the vector-hit path (kills the read-modify-write lost update).
create or replace function bump_curation_topic(
  p_topic_id uuid, p_coverage text, p_seen timestamptz)
returns void language sql as $$
  update curation_topics
     set recurrence = recurrence + 1, last_seen = p_seen, coverage = p_coverage,
         consumed_at = null, resolved_at = null
   where id = p_topic_id;
$$;
