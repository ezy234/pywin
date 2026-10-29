-- Apply ONLY in the new legacy destination project. The old project is read-only.
create function public.skip_external_portal_swarm(p_run uuid,p_sources jsonb) returns bigint
language plpgsql security invoker set search_path=public,pg_temp as $$
declare r public.portal_swarm_runs%rowtype; changed bigint;
begin
  if jsonb_typeof(p_sources) is distinct from 'array' or jsonb_array_length(p_sources)>500 then
    raise exception 'Invalid source-key batch' using errcode='22023';
  end if;
  select * into r from public.portal_swarm_runs where id=p_run for update;
  if not found then raise exception 'Unknown swarm run' using errcode='22023'; end if;
  if r.status not in ('preparing','running') then return 0; end if;
  update public.portal_swarm_queue q set status='saved',reason='existing_old_source_receipt',
    finished_at=now(),lease_token=null,lease_until=null,finish_token=null,finish_outcome='external_saved',
    finish_result=jsonb_build_object('action','already_saved_old_project')
    from jsonb_to_recordset(p_sources) as s(list_name text,reg_no text)
    where q.run_id=p_run and q.status='pending' and q.list_name=s.list_name and q.reg_no=s.reg_no;
  get diagnostics changed=row_count;
  return changed;
end $$;

create or replace function public.finish_portal_swarm(p_id bigint,p_token uuid,p_outcome text,p_data jsonb default null)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare q public.portal_swarm_queue%rowtype; result jsonb;
begin
  select * into q from public.portal_swarm_queue where id=p_id for update;
  if not found or p_token is null or p_outcome is null then
    raise exception 'Queue item, token and outcome required' using errcode='22023';
  end if;
  if q.finish_token=p_token and q.finish_outcome=p_outcome then return q.finish_result; end if;
  if q.status<>'leased' or q.lease_token is distinct from p_token or q.lease_until<=now() then
    raise exception 'Lease no longer owned' using errcode='22023';
  end if;
  if p_outcome='saved' then
    if jsonb_typeof(p_data) is distinct from 'object'
      or p_data->>'reg_no' is distinct from q.reg_no or p_data->>'list_name' is distinct from q.list_name
      or p_data->>'course' is distinct from q.candidate->>'department'
      or p_data->>'faculty' is distinct from q.candidate->>'faculty' then
      raise exception 'Candidate metadata mismatch' using errcode='22023';
    end if;
    result:=public.save_portal_candidate(p_data,q.candidate->>'academic_session');
  elsif p_outcome='external_saved' then
    -- The trusted worker checked the old receipt. Never insert a candidate or
    -- a fake source receipt here: that data stays exclusively in the old DB.
    result:=jsonb_build_object('action','already_saved_old_project');
  elsif p_outcome in ('closed','retry') then
    result:=jsonb_build_object('outcome',p_outcome);
  else
    raise exception 'Invalid outcome' using errcode='22023';
  end if;
  update public.portal_swarm_queue set
    status=case when p_outcome in ('saved','external_saved') then 'saved' when p_outcome='closed' then 'closed'
      when attempts>=3 then 'failed' else 'pending' end,
    reason=case when p_outcome='external_saved' then 'existing_old_source_receipt'
      when p_outcome='closed' then 'application_closed' when p_outcome='retry' then 'portal_or_parse_failure' else null end,
    available_at=now()+interval '60 seconds',lease_until=null,lease_token=null,
    finish_token=p_token,finish_outcome=p_outcome,finish_result=result,
    finished_at=case when p_outcome<>'retry' or attempts>=3 then now() else null end
    where id=p_id;
  return result;
end $$;
revoke all on function public.skip_external_portal_swarm(uuid,jsonb),public.finish_portal_swarm(bigint,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.skip_external_portal_swarm(uuid,jsonb),public.finish_portal_swarm(bigint,uuid,text,jsonb) to service_role;
notify pgrst,'reload schema';
