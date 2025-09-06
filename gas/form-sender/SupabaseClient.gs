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
  if (code >= 200 && code < 300) {
    try { return JSON.parse(text || 'null'); } catch (e) { return null; }
  }
  throw new Error('Supabase RPC error ' + code + ': ' + text);
}

/** リセット（06:25 JST） */
function resetSendQueueAll() {
  return callRpc_('reset_send_queue_all', {});
}

/** targeting用の当日キュー作成（06:35–06:50 JST） */
function createQueueForTargeting(targetingId, targetDateJst, targetingSql, ngCompaniesCsv, maxDailySends, shards) {
  return callRpc_('create_queue_for_targeting', {
    p_target_date: targetDateJst,
    p_targeting_id: Number(targetingId),
    p_targeting_sql: targetingSql || '',
    p_ng_companies: ngCompaniesCsv || '',
    p_max_daily: Number(maxDailySends || 0),
    p_shards: Number(shards || 8)
  });
}

