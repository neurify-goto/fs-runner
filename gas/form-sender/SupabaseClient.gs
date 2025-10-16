/**
 * Supabase REST/RPC クライアント（GAS直呼び出し）
 * Script Properties に SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY を設定して利用する。
 */

function getSupabaseConfig_() {
  const props = PropertiesService.getScriptProperties();
  const base = props.getProperty('SUPABASE_URL');
  const key = props.getProperty('SUPABASE_SERVICE_ROLE_KEY');
  if (!base || !key) throw new Error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set');
  return { base, key };
}

function callRpc_(fnName, payload) {
  const { base, key } = getSupabaseConfig_();
  const url = base.replace(/\/$/, '') + '/rest/v1/rpc/' + fnName;

  // 軽いリトライ（DB側の一時的な過負荷・statement_timeout回避用）
  // 3回まで指数バックオフ（1s, 2s, 4s）。GAS全体の実行上限を考慮して控えめに設定
  const maxAttempts = 3;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const startedAt = new Date();
    const startedMs = Date.now();
    // デバッグ: 呼び出しパラメータ要約
    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'rpc_call_start',
        fn: fnName,
        attempt,
        url,
        payload_summary: {
          keys: Object.keys(payload || {}),
          targeting_id: payload && payload.p_targeting_id,
          target_date: payload && payload.p_target_date,
          shards: payload && payload.p_shards,
          targeting_sql_len: (payload && (payload.p_targeting_sql || '')).length,
          ng_companies_len: (payload && (payload.p_ng_companies || '')).split(/[,，]/).filter(s => s.trim()).length
        },
        started_at: startedAt.toISOString()
      }));
    } catch (_) {}

    const res = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload || {}),
      muteHttpExceptions: true,
      headers: {
        'apikey': key,
        'Authorization': 'Bearer ' + key,
        'Prefer': 'return=representation'
      }
    });
    const code = res.getResponseCode();
    const text = res.getContentText();
    const elapsedMs = Date.now() - startedMs;

    // デバッグ: 応答のサマリ
    try {
      console.log(JSON.stringify({
        level: (code >= 200 && code < 300) ? 'info' : 'error',
        event: 'rpc_call_end',
        fn: fnName,
        attempt,
        code,
        elapsed_ms: elapsedMs,
        body_preview: (text || '').slice(0, 300)
      }));
    } catch (_) {}
    if (code >= 200 && code < 300) {
      try { return JSON.parse(text || 'null'); } catch (e) { return null; }
    }

    // 5xx かつ statement_timeout に類するメッセージのみリトライ対象
    const lower = (text || '').toLowerCase();
    const isRetryable = (code >= 500 && code < 600) && (
      lower.includes('statement timeout') ||
      lower.includes('canceling statement') ||
      lower.includes('57014')
    );
    if (attempt < maxAttempts && isRetryable) {
      const backoffMs = Math.pow(2, attempt - 1) * 1000; // 1s,2s,4s
      try {
        console.log(JSON.stringify({
          level: 'warning',
          event: 'rpc_call_retry',
          fn: fnName,
          attempt_next: attempt + 1,
          reason: 'retryable_statement_timeout',
          backoff_ms: backoffMs
        }));
      } catch (_) {}
      Utilities.sleep(backoffMs);
      continue;
    }
    throw new Error('Supabase RPC error ' + code + ': ' + text);
  }
}

/** リセット */
function resetSendQueueAll(options) {
  // 後方互換: 引数なしは従来テーブル（send_queue）
  const useExtra = options && options.useExtra === true;
  const fn = useExtra ? 'reset_send_queue_all_extra' : 'reset_send_queue_all';
  return callRpc_(fn, {});
}

/** targeting用の当日キュー作成 */
function createQueueForTargeting(targetingId, targetDateJst, targetingSql, ngCompaniesCsv, maxDailySends, shards, options) {
  const useExtra = options && options.useExtra === true;
  const clientName = (function(opt) {
    if (!opt || typeof opt.clientName !== 'string') return '';
    const trimmed = opt.clientName.trim();
    return trimmed || '';
  })(options);
  const fn = useExtra ? 'create_queue_for_targeting_extra' : 'create_queue_for_targeting';
  const payload = {
    p_target_date: targetDateJst,
    p_targeting_id: Number(targetingId),
    p_targeting_sql: targetingSql || '',
    p_ng_companies: ngCompaniesCsv || '',
    p_max_daily: Number(maxDailySends || 0),
    p_shards: Number(shards || 8)
  };
  if (useExtra) {
    payload.p_client_name = clientName || null;
  }
  return callRpc_(fn, payload);
}

/** チャンク投入: 単一ステージ(1/2)を after_id 以降 p_limit 件だけ投入 */
function createQueueForTargetingStep(targetingId, targetDateJst, targetingSql, ngCompaniesCsv, shards, limitPerCall, afterId, stage, idWindow, options) {
  const useExtra = options && options.useExtra === true;
  const clientName = (function(opt) {
    if (!opt || typeof opt.clientName !== 'string') return '';
    const trimmed = opt.clientName.trim();
    return trimmed || '';
  })(options);
  const fn = useExtra ? 'create_queue_for_targeting_step_extra' : 'create_queue_for_targeting_step';
  const payload = {
    p_target_date: targetDateJst,
    p_targeting_id: Number(targetingId),
    p_targeting_sql: targetingSql || '',
    p_ng_companies: ngCompaniesCsv || '',
    p_shards: Number(shards || 8),
    p_limit: Number(limitPerCall || 2000),
    p_after_id: Number(afterId || 0),
    p_stage: Number(stage || 1),
    p_id_window: Number(idWindow || 50000)
  };
  if (useExtra) {
    payload.p_client_name = clientName || null;
  }
  return callRpc_(fn, payload);
}

function resetSendQueueAllTestRpc_() {
  return callRpc_('reset_send_queue_all_test', {});
}

function clearSendQueueForTargeting(targetingId, options) {
  if (typeof targetingId === 'undefined' || targetingId === null) {
    throw new Error('targetingId is required for clearSendQueueForTargeting');
  }
  const testMode = options && options.testMode === true;
  const useExtra = options && options.useExtra === true;
  const fn = testMode
    ? 'clear_send_queue_for_targeting_test'
    : (useExtra ? 'clear_send_queue_for_targeting_extra' : 'clear_send_queue_for_targeting');
  const payload = { p_targeting_id: Number(targetingId) };
  return callRpc_(fn, payload);
}

function createQueueForTargetingTest(targetingId, targetDateJst, targetingSql, ngCompaniesCsv, maxDailySends, shards) {
  const payload = {
    p_target_date: targetDateJst,
    p_targeting_id: Number(targetingId),
    p_targeting_sql: targetingSql || '',
    p_ng_companies: ngCompaniesCsv || '',
    p_max_daily: Number(maxDailySends || 0),
    p_shards: Number(shards || 8)
  };
  return callRpc_('create_queue_for_targeting_test', payload);
}

function createQueueForTargetingStepTest(targetingId, targetDateJst, targetingSql, ngCompaniesCsv, shards, limitPerCall, afterId, stage, idWindow) {
  const payload = {
    p_target_date: targetDateJst,
    p_targeting_id: Number(targetingId),
    p_targeting_sql: targetingSql || '',
    p_ng_companies: ngCompaniesCsv || '',
    p_shards: Number(shards || 8),
    p_limit: Number(limitPerCall || 2000),
    p_after_id: Number(afterId || 0),
    p_stage: Number(stage || 1),
    p_id_window: Number(idWindow || 50000)
  };
  return callRpc_('create_queue_for_targeting_step_test', payload);
}

/**
 * ng_companies 列の入力方針（重要）
 * - 入力は「企業名（company_name）のカンマ区切り」のみを受け付ける方針です。
 * - 数値IDの指定や、企業名とIDの混在はサポートしません。
 * - SQL 側（create_queue_for_targeting / create_queue_for_targeting_step）でも
 *   company_name の完全一致で除外判定を行っています。
 * - 全角カンマは許容されますが、可能な限り半角カンマで入力してください。
 * - 見つからない企業名は除外対象になりません（ログの要約のみ）。
 *
 * 例: "株式会社サンプル, サンプル株式会社, Sample Inc."
 */
