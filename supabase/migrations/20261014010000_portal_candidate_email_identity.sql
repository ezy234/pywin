-- One person per nonblank, case-insensitive email; receipts remain per list/JAMB.
lock table public.portal_candidates in access exclusive mode;

alter table public.portal_candidates
  add column if not exists email_normalized text generated always as (nullif(lower(btrim(email)), '')) stored,
  add column if not exists academic_session text,
  add column if not exists department_source_list text,
  add column if not exists department_source_reg_no text,
  add column if not exists updated_at timestamptz not null default now();

-- Infer legacy sessions only from explicit session labels, never insertion dates.
with sessions as (
  select id, regexp_match(list_name, '^(20[0-9]{2})[-/.](20[0-9]{2})') as years
  from public.portal_candidates where academic_session is null
)
update public.portal_candidates c set
  academic_session = s.years[1] || '-' || s.years[2],
  department_source_list = c.list_name, department_source_reg_no = c.reg_no
from sessions s where s.id = c.id and s.years is not null
  and s.years[2]::integer = s.years[1]::integer + 1;

alter table public.portal_candidates drop constraint if exists portal_candidates_session_valid;
alter table public.portal_candidates add constraint portal_candidates_session_valid check (
  case when academic_session is null then true
    when academic_session ~ '^20[0-9]{2}-20[0-9]{2}$'
      then right(academic_session, 4)::integer = left(academic_session, 4)::integer + 1
    else false end
);
-- NULL emails do not merge unrelated people. Existing duplicates fail safely.
create unique index if not exists portal_candidates_email_unique
  on public.portal_candidates (email_normalized);

create table if not exists public.portal_candidate_sources (
  list_name text not null,
  reg_no text not null,
  candidate_id bigint not null references public.portal_candidates(id) on delete cascade,
  academic_session text,
  processed_at timestamptz not null default now(),
  primary key (list_name, reg_no)
);
create index if not exists portal_candidate_sources_candidate_idx
  on public.portal_candidate_sources (candidate_id);
alter table public.portal_candidate_sources enable row level security;
revoke all on public.portal_candidate_sources from anon, authenticated;
grant all on public.portal_candidate_sources to service_role;

insert into public.portal_candidate_sources (list_name, reg_no, candidate_id, academic_session, processed_at)
select list_name, reg_no, id, academic_session, created_at from public.portal_candidates
on conflict (list_name, reg_no) do nothing;

create or replace function public.save_portal_candidate(p_candidate jsonb, p_academic_session text)
returns jsonb language plpgsql security invoker set search_path = public, pg_temp
as $$
declare
  v_email text;
  v_reg text;
  v_list text;
  v_existing public.portal_candidates%rowtype;
  v_id bigint;
  v_action text;
  v_session text;
begin
  if jsonb_typeof(p_candidate) is distinct from 'object' then
    raise exception 'Candidate must be a JSON object' using errcode = '22023';
  end if;
  if p_academic_session is null or p_academic_session !~ '^20[0-9]{2}-20[0-9]{2}$' then
    raise exception 'A valid academic session is required' using errcode = '22023';
  end if;
  if right(p_academic_session, 4)::integer <> left(p_academic_session, 4)::integer + 1 then
    raise exception 'Academic session years must be consecutive' using errcode = '22023';
  end if;
  v_reg := upper(btrim(p_candidate->>'reg_no'));
  v_list := nullif(btrim(p_candidate->>'list_name'), '');
  v_email := nullif(lower(btrim(p_candidate->>'email')), '');
  if v_reg is null or v_reg !~ '^[0-9]{8,14}[A-Z]{1,3}$' or v_list is null
    or nullif(btrim(p_candidate->>'fullname'), '') is null
    or nullif(btrim(p_candidate->>'course'), '') is null then
    raise exception 'Candidate registration, list, name, and department are required' using errcode = '22023';
  end if;
  if v_email is not null and v_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then
    raise exception 'Invalid candidate email format' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(hashtextextended('portal-source:' || v_list || chr(31) || v_reg, 0));
  select c.* into v_existing from public.portal_candidate_sources s
    join public.portal_candidates c on c.id = s.candidate_id
    where s.list_name = v_list and s.reg_no = v_reg for update of c;
  if found then
    return jsonb_build_object('action', 'already_processed', 'candidate_id', v_existing.id,
                             'academic_session', v_existing.academic_session);
  end if;

  -- Recognize a source saved by a legacy writer after the initial receipt backfill.
  select * into v_existing from public.portal_candidates
    where list_name = v_list and reg_no = v_reg for update;
  if found then
    if v_existing.academic_session is null then
      update public.portal_candidates set academic_session = p_academic_session,
        department_source_list = v_list, department_source_reg_no = v_reg where id = v_existing.id;
    end if;
    insert into public.portal_candidate_sources(list_name, reg_no, candidate_id, academic_session)
      values (v_list, v_reg, v_existing.id, p_academic_session);
    return jsonb_build_object('action', 'already_processed', 'candidate_id', v_existing.id,
                             'academic_session', coalesce(v_existing.academic_session, p_academic_session));
  end if;

  if v_email is not null then
    perform pg_advisory_xact_lock(hashtextextended('portal-email:' || v_email, 0));
    select * into v_existing from public.portal_candidates where email_normalized = v_email for update;
  end if;
  if v_existing.id is not null then
    v_id := v_existing.id;
    v_session := v_existing.academic_session;
    if v_session is null then
      v_action := 'kept_unknown_session';
    elsif p_academic_session > v_session then
      update public.portal_candidates set
        course = p_candidate->>'course', faculty = p_candidate->>'faculty',
        academic_session = p_academic_session, department_source_list = v_list,
        department_source_reg_no = v_reg, updated_at = clock_timestamp() where id = v_id;
      v_session := p_academic_session;
      v_action := 'updated';
    else
      v_action := 'unchanged';
    end if;
  else
    insert into public.portal_candidates (
      fullname, gender, date_of_birth, email, mobile_phone, reg_no, state_of_origin,
      address, local_government, course, faculty, list_name, academic_session,
      department_source_list, department_source_reg_no
    ) values (
      p_candidate->>'fullname', p_candidate->>'gender', p_candidate->>'date_of_birth', v_email,
      p_candidate->>'mobile_phone', v_reg, p_candidate->>'state_of_origin',
      p_candidate->>'address', p_candidate->>'local_government', p_candidate->>'course',
      p_candidate->>'faculty', v_list, p_academic_session, v_list, v_reg
    ) returning id into v_id;
    v_session := p_academic_session;
    v_action := 'inserted';
  end if;

  -- Receipt and person changes commit together, even when an older entry is skipped.
  insert into public.portal_candidate_sources (list_name, reg_no, candidate_id, academic_session)
    values (v_list, v_reg, v_id, p_academic_session);
  return jsonb_build_object('action', v_action, 'candidate_id', v_id, 'academic_session', v_session);
end;
$$;

revoke all on function public.save_portal_candidate(jsonb, text) from public, anon, authenticated;
grant execute on function public.save_portal_candidate(jsonb, text) to service_role;
notify pgrst, 'reload schema';
