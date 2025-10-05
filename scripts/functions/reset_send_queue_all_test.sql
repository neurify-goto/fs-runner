drop function if exists public.reset_send_queue_all_test();
create or replace function public.reset_send_queue_all_test()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_deleted integer := 0;
begin
  -- 事前に件数を取得（TRUNCATE は row_count を返さないため）
  begin
    select count(*)::int into v_deleted from public.send_queue_test;
  exception when others then
    v_deleted := 0;
  end;

  begin
    execute 'truncate table public.send_queue_test restart identity';
  exception when others then
    -- TRUNCATE 不可時のフォールバック: DELETE + ID リセット
    execute 'delete from public.send_queue_test where true';
    begin
      execute 'alter table public.send_queue_test alter column id restart with 1';
    exception when others then
      perform setval(pg_get_serial_sequence('public.send_queue_test','id'), 1, false);
    end;
  end;

  return v_deleted;
end;
$$;

grant execute on function public.reset_send_queue_all_test() to authenticated, service_role;
