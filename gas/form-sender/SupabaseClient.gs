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
function normalizeNgCompaniesToIdCsv(rawNgCompanies) {
  try {
    const s = (rawNgCompanies || '').trim();
    if (!s) return '';

    // 区切り: カンマ（全角/半角）、改行、タブ、セミコロンに対応
    const tokens = s
      .split(/[\n\r,，;\t]/)
      .map(x => x.trim())
      .filter(Boolean);

    if (tokens.length === 0) return '';

    const numericIds = [];
    const nameTokens = [];
    for (const t of tokens) {
      if (/^\d+$/.test(t)) {
        numericIds.push(String(Number(t))); // 先頭ゼロ対策
      } else {
        nameTokens.push(t);
      }
    }

    // 企業名→ID 解決
    const resolvedIds = lookupCompanyIdsByNames_(nameTokens);
    const allIds = [...numericIds, ...resolvedIds]
      .filter(Boolean)
      .map(String);

    // 重複排除
    const deduped = Array.from(new Set(allIds));
    const csv = deduped.join(',');

    console.log(`ng_companies 正規化: 入力=${tokens.length}件, 数値=${numericIds.length}件, 名前解決=${resolvedIds.length}件, 合計ID=${deduped.length}件`);
    if (nameTokens.length > 0 && resolvedIds.length < nameTokens.length) {
      const unresolved = nameTokens.filter(nm => !resolvedIds.__names_ok || !resolvedIds.__names_ok.has(nm));
      if (unresolved.length > 0) {
        console.warn(`ng_companiesの企業名の一部が見つかりませんでした（無視）: ${unresolved.join(' / ')}`);
      }
    }
    return csv;
  } catch (e) {
    console.error('ng_companies 正規化エラー（安全のため空とみなす）:', e.message || e);
    return '';
  }
}

/**
 * 内部利用: 企業名配列を companies.id に解決
 * - 完全一致で1件取得、重複名は最初の1件のみ採用
 * - 検索件数が多すぎる事故を避けるため最大100件に制限
 * @param {string[]} names
 * @returns {string[]} 取得できたID配列（付加プロパティ __names_ok: Set<string>）
 */
function lookupCompanyIdsByNames_(names) {
  const resultIds = [];
  const okNames = new Set();
  try {
    if (!names || names.length === 0) return resultIds;
    const { base, key } = getSupabaseConfig_();
    const urlBase = base.replace(/\/$/, '') + '/rest/v1/companies?select=id&limit=1&company_name=eq.';
    const max = Math.min(names.length, 100);
    for (let i = 0; i < max; i++) {
      const name = names[i];
      if (!name) continue;
      const url = urlBase + encodeURIComponent(name);
      const res = UrlFetchApp.fetch(url, {
        method: 'get',
        muteHttpExceptions: true,
        headers: { 'apikey': key, 'Authorization': 'Bearer ' + key }
      });
      const code = res.getResponseCode();
      if (code >= 200 && code < 300) {
        const arr = JSON.parse(res.getContentText() || '[]');
        if (Array.isArray(arr) && arr.length > 0 && arr[0].id) {
          resultIds.push(String(arr[0].id));
          okNames.add(name);
        } else {
          console.warn(`企業名が見つかりません: ${name}`);
        }
      } else {
        console.warn(`企業名検索エラー(${code}): ${name} -> ${res.getContentText()}`);
      }
    }
  } catch (e) {
    console.error('企業名→ID解決エラー:', e.message || e);
  }
  // 呼び出し元で未解決名の判定に使えるよう補助情報を付与
  resultIds.__names_ok = okNames;
  return resultIds;
}
