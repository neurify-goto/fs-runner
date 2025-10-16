drop function if exists public.clear_send_queue_for_targeting(bigint);
create or replace function public.clear_send_queue_for_targeting(
  p_targeting_id bigint
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_deleted integer := 0;
begin
  if p_targeting_id is null then
    raise exception 'p_targeting_id is required';
  end if;

  delete from public.send_queue
   where targeting_id = p_targeting_id;
  get diagnostics v_deleted = row_count;

  return coalesce(v_deleted, 0);
end;
$$;

grant execute on function public.clear_send_queue_for_targeting(bigint) to authenticated, service_role;
