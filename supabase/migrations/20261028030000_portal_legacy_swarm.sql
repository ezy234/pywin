-- Older admission list swarm: separate queue with existing candidate storage.
-- Requires the existing portal_candidates/source tables and save_portal_candidate RPC.
create table public.portal_swarm_runs (
  id uuid primary key,
  fingerprint text not null,
  academic_session text not null,
  workers integer not null check(workers between 1 and 20),
  expected_rows integer not null check(expected_rows>0),
  status text not null default 'preparing' check(status in ('preparing','running','paused','complete')),
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  last_terminal_count integer not null default 0,
  stalled_cycles integer not null default 0,
  stop_reason text
);
create unique index portal_swarm_one_active on public.portal_swarm_runs ((true))
  where status in ('preparing','running','paused');
create table public.portal_swarm_queue (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.portal_swarm_runs(id),
  list_name text not null, reg_no text not null,
  priority integer not null, candidate jsonb not null,
  status text not null default 'pending' check(status in ('pending','leased','saved','closed','failed')),
  attempts integer not null default 0 check(attempts between 0 and 3),
  lease_token uuid, lease_until timestamptz,
  available_at timestamptz not null default now(), reason text,
  started_at timestamptz, finished_at timestamptz,
  finish_token uuid, finish_outcome text, finish_result jsonb,
  unique(run_id,list_name,reg_no), unique(run_id,priority)
);
create index portal_swarm_claim_order on public.portal_swarm_queue(run_id,status,available_at,priority);
alter table public.portal_swarm_runs enable row level security;
alter table public.portal_swarm_queue enable row level security;
revoke all on public.portal_swarm_runs,public.portal_swarm_queue from anon,authenticated;
grant all on public.portal_swarm_runs,public.portal_swarm_queue to service_role;
grant usage,select on sequence public.portal_swarm_queue_id_seq to service_role;

create function public.activate_portal_swarm(p_run uuid) returns boolean
language plpgsql security invoker set search_path=public,pg_temp as $$
declare r public.portal_swarm_runs%rowtype; n bigint;
begin
  select * into r from public.portal_swarm_runs where id=p_run for update;
  if not found then raise exception 'Unknown run' using errcode='22023'; end if;
  if r.status<>'preparing' then return r.status='running'; end if;
  select count(*) into n from public.portal_swarm_queue where run_id=p_run;
  if n<>r.expected_rows then raise exception 'Queue seed incomplete' using errcode='22023'; end if;
  update public.portal_swarm_runs set status='running' where id=p_run;
  return true;
end $$;

create function public.claim_portal_swarm(p_run uuid,p_token uuid) returns jsonb
language plpgsql security invoker set search_path=public,pg_temp as $$
declare r public.portal_swarm_runs%rowtype; q public.portal_swarm_queue%rowtype;
begin
  if p_token is null then raise exception 'Lease token required' using errcode='22023'; end if;
  select * into r from public.portal_swarm_runs where id=p_run for update;
  if not found or r.status<>'running' then return null; end if;
  update public.portal_swarm_queue set status='failed',reason='lease_expired_after_three_attempts',finished_at=now()
    where run_id=p_run and status='leased' and lease_until<now() and attempts>=3;
  select * into q from public.portal_swarm_queue where run_id=p_run and attempts<3
    and ((status='pending' and available_at<=now()) or (status='leased' and lease_until<now()))
    order by priority for update skip locked limit 1;
  if not found then return null; end if;
  update public.portal_swarm_queue set status='leased',attempts=attempts+1,
    lease_token=p_token,lease_until=now()+interval '20 minutes',started_at=now()
    where id=q.id returning * into q;
  return to_jsonb(q);
end $$;

create function public.finish_portal_swarm(p_id bigint,p_token uuid,p_outcome text,p_data jsonb default null)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare q public.portal_swarm_queue%rowtype; result jsonb;
begin
  select * into q from public.portal_swarm_queue where id=p_id for update;
  if not found or p_token is null or p_outcome is null then
    raise exception 'Queue item, token and outcome required' using errcode='22023';
  end if;
  -- Retrying a lost HTTP response is safe, even after the row is re-leased.
  if q.finish_token=p_token and q.finish_outcome=p_outcome then return q.finish_result; end if;
  if q.status<>'leased' or q.lease_token is distinct from p_token or q.lease_until<=now() then
    raise exception 'Lease no longer owned' using errcode='22023';
  end if;
  if p_outcome='saved' then
    if jsonb_typeof(p_data) is distinct from 'object'
      or p_data->>'reg_no' is distinct from q.reg_no
      or p_data->>'list_name' is distinct from q.list_name
      or p_data->>'course' is distinct from q.candidate->>'department'
      or p_data->>'faculty' is distinct from q.candidate->>'faculty' then
      raise exception 'Candidate metadata mismatch' using errcode='22023';
    end if;
    result:=public.save_portal_candidate(p_data,q.candidate->>'academic_session');
  elsif p_outcome in ('closed','retry') then
    result:=jsonb_build_object('outcome',p_outcome);
  else
    raise exception 'Invalid outcome' using errcode='22023';
  end if;
  update public.portal_swarm_queue set
    status=case when p_outcome='saved' then 'saved' when p_outcome='closed' then 'closed'
      when attempts>=3 then 'failed' else 'pending' end,
    reason=case when p_outcome='closed' then 'application_closed' when p_outcome='retry' then 'portal_or_parse_failure' else null end,
    available_at=now()+interval '60 seconds',lease_until=null,lease_token=null,
    finish_token=p_token,finish_outcome=p_outcome,finish_result=result,
    finished_at=case when p_outcome<>'retry' or attempts>=3 then now() else null end
    where id=p_id;
  return result;
end $$;

revoke all on function public.activate_portal_swarm(uuid),public.claim_portal_swarm(uuid,uuid),public.finish_portal_swarm(bigint,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.activate_portal_swarm(uuid),public.claim_portal_swarm(uuid,uuid),public.finish_portal_swarm(bigint,uuid,text,jsonb) to service_role;
notify pgrst,'reload schema';
