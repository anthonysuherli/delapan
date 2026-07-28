-- kg_schemas: grant INSERT to authenticated org members.
-- Apply to the cloud project (<project-ref>) so the KG-intent write seam
-- (SupabaseStore.set_kg_intent, reached via delapan_set_kg_schema and br8n's
-- identical br8n_set_kg_schema) can persist a schema under a user JWT.
--
-- Why this is needed: kg_schemas has RLS enabled and a SELECT policy (reads
-- always worked), but no INSERT policy for user-scoped (authenticated) clients —
-- nothing cloud-side wrote a schema before this seam existed. A user-scoped
-- set_kg_intent therefore failed with postgrest 42501
-- ("new row violates row-level security policy for table \"kg_schemas\"").
--
-- Pattern mirrors the other org-scoped writable tables (findings, kg_nodes,
-- kg_edges, resolution_events): a WITH CHECK that constrains the row's org_id to
-- the caller's own org_members membership. Those policies are roles={public}
-- with predicate `org_id in (select org_members.org_id from org_members where
-- org_members.user_id = auth.uid())` (confirmed live from pg_policies before
-- mirroring). The load-bearing invariant: a row is insertable only when its
-- org_id belongs to the authenticated caller. set_kg_intent already stamps
-- org_id on the row.
--
-- INSERT only: kg_schemas is append-only (one row per version, newest = active);
-- set_kg_intent never updates or deletes, so no update/delete policy is added.

-- Idempotent: enable RLS + drop-then-create so re-apply is safe.
alter table kg_schemas enable row level security;

drop policy if exists "kg_schemas_insert_own_org" on kg_schemas;
create policy "kg_schemas_insert_own_org" on kg_schemas
  for insert with check (
    org_id in (select org_members.org_id from org_members where org_members.user_id = auth.uid())
  );
