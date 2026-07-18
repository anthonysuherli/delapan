-- Reversible KB lifecycle: NULL archived_at = active.
alter table projects add column if not exists archived_at timestamptz;
alter table kbs      add column if not exists archived_at timestamptz;

-- Single-aggregate replacement for the per-KB N+1 in list_projects.
-- PostgREST cannot express this aggregate, so it ships as an RPC — same
-- precedent as match_findings / match_kg_nodes.
create or replace function list_projects_with_activity(
  p_org_id uuid,
  p_include_archived boolean default false
)
returns table (
  project_id uuid,
  project_name text,
  project_archived_at timestamptz,
  kb_id uuid,
  kb_name text,
  kb_archived_at timestamptz,
  finding_count bigint,
  last_finding_at timestamptz
)
language sql
stable
security invoker
set search_path = public
as $$
  select p.id, p.name, p.archived_at,
         k.id, k.name, k.archived_at,
         coalesce(f.n, 0), f.last
    from projects p
    -- The archived-KB filter belongs in the JOIN, not the WHERE. In the WHERE it
    -- would drop the project's last row once all its KBs are archived, making the
    -- project vanish -- where the SQLite tier keeps it with an empty kbs list.
    left join kbs k
      on k.project_id = p.id
     and k.org_id = p.org_id
     and (p_include_archived or k.archived_at is null)
    left join lateral (
      select count(*) as n, max(created_at) as last
        from findings
       where findings.kb_id = k.id
         and findings.invalidated_at is null
    ) f on true
   where p.org_id = p_org_id
     and p.name <> '__journal__'
     and (p_include_archived or p.archived_at is null)
   -- id tiebreakers: created_at alone is not unique when rows are
   -- created in the same transaction, and the order must be stable.
   order by p.created_at, p.id, k.created_at, k.id;
$$;
