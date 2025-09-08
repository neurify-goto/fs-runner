-- JOIN最適化用の複合インデックス（JST当日重複除外の結合を高速化）
-- 用途:
--   - scripts/functions/create_queue_for_targeting.sql の candidates 生成時の LEFT JOIN
--   - scripts/functions/claim_next_batch.sql の LEFT JOIN
-- 条件:
--   targeting_id, company_id の一致 + submitted_at のJST当日(UTC境界)範囲条件
-- 既存インデックス(idx_submissions_targeting_company, idx_submissions_targeting_submitted_success)を補完
create index if not exists idx_submissions_target_company_submitted
  on public.submissions (targeting_id, company_id, submitted_at);

