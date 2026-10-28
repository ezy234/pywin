create table public.admission_2026_runs (
  id uuid primary key, fingerprint text not null,
  status text not null default 'running' check (status in ('running','paused','complete','deadline')),
  workers integer not null check (workers between 1 and 20),
  created_at timestamptz not null default now(), deadline timestamptz not null default now()+interval '24 hours'
);
create unique index admission_2026_one_running on public.admission_2026_runs ((true)) where status='running';
create table public.admission_2026_queue (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.admission_2026_runs(id),
  reg_no text not null, candidate jsonb not null,
  status text not null default 'pending' check(status in ('pending','leased','saved','closed','failed')),
  attempts integer not null default 0, lease_token uuid, lease_until timestamptz,
  available_at timestamptz not null default now(), reason text,
  started_at timestamptz, finished_at timestamptz,
  unique(run_id,reg_no)
);
create index admission_2026_queue_claim on public.admission_2026_queue(run_id,status,available_at,id);
alter table public.admission_2026_runs enable row level security;
alter table public.admission_2026_queue enable row level security;
revoke all on public.admission_2026_runs,public.admission_2026_queue from anon,authenticated;
grant all on public.admission_2026_runs,public.admission_2026_queue to service_role;
grant usage,select on sequence public.admission_2026_queue_id_seq to service_role;

create function public.claim_admission_2026(p_run uuid,p_token uuid) returns jsonb
language plpgsql security invoker set search_path=public,pg_temp as $$
declare r public.admission_2026_runs%rowtype; q public.admission_2026_queue%rowtype;
begin
  select * into r from public.admission_2026_runs where id=p_run for update;
  if not found or r.status <> 'running' then return null; end if;
  if now()>=r.deadline then
    update public.admission_2026_runs set status='deadline' where id=p_run; return null;
  end if;
  update public.admission_2026_queue set status='failed',reason='lease_expired_after_three_attempts',finished_at=now()
    where run_id=p_run and status='leased' and lease_until<now() and attempts>=3;
  select * into q from public.admission_2026_queue
    where run_id=p_run and attempts<3 and
      ((status='pending' and available_at<=now()) or (status='leased' and lease_until<now()))
    order by id for update skip locked limit 1;
  if not found then return null; end if;
  update public.admission_2026_queue set status='leased',attempts=attempts+1,
    lease_token=p_token,lease_until=now()+interval '20 minutes',started_at=now()
    where id=q.id returning * into q;
  return to_jsonb(q);
end $$;

create function public.finish_admission_2026(p_id bigint,p_token uuid,p_outcome text,p_data jsonb default null)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare q public.admission_2026_queue%rowtype; result jsonb;
begin
  select * into q from public.admission_2026_queue where id=p_id for update;
  if not found or q.status<>'leased' or q.lease_token is distinct from p_token or q.lease_until<now() then
    raise exception 'Lease no longer owned' using errcode='22023';
  end if;
  if p_outcome='saved' then
    if p_data->>'reg_no' is distinct from q.reg_no or p_data->>'list_name' is distinct from q.candidate->>'list_name'
      or p_data->>'course' is distinct from q.candidate->>'department'
      or p_data->>'faculty' is distinct from q.candidate->>'faculty' then
      raise exception 'Candidate metadata mismatch' using errcode='22023';
    end if;
    result:=public.save_admission_2026_candidate(p_data,q.candidate->>'academic_session');
  elsif p_outcome not in ('closed','retry') then
    raise exception 'Invalid outcome' using errcode='22023';
  end if;
  update public.admission_2026_queue set
    status=case when p_outcome='saved' then 'saved' when p_outcome='closed' then 'closed'
      when attempts>=3 then 'failed' else 'pending' end,
    reason=case when p_outcome='closed' then 'application_closed' when p_outcome='retry' then 'portal_or_parse_failure' else null end,
    available_at=now()+interval '60 seconds',lease_until=null,lease_token=null,
    finished_at=case when p_outcome<>'retry' or attempts>=3 then now() else null end
    where id=p_id;
  return coalesce(result,jsonb_build_object('outcome',p_outcome));
end $$;
revoke all on function public.claim_admission_2026(uuid,uuid),public.finish_admission_2026(bigint,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.claim_admission_2026(uuid,uuid),public.finish_admission_2026(bigint,uuid,text,jsonb) to service_role;
notify pgrst,'reload schema';
