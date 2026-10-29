-- Regression checks for the deployed legacy swarm migration.
-- Every modification below rolls back, including fixtures and test run statuses.
begin;
set local role service_role;
do $$
declare r uuid:='ad112233-1234-4567-8910-123456789012'; tok uuid:='be112233-1234-4567-8910-123456789012';
  a jsonb; b jsonb; answer jsonb;
  source jsonb:='{"jamb_reg_no":"202500000001AA","fullname":"LOCAL SWARM TEST","department":"TEST","faculty":"TEST","academic_session":"2025-2026","list_name":"2025-2026/__local_swarm_test__"}';
begin
  -- Fixture run is not activated until its full queue exists.
  insert into public.portal_swarm_runs(id,fingerprint,academic_session,workers,expected_rows)
    values(r,'test','all',2,2);
  if public.claim_portal_swarm(r,tok) is not null then raise exception 'Claimed preparing run'; end if;
  begin
    perform public.activate_portal_swarm(r);
    raise exception 'Incomplete seed activated';
  exception when sqlstate '22023' then null; end;
  -- Same JAMB under two lists is deliberately retained as two source observations.
  insert into public.portal_swarm_queue(run_id,list_name,reg_no,priority,candidate)
    values(r,source->>'list_name',source->>'jamb_reg_no',0,source),
      (r,'2024-2025/__local_swarm_test__',source->>'jamb_reg_no',1,
        source||'{"academic_session":"2024-2025","list_name":"2024-2025/__local_swarm_test__"}');
  perform public.activate_portal_swarm(r);
  a:=public.claim_portal_swarm(r,tok); b:=public.claim_portal_swarm(r,tok);
  if a is null or b is null or a->>'id'=b->>'id' then raise exception 'Claims overlap'; end if;
  if (a->>'priority')::integer<>0 then raise exception 'Newest-first priority lost'; end if;
  if public.claim_portal_swarm(r,tok) is not null then raise exception 'Leased item reclaimed too early'; end if;
  begin
    perform public.finish_portal_swarm((a->>'id')::bigint,gen_random_uuid(),'closed');
    raise exception 'Wrong token accepted';
  exception when sqlstate '22023' then null; end;
  begin
    perform public.finish_portal_swarm((a->>'id')::bigint,tok,'saved','{"reg_no":"wrong"}');
    raise exception 'Wrong candidate metadata accepted';
  exception when sqlstate '22023' then null; end;
  answer:=public.finish_portal_swarm((a->>'id')::bigint,tok,'saved',jsonb_build_object(
    'fullname','LOCAL SWARM TEST','reg_no',source->>'jamb_reg_no','list_name',source->>'list_name',
    'course','TEST','faculty','TEST','email','local-swarm-test@example.invalid'));
  if public.finish_portal_swarm((a->>'id')::bigint,tok,'saved','{}') is distinct from answer then
    raise exception 'Lost-response retry not idempotent';
  end if;
  if not exists(select 1 from public.portal_candidate_sources where list_name=source->>'list_name' and reg_no=source->>'jamb_reg_no') then
    raise exception 'Source receipt not saved atomically';
  end if;
  answer:=public.finish_portal_swarm((b->>'id')::bigint,tok,'retry');
  if public.finish_portal_swarm((b->>'id')::bigint,tok,'retry') is distinct from answer then
    raise exception 'Retry response not idempotent';
  end if;
  if public.claim_portal_swarm(r,gen_random_uuid()) is not null then raise exception 'Retry cooldown ignored'; end if;
  update public.portal_swarm_queue set available_at=now()-interval '1 second' where id=(b->>'id')::bigint;
  tok:=gen_random_uuid();
  b:=public.claim_portal_swarm(r,tok);
  update public.portal_swarm_queue set lease_until=now()-interval '1 second' where id=(b->>'id')::bigint;
  tok:=gen_random_uuid();
  b:=public.claim_portal_swarm(r,tok);
  if (b->>'attempts')::integer<>3 then raise exception 'Expired lease not recovered'; end if;
  perform public.finish_portal_swarm((b->>'id')::bigint,tok,'retry');
  if not exists(select 1 from public.portal_swarm_queue where id=(b->>'id')::bigint and status='failed') then
    raise exception 'Attempt limit not enforced';
  end if;
  update public.portal_swarm_runs set status='paused' where id=r;
  if public.claim_portal_swarm(r,tok) is not null then raise exception 'Paused run claimed'; end if;
  if has_function_privilege('anon','public.claim_portal_swarm(uuid,uuid)','execute') then raise exception 'Anon can claim'; end if;
  if has_function_privilege('authenticated','public.finish_portal_swarm(bigint,uuid,text,jsonb)','execute') then raise exception 'Authenticated can finish'; end if;
end $$;
rollback;
