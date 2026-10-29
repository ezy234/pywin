-- Reconcile durable source receipts in one operation before workers claim work.
create or replace function public.skip_saved_portal_swarm(p_run uuid) returns bigint
language plpgsql security invoker set search_path=public,pg_temp as $$
declare r public.portal_swarm_runs%rowtype; changed bigint;
begin
  select * into r from public.portal_swarm_runs where id=p_run for update;
  if not found then raise exception 'Unknown legacy swarm run' using errcode='22023'; end if;
  if r.status not in ('preparing','running') then return 0; end if;
  update public.portal_swarm_queue q set
    status='saved',reason='existing_source_receipt',finished_at=now(),
    lease_token=null,lease_until=null,finish_token=null,finish_outcome='saved',
    finish_result=jsonb_build_object('action','already_processed','candidate_id',s.candidate_id)
  from public.portal_candidate_sources s
  where q.run_id=p_run and q.status='pending'
    and s.list_name=q.list_name and s.reg_no=q.reg_no;
  get diagnostics changed = row_count;
  return changed;
end $$;
revoke all on function public.skip_saved_portal_swarm(uuid) from public,anon,authenticated;
grant execute on function public.skip_saved_portal_swarm(uuid) to service_role;
notify pgrst,'reload schema';
