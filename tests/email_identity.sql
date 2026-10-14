-- Real PostgreSQL regression cases; all fixture rows are rolled back.
begin;
set local role service_role;
do $$
declare
  nonce text := md5(clock_timestamp()::text || random()::text);
  email_one text;
  email_two text;
  prefix text;
  original jsonb;
  incoming jsonb;
  result jsonb;
  canonical public.portal_candidates%rowtype;
  original_id bigint;
  second_id bigint;
  blank_id bigint;
  amount integer;
begin
  email_one := 'email-identity-one-' || nonce || '@example.invalid';
  email_two := 'email-identity-two-' || nonce || '@example.invalid';
  prefix := '__email_identity_test__/' || nonce || '/';
  original := jsonb_build_object('fullname', 'ORIGINAL TEST PROFILE', 'email', email_one,
    'mobile_phone', '111', 'reg_no', '209900000001AA', 'list_name', prefix || 'old',
    'course', 'OLD DEPARTMENT', 'faculty', 'OLD FACULTY');
  result := public.save_portal_candidate(original, '2022-2023');
  if result->>'action' <> 'inserted' then raise exception 'Old-first insert failed'; end if;
  original_id := (result->>'candidate_id')::bigint;

  incoming := original || jsonb_build_object('email', '  ' || upper(email_one) || ' ',
    'reg_no', '209900000002AA', 'list_name', prefix || 'new', 'fullname', 'DO NOT REPLACE PROFILE',
    'mobile_phone', '222', 'course', 'NEW DEPARTMENT', 'faculty', 'NEW FACULTY');
  result := public.save_portal_candidate(incoming, '2025-2026');
  if result->>'action' <> 'updated' or (result->>'candidate_id')::bigint <> original_id then
    raise exception 'Newer session did not update the same email identity';
  end if;
  select * into canonical from public.portal_candidates where id = original_id;
  if canonical.course is distinct from 'NEW DEPARTMENT' or canonical.faculty is distinct from 'NEW FACULTY'
    or canonical.academic_session is distinct from '2025-2026'
    or canonical.fullname is distinct from 'ORIGINAL TEST PROFILE' or canonical.mobile_phone is distinct from '111'
    or canonical.reg_no is distinct from '209900000001AA' or canonical.list_name is distinct from prefix || 'old'
    or canonical.department_source_list is distinct from prefix || 'new'
    or canonical.department_source_reg_no is distinct from '209900000002AA' then
    raise exception 'Profile preservation or latest-department provenance failed';
  end if;
  result := public.save_portal_candidate(incoming, '2025-2026');
  if result->>'action' <> 'already_processed' then raise exception 'Retry was not idempotent'; end if;

  incoming := original || jsonb_build_object('reg_no', '209900000003AA', 'list_name', prefix || 'equal',
    'course', 'SAME SESSION MUST NOT OVERWRITE', 'faculty', 'SAME SESSION');
  result := public.save_portal_candidate(incoming, '2025-2026');
  if result->>'action' <> 'unchanged' then raise exception 'Equal session overwrote canonical details'; end if;
  incoming := incoming || jsonb_build_object('reg_no', '209900000004AA', 'list_name', prefix || 'older');
  result := public.save_portal_candidate(incoming, '2023-2024');
  if result->>'action' <> 'unchanged' then raise exception 'Older session overwrote newer details'; end if;
  select count(*) into amount from public.portal_candidates where email_normalized = email_one;
  if amount <> 1 then raise exception 'Email uniqueness failed'; end if;
  select count(*) into amount from public.portal_candidate_sources where candidate_id = original_id;
  if amount <> 4 then raise exception 'Source receipts were lost or duplicated'; end if;
  select * into canonical from public.portal_candidates where id = original_id;
  if canonical.course is distinct from 'NEW DEPARTMENT' then raise exception 'Newest department was not preserved'; end if;

  -- In reverse processing order, the first (newest) observation remains authoritative.
  incoming := original || jsonb_build_object('email', email_two, 'reg_no', '209900000011AA',
    'list_name', prefix || 'reverse-new', 'course', 'NEWEST FIRST', 'faculty', 'NEWEST FACULTY');
  result := public.save_portal_candidate(incoming, '2025-2026');
  second_id := (result->>'candidate_id')::bigint;
  incoming := incoming || jsonb_build_object('reg_no', '209900000012AA', 'list_name', prefix || 'reverse-old',
    'course', 'OLDER SECOND', 'faculty', 'OLDER FACULTY');
  result := public.save_portal_candidate(incoming, '2022-2023');
  if result->>'action' <> 'unchanged' or (result->>'candidate_id')::bigint <> second_id then
    raise exception 'Newest-first processing failed';
  end if;
  select * into canonical from public.portal_candidates where id = second_id;
  if canonical.course is distinct from 'NEWEST FIRST' or canonical.faculty is distinct from 'NEWEST FACULTY' then
    raise exception 'An older observation changed the newest details';
  end if;

  -- Blank email addresses must never merge different source candidates.
  incoming := original || jsonb_build_object('email', '', 'reg_no', '209900000021AA', 'list_name', prefix || 'blank-one');
  result := public.save_portal_candidate(incoming, '2022-2023');
  blank_id := (result->>'candidate_id')::bigint;
  result := public.save_portal_candidate(incoming, '2022-2023');
  if result->>'action' <> 'already_processed' then raise exception 'Blank-email source retry failed'; end if;
  incoming := incoming || jsonb_build_object('email', null, 'reg_no', '209900000022AA', 'list_name', prefix || 'blank-two');
  result := public.save_portal_candidate(incoming, '2022-2023');
  if result->>'action' <> 'inserted' or (result->>'candidate_id')::bigint = blank_id then
    raise exception 'Unrelated blank-email candidates were merged';
  end if;

  -- Unknown legacy dates must not be guessed from insertion order.
  insert into public.portal_candidates(fullname, email, reg_no, list_name, course, faculty)
    values ('UNKNOWN DATE TEST', 'unknown-' || nonce || '@example.invalid', '209900000041AA',
            prefix || 'unknown-original', 'UNKNOWN ORIGINAL', 'UNKNOWN FACULTY');
  incoming := original || jsonb_build_object('email', 'unknown-' || nonce || '@example.invalid',
    'reg_no', '209900000042AA', 'list_name', prefix || 'unknown-observed', 'course', 'DO NOT GUESS');
  result := public.save_portal_candidate(incoming, '2025-2026');
  if result->>'action' <> 'kept_unknown_session' then raise exception 'Unknown session was treated as older'; end if;
  select * into canonical from public.portal_candidates where id = (result->>'candidate_id')::bigint;
  if canonical.course is distinct from 'UNKNOWN ORIGINAL' or canonical.academic_session is not null then
    raise exception 'Unknown-session profile was changed';
  end if;

  -- The database index also blocks direct writes with case/space variations.
  begin
    insert into public.portal_candidates(fullname, email, reg_no, list_name)
      values ('DUPLICATE TEST', ' ' || upper(email_one) || ' ', '209900000031AA', prefix || 'direct-duplicate');
    raise exception 'Direct duplicate bypassed the email index';
  exception when unique_violation then null;
  end;

  begin
    perform public.save_portal_candidate(incoming, '2025-2025');
    raise exception 'Invalid academic session accepted';
  exception when invalid_parameter_value then null;
  end;
  if has_function_privilege('anon', 'public.save_portal_candidate(jsonb,text)', 'EXECUTE')
    or has_function_privilege('authenticated', 'public.save_portal_candidate(jsonb,text)', 'EXECUTE') then
    raise exception 'Public roles can execute the server-only write function';
  end if;
end;
$$;
rollback;
