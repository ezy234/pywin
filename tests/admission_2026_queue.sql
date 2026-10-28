begin;
set local role service_role;
do $$
declare r uuid:='ab112233-1234-4567-8910-123456789012'; a jsonb; b jsonb; n integer;
  tok uuid:='bc112233-1234-4567-8910-123456789012';
  source jsonb:='{"jamb_reg_no":"202600000001AA","fullname":"Queue Test","department":"TEST","faculty":"TEST","list_name":"2026-2027/test","academic_session":"2026-2027"}';
begin
  -- Dry-run transaction only; pause existing runs within this rollback to test the unique running index.
  update public.admission_2026_runs set status='paused' where status='running';
  insert into public.admission_2026_runs(id,fingerprint,workers) values(r,'test',2);
  insert into public.admission_2026_queue(run_id,reg_no,candidate) values(r,'202600000001AA',source),(r,'202600000002AA',source||'{"jamb_reg_no":"202600000002AA"}');
  a:=public.claim_admission_2026(r,tok); b:=public.claim_admission_2026(r,tok);
  if a->>'id'=b->>'id' or a is null or b is null then raise exception 'Claims overlap'; end if;
  if public.claim_admission_2026(r,tok) is not null then raise exception 'Leased queue claimed again'; end if;
  begin
    perform public.finish_admission_2026((a->>'id')::bigint,gen_random_uuid(),'closed');
    raise exception 'Wrong lease accepted';
  exception when sqlstate '22023' then null; end;
  perform public.finish_admission_2026((a->>'id')::bigint,tok,'closed');
  perform public.finish_admission_2026((b->>'id')::bigint,tok,'retry');
  update public.admission_2026_queue set available_at=now()-interval '1 second' where run_id=r and status='pending';
  b:=public.claim_admission_2026(r,tok);
  perform public.finish_admission_2026((b->>'id')::bigint,tok,'saved',jsonb_build_object(
    'reg_no',b->>'reg_no','fullname','Queue Test','course','TEST','faculty','TEST','list_name','2026-2027/test','email','queue-test@example.invalid'));
  select count(*) into n from public.admission_2026_sources where list_name='2026-2027/test';
  if n<>1 then raise exception 'Atomic receipt missing'; end if;
  update public.admission_2026_runs set status='paused' where id=r;
  if public.claim_admission_2026(r,tok) is not null then raise exception 'Paused run claimed'; end if;
  update public.admission_2026_runs set status='running',deadline=now()-interval '1 second' where id=r;
  if public.claim_admission_2026(r,tok) is not null then raise exception 'Expired run claimed'; end if;
  if has_function_privilege('anon','public.claim_admission_2026(uuid,uuid)','execute') then raise exception 'Anon can claim'; end if;
  if has_function_privilege('authenticated','public.finish_admission_2026(bigint,uuid,text,jsonb)','execute') then raise exception 'Authenticated can finish'; end if;
end $$;
rollback;
