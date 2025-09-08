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

/** リセット */
function resetSendQueueAll() {
  return callRpc_('reset_send_queue_all', {});
}

/** targeting用の当日キュー作成 */
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

/**
 * ng_companies列の値（企業名・ID混在可）を、RPCが受け付ける「カンマ区切りのID群」に正規化
 * - 数値トークンはそのままIDとして扱う
 * - 数値以外は company_name の完全一致で companies を検索し、id に解決する
 * - 見つからない企業名は無視（警告ログのみ）
 * @param {string} rawNgCompanies シートのng_companies文字列
 * @returns {string} カンマ区切りID文字列（空なら''）
 */
// 社名ベース除外に移行したため、ng_companiesのID正規化は不要。
