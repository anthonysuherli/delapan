-- 2026-07-20-rls-with-check-remaining.sql — second RLS sweep.
-- Applied to the cloud project 2026-07-20 and verified (0 UPDATE policies
-- without WITH CHECK, 0 tables with RLS disabled, public schema wide).
--
-- Widening scripts/rls_audit.py's TENANT_TABLES from 11 to 29 tables surfaced
-- six more UPDATE policies with the same tenant-reassignment hole the first
-- sweep closed: USING scopes the rows you may target, but without WITH CHECK
-- the POST-update row is unvalidated, so a member could reassign org_id and
-- push the row into another tenant. Idempotent: drop-then-create.
--
-- Known remaining (not fixed here, deliberately): public.access_requests has
-- RLS enabled with zero policies — fail-closed for the authenticated role
-- (deny-all), reachable only via the service role. Safe by default; revisit if
-- the table is ever read from a user-scoped client.

drop policy if exists api_keys_update on public.api_keys;
create policy api_keys_update on public.api_keys for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists chat_messages_update on public.chat_messages;
create policy chat_messages_update on public.chat_messages for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists chat_threads_update on public.chat_threads;
create policy chat_threads_update on public.chat_threads for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists kb_synopsis_update on public.kb_synopsis;
create policy kb_synopsis_update on public.kb_synopsis for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

drop policy if exists uploads_update on public.uploads;
create policy uploads_update on public.uploads for update to authenticated
  using (org_id in (select org_id from org_members where user_id = auth.uid()))
  with check (org_id in (select org_id from org_members where user_id = auth.uid()));

-- orgs keeps its owner/admin role gate on both sides of the policy.
drop policy if exists org_update on public.orgs;
create policy org_update on public.orgs for update to authenticated
  using (id in (select org_id from org_members
                where user_id = auth.uid() and role = any (array['owner','admin'])))
  with check (id in (select org_id from org_members
                     where user_id = auth.uid() and role = any (array['owner','admin'])));
