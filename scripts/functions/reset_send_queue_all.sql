drop function if exists public.reset_send_queue_all();
create or replace function public.reset_send_queue_all()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_deleted integer := 0;
begin
  -- 事前に件数を取得（TRUNCATE は row_count を返さないため）
  select count(*)::int into v_deleted from public.send_queue;

  begin
    -- 最速: テーブルを一括TRUNCATEし、IDも初期化
    execute 'truncate table public.send_queue restart identity';
  exception when others then
    -- 例: 外部参照などでTRUNCATE不可の場合にフォールバック
    perform 1; -- no-op
    begin
      execute 'delete from public.send_queue where true';
    exception when others then
      -- DELETEも失敗する場合は例外を投げ直す
      raise;
    end;

    -- IDリセット（IDENTITY/serial双方を考慮して二段構え）
    begin
      execute 'alter table public.send_queue alter column id restart with 1';
    exception when others then
      perform setval(pg_get_serial_sequence('public.send_queue','id'), 1, false);
    end;
  end;

  return v_deleted;
end;
$$;

grant execute on function public.reset_send_queue_all() to authenticated, service_role;
