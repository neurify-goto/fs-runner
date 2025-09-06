-- キューを毎朝完全リセット（保持なし）
-- 既存の関数シグネチャ（戻り値voidなど）がある環境では、先にDROPしてから再作成する
drop function if exists public.reset_send_queue_all();
create or replace function public.reset_send_queue_all()
returns integer
language sql
security definer
set search_path = public
as $$
  -- pg_safeupdate 等の拡張で WHERE 句必須の場合に備え、明示WHERE句を付与
  with del as (
    delete from public.send_queue where true returning 1
  )
  select count(*)::int from del;
$$;
