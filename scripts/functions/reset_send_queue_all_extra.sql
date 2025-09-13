drop function if exists public.reset_send_queue_all_extra();
create or replace function public.reset_send_queue_all_extra()
returns integer
language plpgsql
as $$
declare
  v_deleted integer := 0;
begin
  -- send_queue_extra を初期化
  begin
    select count(*)::int into v_deleted from public.send_queue_extra;
  exception when others then
    v_deleted := 0;
  end;

  begin
    execute 'truncate table public.send_queue_extra restart identity';
  exception when others then
    -- 権限の都合で TRUNCATE 不可な場合のフォールバック（より低速）
    execute 'delete from public.send_queue_extra where true';
    begin
      execute 'alter table public.send_queue_extra alter column id restart with 1';
    exception when others then
      -- SERIAL/SEQUENCE カラム向けにシーケンスをリセット
      perform setval(pg_get_serial_sequence('public.send_queue_extra','id'), 1, false);
    end;
  end;
  return v_deleted;
end;
$$;

grant execute on function public.reset_send_queue_all_extra() to authenticated, service_role;
