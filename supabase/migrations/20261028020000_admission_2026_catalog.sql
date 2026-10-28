-- Candidate source data is private; do not publish the converted CSV to GitHub.
create table public.admission_2026_catalog (
  id text primary key check(id='primary'),
  fingerprint text not null,
  candidates jsonb not null check(jsonb_typeof(candidates)='array'),
  created_at timestamptz not null default now()
);
alter table public.admission_2026_catalog enable row level security;
revoke all on public.admission_2026_catalog from anon,authenticated;
grant all on public.admission_2026_catalog to service_role;
notify pgrst,'reload schema';
