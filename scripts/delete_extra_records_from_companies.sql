-- companies テーブルの id > 536156 を削除し、ID 採番を最大ID+1にリセットする
-- 注意: 実行前にバックアップ取得と削除件数の確認を推奨
-- 参考: SELECT COUNT(*) FROM public.companies WHERE id > 536156;

BEGIN;

LOCK TABLE public.companies IN EXCLUSIVE MODE;

-- 余剰レコード削除
DELETE FROM public.companies
WHERE id > 536156;

-- ID の次値を残存レコードの最大ID+1に整合
DO $$
DECLARE
  new_start   bigint;
  seq_name    text;
  id_is_ident boolean;
BEGIN
  -- 次に割り当てたい値（残存レコードの最大ID + 1）
  SELECT COALESCE(MAX(id), 0) + 1
    INTO new_start
  FROM public.companies;

  -- IDENTITY 列かどうかを判定
  SELECT (is_identity = 'YES')
    INTO id_is_ident
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name   = 'companies'
    AND column_name  = 'id';

  IF id_is_ident THEN
    -- IDENTITY の場合: RESTART WITH で次値を設定
    EXECUTE format('ALTER TABLE public.companies ALTER COLUMN id RESTART WITH %s', new_start);
  ELSE
    -- serial/sequence の場合
    SELECT pg_get_serial_sequence('public.companies','id')
      INTO seq_name;

    IF seq_name IS NOT NULL THEN
      PERFORM setval(seq_name, new_start, false);
    ELSE
      -- 手動採番など、シーケンス未連携の場合はスキップ
      RAISE NOTICE 'No sequence/identity found on public.companies.id; reset skipped.';
    END IF;
  END IF;
END $$;

COMMIT;

