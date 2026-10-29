-- Run ONLY in the new destination after applying its migrations; fixtures roll back.
begin;
set local role service_role;
do $$
declare r uuid:='ce112233-1234-4567-8910-123456789012'; tok uuid:=gen_random_uuid();
  q jsonb; n bigint; base_people bigint; base_receipts bigint;
  source jsonb:='{"jamb_reg_no":"202500000001AA","fullname":"EXTERNAL SOURCE TEST","department":"TEST","faculty":"TEST","academic_session":"2025-2026","list_name":"2025-2026/__external_test__"}';
begin
  select count(*) into base_people from public.portal_candidates;
  select count(*) into base_receipts from public.portal_candidate_sources;
  insert into public.portal_swarm_runs(id,fingerprint,academic_session,workers,expected_rows)
    values(r,'test','all',2,3);
  insert into public.portal_swarm_queue(run_id,list_name,reg_no,priority,candidate) values
    (r,source->>'list_name',source->>'jamb_reg_no',0,source),
    (r,'2024-2025/__external_test__',source->>'jamb_reg_no',1,
      source||'{"list_name":"2024-2025/__external_test__","academic_session":"2024-2025"}'),
    (r,source->>'list_name','202500000002AA',2,source||'{"jamb_reg_no":"202500000002AA"}');
  n:=public.skip_external_portal_swarm(r,jsonb_build_array(jsonb_build_object(
      'list_name',source->>'list_name','reg_no',source->>'jamb_reg_no')));
  if n<>1 then raise exception 'Old source not matched by list AND registration'; end if;
  if public.skip_external_portal_swarm(r,jsonb_build_array(jsonb_build_object(
      'list_name',source->>'list_name','reg_no',source->>'jamb_reg_no')))<>0 then
    raise exception 'External bulk skip not idempotent';
  end if;
  perform public.activate_portal_swarm(r);
  q:=public.claim_portal_swarm(r,tok);
  if q->>'list_name'<>'2024-2025/__external_test__' then raise exception 'Wrong queue row claimed'; end if;
  perform public.finish_portal_swarm((q->>'id')::bigint,tok,'external_saved');
  if public.skip_external_portal_swarm(r,'[]')<>0 then raise exception 'Empty key set changed queue'; end if;
  if (select count(*) from public.portal_candidates)<>base_people then raise exception 'Old skip copied a person'; end if;
  if (select count(*) from public.portal_candidate_sources)<>base_receipts then raise exception 'Old skip copied a source receipt'; end if;
  tok:=gen_random_uuid(); q:=public.claim_portal_swarm(r,tok);
  perform public.finish_portal_swarm((q->>'id')::bigint,tok,'saved',jsonb_build_object(
    'fullname','EXTERNAL SOURCE TEST','reg_no',q->>'reg_no','list_name',q->>'list_name',
    'course','TEST','faculty','TEST','email','external-source-test@example.invalid'));
  if (select count(*) from public.portal_candidate_sources)<>base_receipts+1 then raise exception 'New source not saved'; end if;
  if has_function_privilege('anon','public.skip_external_portal_swarm(uuid,jsonb)','execute') then raise exception 'Anon can skip'; end if;
end $$;
rollback;
