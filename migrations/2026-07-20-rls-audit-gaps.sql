-- 2026-07-20-rls-audit-gaps.sql — close the gaps scripts/rls_audit.py found on
-- the cloud project (2026-07-20 run: 8 gaps + 1 blind spot).
-- Idempotent: drop-then-create so re-apply is safe.
--
-- 1. UPDATE policies had no WITH CHECK on six tenant tables: a member could
--    update a row they can see and reassign its org_id, pushing data into
--    another tenant. WITH CHECK re-validates the post-update row.
-- 2. kg_communities had RLS disabled entirely (0 rows, never gated).
-- 3. tracking_* were readable by ANY authenticated user (qual = true) — fine
--    while the tracker was single-account, a leak the moment public sign-up
--    opens. These tables carry no org_id/user_id (service-role sync keyed by
--    slug), so ownership is expressed against auth.users by email.

-- ── 1. UPDATE policies: add WITH CHECK ───────────────────────────────────────
drop policy if exists findings_update on public.findings;
create policy findings_update on public.findings for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists kbs_update on public.kbs;
create policy kbs_update on public.kbs for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists projects_update on public.projects;
create policy projects_update on public.projects for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists kg_nodes_update on public.kg_nodes;
create policy kg_nodes_update on public.kg_nodes for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists kg_edges_update on public.kg_edges;
create policy kg_edges_update on public.kg_edges for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists explorations_update on public.explorations;
create policy explorations_update on public.explorations for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

-- ── 2. kg_communities: enable RLS + org-scoped policies ──────────────────────
alter table public.kg_communities enable row level security;

drop policy if exists kg_communities_select on public.kg_communities;
create policy kg_communities_select on public.kg_communities for select to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists kg_communities_insert on public.kg_communities;
create policy kg_communities_insert on public.kg_communities for insert to authenticated
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists kg_communities_update on public.kg_communities;
create policy kg_communities_update on public.kg_communities for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists kg_communities_delete on public.kg_communities;
create policy kg_communities_delete on public.kg_communities for delete to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()));

-- ── 3. tracking_*: owner-only reads (was: any authenticated user) ────────────
-- Revoke the blanket authenticated read here; the owner-side grant moved to
-- 2026-07-28-operator-allowlist.sql, which gates these tables on a
-- public.operators row instead of an email literal written into the policy.
drop policy if exists tracking_initiatives_select_authenticated on public.tracking_initiatives;
drop policy if exists tracking_backlog_select_authenticated on public.tracking_backlog;
