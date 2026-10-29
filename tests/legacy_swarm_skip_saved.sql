-- Regression against the active queue, with all queue changes rolled back.
begin;
set local role service_role;
do $$
declare r uuid; eligible bigint; changed bigint; before_leases jsonb; after_leases jsonb; remaining bigint;
begin
  select id into r from public.portal_swarm_runs where status='running' for update;
  if r is null then raise exception 'No running legacy swarm available for reconciliation regression'; end if;
  perform 1 from public.portal_swarm_queue where run_id=r and status='leased' for update;
  select count(*) into eligible from public.portal_swarm_queue q join public.portal_candidate_sources s
    on s.list_name=q.list_name and s.reg_no=q.reg_no where q.run_id=r and q.status='pending';
  select jsonb_agg(jsonb_build_array(id,lease_token,lease_until) order by id) into before_leases
    from public.portal_swarm_queue where run_id=r and status='leased';
  changed:=public.skip_saved_portal_swarm(r);
  if changed<>eligible then raise exception 'Reconciliation count mismatch'; end if;
  select count(*) into remaining from public.portal_swarm_queue q join public.portal_candidate_sources s
    on s.list_name=q.list_name and s.reg_no=q.reg_no where q.run_id=r and q.status='pending';
  if remaining<>0 then raise exception 'Saved pending records remain'; end if;
  select jsonb_agg(jsonb_build_array(id,lease_token,lease_until) order by id) into after_leases
    from public.portal_swarm_queue where run_id=r and status='leased';
  if after_leases is distinct from before_leases then raise exception 'Active leases were changed'; end if;
  if public.skip_saved_portal_swarm(r)<>0 then raise exception 'Reconciliation is not idempotent'; end if;
  if exists(select 1 from public.portal_swarm_queue q where q.run_id=r and q.reason='existing_source_receipt'
    and not exists(select 1 from public.portal_candidate_sources s where s.list_name=q.list_name and s.reg_no=q.reg_no)) then
    raise exception 'Queue marked saved without a matching source receipt';
  end if;
  if has_function_privilege('anon','public.skip_saved_portal_swarm(uuid)','execute') then raise exception 'Anon can reconcile'; end if;
end $$;
rollback;
