-- Dedicated portal-bot destination in theProject (fkndpskocoatskwzrqwj).
create table if not exists public.portal_candidates (
  id bigint generated always as identity primary key,
  fullname text not null,
  gender text,
  date_of_birth text,
  email text,
  mobile_phone text,
  reg_no text not null,
  state_of_origin text,
  address text,
  local_government text,
  course text,
  faculty text,
  list_name text not null,
  created_at timestamptz not null default now(),
  constraint portal_candidates_reg_list_unique unique (reg_no, list_name)
);
create index if not exists portal_candidates_list_name_idx
  on public.portal_candidates (list_name);
alter table public.portal_candidates enable row level security;
revoke all on public.portal_candidates from anon, authenticated;
grant all on public.portal_candidates to service_role;
grant usage, select on sequence public.portal_candidates_id_seq to service_role;
notify pgrst, 'reload schema';
