/**
 * 当日用キュー生成およびリセット関連のユーティリティ
 */

function resetSendQueueAllDaily() {
  try {
    const res = resetSendQueueAll();
    console.log('send_queue truncated');
    return { success: true, result: res };
  } catch (e) {
    console.error('resetSendQueueAll error:', e);
    return { success: false, error: String(e) };
  }
}

function resetSendQueueAllDailyExtra() {
  try {
    const res = resetSendQueueAll({ useExtra: true });
    console.log('send_queue_extra truncated');
    return { success: true, result: res };
  } catch (e) {
    console.error('resetSendQueueAllExtra error:', e);
    return { success: false, error: String(e) };
  }
}

function extractClearedCount_(res, key) {
  if (res === null || typeof res === 'undefined') return 0;
  if (typeof res === 'number') return Number(res) || 0;
  if (Array.isArray(res) && res.length > 0) {
    const first = res[0];
    if (first && typeof first === 'object' && first !== null) {
      const targetKey = key || Object.keys(first)[0];
      if (typeof targetKey !== 'undefined' && targetKey !== null) {
        const val = first[targetKey];
        return typeof val === 'number' ? val : Number(val) || 0;
      }
    }
  }
  if (typeof res === 'object') {
    const keys = Object.keys(res);
    if (keys.length > 0) {
      const targetKey = key || keys[0];
      const val = res[targetKey];
      return typeof val === 'number' ? val : Number(val) || 0;
    }
  }
  return 0;
}

function buildSendQueueForTargeting(targetingId = null, options) {
  try {
    options = options || {};
    const testMode = options.testMode === true;
    if (targetingId === null || typeof targetingId === 'undefined') {
      const ids = Array.isArray(CONFIG.QUEUE_TARGETING_IDS) ? CONFIG.QUEUE_TARGETING_IDS : [];
      if (ids.length > 0) {
        console.log(JSON.stringify({ level: 'info', event: 'queue_build_configured_list_start', ids }));
        let total = 0;
        const details = [];
        for (const id of ids) {
          const r = buildSendQueueForTargeting(id, options);
          if (r && r.success) total += Number(r.inserted || r.inserted_total || 0);
          details.push(Object.assign({ targeting_id: id }, r));
        }
        console.log(JSON.stringify({ level: 'info', event: 'queue_build_configured_list_done', total, count: ids.length }));
        return { success: details.every(d => d && d.success), inserted_total: total, details };
      } else {
        targetingId = CONFIG.DEFAULT_TARGETING_ID;
      }
    }

    const cfg = getTargetingConfig(targetingId);
    if (!cfg || !cfg.client || !cfg.targeting) throw new Error('invalid 2-sheet config');
    const t = cfg.targeting;
    const useExtra = testMode
      ? false
      : (Object.prototype.hasOwnProperty.call(options, 'useExtra')
        ? !!options.useExtra
        : !!(cfg.use_extra_table || t.use_extra_table));
    const clientName = (function(clientSection) {
      if (!clientSection || typeof clientSection.company_name !== 'string') return '';
      const trimmed = clientSection.company_name.trim();
      return trimmed || '';
    })(cfg.client);
    if (useExtra && !clientName) {
      throw new Error('clientシートのcompany_nameが空のためextraテーブルを利用できません');
    }
    const dateJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
    const shardCount = resolveShardCount_();
    const ngTokens = (t.ng_companies || '').split(/[，,]/).map(s => s.trim()).filter(Boolean);
    const tableMode = testMode ? 'test' : (useExtra ? 'extra' : 'primary');
    try {
      const cleared = clearSendQueueForTargeting(targetingId, testMode ? { testMode: true } : (useExtra ? { useExtra: true } : {}));
      const clearedCount = extractClearedCount_(cleared, testMode ? 'clear_send_queue_for_targeting_test' : (useExtra ? 'clear_send_queue_for_targeting_extra' : 'clear_send_queue_for_targeting'));
      console.log(JSON.stringify({
        level: 'info',
        event: 'queue_clear_before_build',
        targeting_id: targetingId,
        table_mode: tableMode,
        cleared: clearedCount
      }));
    } catch (clearError) {
      console.error(JSON.stringify({
        level: 'error',
        event: 'queue_clear_failed',
        targeting_id: targetingId,
        table_mode: tableMode,
        error: String(clearError && clearError.message ? clearError.message : clearError)
      }));
      throw clearError;
    }
    console.log(JSON.stringify({
      level: 'info', event: 'queue_build_start', targeting_id: targetingId, date_jst: dateJst,
      param_summary: {
        shards: shardCount, limit: 10000,
        targeting_sql_len: (t.targeting_sql || '').length,
        ng_companies_tokens: ngTokens.length
      }
    }));

    const startedMs = Date.now();
    try {
      const inserted = testMode
        ? createQueueForTargetingTest(
            targetingId,
            dateJst,
            t.targeting_sql || '',
            (t.ng_companies || ''),
            10000,
            shardCount
          )
        : createQueueForTargeting(
            targetingId,
            dateJst,
            t.targeting_sql || '',
            (t.ng_companies || ''),
            10000,
            shardCount,
            useExtra ? { useExtra: true, clientName } : undefined
          );
      const elapsedMs = Date.now() - startedMs;
      console.log(JSON.stringify({ level: 'info', event: 'queue_build_done', targeting_id: targetingId, inserted: Number(inserted) || 0, elapsed_ms: elapsedMs }));
      return { success: true, inserted };
    } catch (e) {
      const msg = String(e || '');
      const isStmtTimeout = /57014|statement timeout|canceling statement/i.test(msg);
      if (!isStmtTimeout) throw e;
      console.warn(JSON.stringify({ level: 'warning', event: 'queue_build_fallback_chunked', targeting_id: targetingId, reason: 'statement_timeout' }));
      const result = testMode
        ? buildSendQueueForTargetingChunkedTest_(targetingId, dateJst, t.targeting_sql || '', (t.ng_companies || ''))
        : (useExtra
            ? buildSendQueueForTargetingChunkedExtra_(targetingId, dateJst, t.targeting_sql || '', (t.ng_companies || ''), clientName)
            : buildSendQueueForTargetingChunked_(targetingId, dateJst, t.targeting_sql || '', (t.ng_companies || '')));
      return result;
    }
  } catch (e) {
    console.error('buildSendQueueForTargeting error:', e);
    return { success: false, error: String(e) };
  }
}

function buildSendQueueForAllTargetings() {
  console.log('=== 当日キュー一括生成開始 ===');
  try {
    const activeTargetings = getActiveTargetingIdsFromSheet();
    if (!activeTargetings || activeTargetings.length === 0) {
      console.log('アクティブなターゲティング設定が見つかりません');
      return { success: false, message: 'アクティブtargetingなし', processed: 0 };
    }

    let targetList = activeTargetings;
    console.log(JSON.stringify({ level: 'info', event: 'queue_build_target_all_active', total_active: activeTargetings.length }));

    const dateJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
    let processed = 0;
    let failed = 0;
    let totalInserted = 0;
    const details = [];

    const shardCount = resolveShardCount_();

    for (const t of targetList) {
      const targetingId = t.targeting_id || t.id || t;
      try {
        const cfg = getTargetingConfig(targetingId);
        if (!cfg || !cfg.client || !cfg.targeting) throw new Error('invalid 2-sheet config');

        const targeting = cfg.targeting;
        const useExtra = !!(cfg.use_extra_table || targeting.use_extra_table || t.use_extra_table === true);
        const clientName = (function(clientSection) {
          if (!clientSection || typeof clientSection.company_name !== 'string') return '';
          const trimmed = clientSection.company_name.trim();
          return trimmed || '';
        })(cfg.client);
        if (useExtra && !clientName) {
          throw new Error('clientシートのcompany_nameが空のためextraテーブルを利用できません');
        }
        const dateStartMs = Date.now();
        const ngTokens = (targeting.ng_companies || '').split(/[，,]/).map(s => s.trim()).filter(Boolean);
        console.log(JSON.stringify({
          level: 'info', event: 'queue_build_start', targeting_id: targetingId, date_jst: dateJst,
          param_summary: {
            shards: shardCount, limit: 10000,
            targeting_sql_len: (targeting.targeting_sql || '').length,
            ng_companies_tokens: ngTokens.length
          }
        }));

        let n = 0;
        try {
          const inserted = createQueueForTargeting(
            targetingId,
            dateJst,
            targeting.targeting_sql || '',
            (targeting.ng_companies || ''),
            10000,
            shardCount,
            useExtra ? { useExtra: true, clientName } : undefined
          );
          n = Number(inserted) || 0;
          const elapsedMs = Date.now() - dateStartMs;
          totalInserted += n;
          processed += 1;
          details.push({ targeting_id: targetingId, inserted: n, success: true });
          console.log(JSON.stringify({ level: 'info', event: 'queue_build_done', targeting_id: targetingId, inserted: n, elapsed_ms: elapsedMs }));
        } catch (e) {
          const msg = String(e || '');
          const isStmtTimeout = /57014|statement timeout|canceling statement/i.test(msg);
          if (!isStmtTimeout) throw e;
          console.warn(JSON.stringify({ level: 'warning', event: 'queue_build_fallback_chunked', targeting_id: targetingId, reason: 'statement_timeout' }));
          const res = useExtra
            ? buildSendQueueForTargetingChunkedExtra_(targetingId, dateJst, targeting.targeting_sql || '', (targeting.ng_companies || ''), clientName)
            : buildSendQueueForTargetingChunked_(targetingId, dateJst, targeting.targeting_sql || '', (targeting.ng_companies || ''));
          if (res && res.success) {
            n = Number(res.inserted_total || 0);
            totalInserted += n;
            processed += 1;
            details.push({ targeting_id: targetingId, inserted: n, success: true, mode: 'chunked' });
          } else {
            throw new Error(res && res.error ? res.error : 'chunked_fallback_failed');
          }
        }
      } catch (e) {
        failed += 1;
        details.push({ targeting_id: targetingId, success: false, error: String(e) });
        console.error(JSON.stringify({ level: 'error', event: 'queue_build_failed', targeting_id: targetingId, error: String(e) }));
      }
    }

    console.log(`=== 当日キュー一括生成完了: 成功=${processed - failed} / 失敗=${failed} / 合計投入=${totalInserted}件 ===`);
    return {
      success: failed === 0,
      date_jst: dateJst,
      processed,
      failed,
      total_inserted: totalInserted,
      details
    };
  } catch (e) {
    console.error('当日キュー一括生成エラー:', e);
    return { success: false, error: String(e) };
  }
}

function buildSendQueueForTargetingExtra(targetingId = null) {
  try {
    if (targetingId === null || typeof targetingId === 'undefined') {
      const ids = Array.isArray(CONFIG.QUEUE_TARGETING_IDS) ? CONFIG.QUEUE_TARGETING_IDS : [];
      if (ids.length > 0) {
        console.log(JSON.stringify({ level: 'info', event: 'queue_build_configured_list_start_extra', ids }));
        let total = 0;
        const details = [];
        for (const id of ids) {
          const r = buildSendQueueForTargetingExtra(id);
          if (r && r.success) total += Number(r.inserted || r.inserted_total || 0);
          details.push(Object.assign({ targeting_id: id }, r));
        }
        console.log(JSON.stringify({ level: 'info', event: 'queue_build_configured_list_done_extra', total, count: ids.length }));
        return { success: details.every(d => d && d.success), inserted_total: total, details };
      } else {
        targetingId = CONFIG.DEFAULT_TARGETING_ID;
      }
    }

    const cfg = getTargetingConfig(targetingId);
    if (!cfg || !cfg.client || !cfg.targeting) throw new Error('invalid 2-sheet config');
    const t = cfg.targeting;
    const shardCount = resolveShardCount_();
    const clientName = (function(clientSection) {
      if (!clientSection || typeof clientSection.company_name !== 'string') return '';
      const trimmed = clientSection.company_name.trim();
      return trimmed || '';
    })(cfg.client);
    const dateJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
    const ngTokens = (t.ng_companies || '').split(/[，,]/).map(s => s.trim()).filter(Boolean);
    try {
      const cleared = clearSendQueueForTargeting(targetingId, { useExtra: true });
      const clearedCount = extractClearedCount_(cleared, 'clear_send_queue_for_targeting_extra');
      console.log(JSON.stringify({
        level: 'info',
        event: 'queue_clear_before_build_extra',
        targeting_id: targetingId,
        table_mode: 'extra',
        cleared: clearedCount
      }));
    } catch (clearError) {
      console.error(JSON.stringify({
        level: 'error',
        event: 'queue_clear_failed_extra',
        targeting_id: targetingId,
        table_mode: 'extra',
        error: String(clearError && clearError.message ? clearError.message : clearError)
      }));
      throw clearError;
    }
    console.log(JSON.stringify({
      level: 'info', event: 'queue_build_start_extra', targeting_id: targetingId, date_jst: dateJst,
      param_summary: { shards: shardCount, limit: 10000, targeting_sql_len: (t.targeting_sql || '').length, ng_companies_tokens: ngTokens.length }
    }));

    const startedMs = Date.now();
    try {
      const inserted = createQueueForTargeting(
        targetingId,
        dateJst,
        t.targeting_sql || '',
        (t.ng_companies || ''),
        10000,
        shardCount,
        { useExtra: true, clientName }
      );
      const elapsedMs = Date.now() - startedMs;
      console.log(JSON.stringify({ level: 'info', event: 'queue_build_done_extra', targeting_id: targetingId, inserted: Number(inserted) || 0, elapsed_ms: elapsedMs }));
      return { success: true, inserted: Number(inserted) || 0, targeting_id: targetingId };
    } catch (e) {
      const msg = String(e || '');
      const isStmtTimeout = /57014|statement timeout|canceling statement/i.test(msg);
      if (!isStmtTimeout) throw e;
      console.warn(JSON.stringify({ level: 'warning', event: 'queue_build_fallback_chunked_extra', targeting_id: targetingId, reason: 'statement_timeout' }));
      const result = buildSendQueueForTargetingChunkedExtra_(targetingId, dateJst, t.targeting_sql || '', (t.ng_companies || ''), clientName);
      if (result && result.success) {
        return { success: true, inserted_total: Number(result.inserted_total || 0), targeting_id: targetingId, mode: 'chunked' };
      }
      throw new Error(result && result.error ? result.error : 'chunked_fallback_failed');
    }
  } catch (e) {
    console.error('buildSendQueueForTargetingExtra error:', e);
    return { success: false, error: String(e) };
  }
}

function buildSendQueueForAllTargetingsExtra() {
  console.log('=== 当日キュー一括生成開始（extra） ===');
  try {
    const activeTargetings = getActiveTargetingIdsFromSheet();
    if (!activeTargetings || activeTargetings.length === 0) {
      console.log('アクティブなターゲティング設定が見つかりません');
      return { success: false, message: 'アクティブtargetingなし', processed: 0 };
    }
    let processed = 0, failed = 0, totalInserted = 0; const details = [];
    const dateJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
    const shardCount = resolveShardCount_();
    for (const t of activeTargetings) {
      const targetingId = t.targeting_id || t.id || t;
      let cfg = null;
      let targetingSql = '';
      let ngCompaniesCsv = '';
      let clientName = '';
      try {
        cfg = getTargetingConfig(targetingId);
        if (!cfg || !cfg.client || !cfg.targeting) throw new Error('invalid 2-sheet config');
        targetingSql = cfg.targeting.targeting_sql || '';
        ngCompaniesCsv = cfg.targeting.ng_companies || '';
        if (cfg.client && typeof cfg.client.company_name === 'string') {
          const trimmed = cfg.client.company_name.trim();
          clientName = trimmed || '';
        }
        if (useExtra && !clientName) {
          throw new Error('clientシートのcompany_nameが空のためextraテーブルを利用できません');
        }

        const inserted = createQueueForTargeting(
          targetingId,
          dateJst,
          targetingSql,
          ngCompaniesCsv,
          10000,
          8,
          { useExtra: true, clientName }
        );
        processed++; totalInserted += Number(inserted) || 0;
        details.push({ targeting_id: targetingId, success: true, inserted: Number(inserted) || 0 });
      } catch (e) {
        const msg = String(e || '');
        const isStmtTimeout = /57014|statement timeout|canceling statement/i.test(msg);
        if (isStmtTimeout) {
          const res = buildSendQueueForTargetingChunkedExtra_(targetingId, dateJst, targetingSql, ngCompaniesCsv, clientName);
          if (res && res.success) {
            processed++; totalInserted += Number(res.inserted_total || 0);
            details.push({ targeting_id: targetingId, success: true, inserted: Number(res.inserted_total || 0), mode: 'chunked' });
            continue;
          }
        }
        failed++; details.push({ targeting_id: targetingId, success: false, error: msg });
      }
    }
    console.log(`=== 当日キュー一括生成完了（extra）: 成功=${processed - failed} / 失敗=${failed} / 合計投入=${totalInserted}件 ===`);
    return { success: failed === 0, date_jst: dateJst, processed, failed, total_inserted: totalInserted, details };
  } catch (e) {
    console.error('当日キュー一括生成エラー（extra）:', e);
    return { success: false, error: String(e) };
  }
}

function buildSendQueueForTargetingChunked_(targetingId, dateJst, targetingSql, ngCompaniesCsv) {
  const MAX_TOTAL = 10000;
  let total = 0;
  const shards = resolveShardCount_();
  let limit = CONFIG.CHUNK_LIMIT_INITIAL;
  const minLimit = CONFIG.CHUNK_LIMIT_MIN;
  let idWindow = CONFIG.CHUNK_ID_WINDOW_INITIAL;
  const minIdWindow = CONFIG.CHUNK_ID_WINDOW_MIN;
  const startedAll = Date.now();
  for (let stage = 1; stage <= 2; stage++) {
    let afterId = 0;
    let guard = 0;
    while (total < MAX_TOTAL && guard < 100) {
      if (Date.now() - startedAll > CONFIG.CHUNK_TIME_BUDGET_MS) {
        console.warn(JSON.stringify({ level: 'warning', event: 'queue_chunk_time_budget_exceeded', targeting_id: targetingId, stage, total, limit, idWindow }));
        return { success: true, inserted_total: total, targeting_id: targetingId, time_budget_exceeded: true };
      }
      guard++;
      const started = Date.now();
      try {
        const windowStart = afterId;
        const res = createQueueForTargetingStep(targetingId, dateJst, targetingSql, ngCompaniesCsv, shards, limit, windowStart, stage, idWindow);
        const elapsed = Date.now() - started;
        const inserted = Number((res && res[0] && res[0].inserted) || 0);
        const lastId = Number((res && res[0] && res[0].last_id) || windowStart);
        const hasMore = !!(res && res[0] && res[0].has_more);
        afterId = hasMore ? Math.max(lastId, windowStart) : (windowStart + idWindow);
        total += inserted;
        console.log(JSON.stringify({ level: 'info', event: 'queue_chunk_step', targeting_id: targetingId, stage, limit, after_id: afterId, inserted, total, elapsed_ms: elapsed, has_more: hasMore }));
        if (total >= MAX_TOTAL) break;
        if (!hasMore) { continue; }
        if (elapsed < 3000 && limit < 4000) limit = Math.min(4000, Math.floor(limit * 1.25));
      } catch (e) {
        const msg = String(e || '');
        const isStmtTimeout = /57014|statement timeout|canceling statement/i.test(msg);
        console.warn(JSON.stringify({ level: 'warning', event: 'queue_chunk_step_failed', targeting_id: targetingId, stage, limit, after_id: afterId, error: msg }));
        if (isStmtTimeout) {
          if (limit > minLimit) {
            limit = Math.max(minLimit, Math.floor(limit / 2));
            Utilities.sleep(500);
            continue;
          }
          if (idWindow > minIdWindow) {
            idWindow = Math.max(minIdWindow, Math.floor(idWindow / 2));
            Utilities.sleep(500);
            continue;
          }
        }
        return { success: false, error: msg, inserted_total: total, targeting_id: targetingId };
      }
    }
  }
  return { success: true, inserted_total: total, targeting_id: targetingId };
}

function buildSendQueueForTargetingChunkedExtra_(targetingId, dateJst, targetingSql, ngCompaniesCsv, clientName) {
  const MAX_TOTAL = 10000;
  let total = 0;
  const shards = resolveShardCount_();
  const normalizedClientName = (function(name) {
    if (typeof name !== 'string') return '';
    const trimmed = name.trim();
    return trimmed || '';
  })(clientName);
  let limit = CONFIG.CHUNK_LIMIT_INITIAL;
  const minLimit = CONFIG.CHUNK_LIMIT_MIN;
  let idWindow = CONFIG.CHUNK_ID_WINDOW_INITIAL;
  const minIdWindow = CONFIG.CHUNK_ID_WINDOW_MIN;
  const startedAll = Date.now();
  for (let stage = 1; stage <= 2; stage++) {
    let afterId = 0; let guard = 0;
    while (total < MAX_TOTAL && guard < 100) {
      if (Date.now() - startedAll > CONFIG.CHUNK_TIME_BUDGET_MS) {
        console.warn(JSON.stringify({ level: 'warning', event: 'queue_chunk_time_budget_exceeded_extra', targeting_id: targetingId, stage, total, limit, idWindow }));
        return { success: true, inserted_total: total, targeting_id: targetingId, time_budget_exceeded: true };
      }
      guard++;
      const started = Date.now();
      try {
        const windowStart = afterId;
        const res = createQueueForTargetingStep(targetingId, dateJst, targetingSql, ngCompaniesCsv, shards, limit, windowStart, stage, idWindow, { useExtra: true, clientName: normalizedClientName });
        const elapsed = Date.now() - started;
        const inserted = Number((res && res[0] && res[0].inserted) || 0);
        const lastId = Number((res && res[0] && res[0].last_id) || windowStart);
        const hasMore = !!(res && res[0] && res[0].has_more);
        afterId = hasMore ? Math.max(lastId, windowStart) : (windowStart + idWindow);
        total += inserted;
        console.log(JSON.stringify({ level: 'info', event: 'queue_chunk_step_extra', targeting_id: targetingId, stage, limit, after_id: afterId, inserted, total, elapsed_ms: elapsed, has_more: hasMore }));
        if (total >= MAX_TOTAL) break;
        if (!hasMore) { continue; }
        if (elapsed < 3000 && limit < 4000) limit = Math.min(4000, Math.floor(limit * 1.25));
      } catch (e) {
        const msg = String(e || '');
        const isStmtTimeout = /57014|statement timeout|canceling statement/i.test(msg);
        console.warn(JSON.stringify({ level: 'warning', event: 'queue_chunk_step_failed_extra', targeting_id: targetingId, stage, limit, after_id: afterId, error: msg }));
        if (isStmtTimeout) {
          if (limit > minLimit) { limit = Math.max(minLimit, Math.floor(limit / 2)); Utilities.sleep(500); continue; }
          if (idWindow > minIdWindow) { idWindow = Math.max(minIdWindow, Math.floor(idWindow / 2)); Utilities.sleep(500); continue; }
        }
        return { success: false, error: msg, inserted_total: total, targeting_id: targetingId };
      }
    }
  }
  return { success: true, inserted_total: total, targeting_id: targetingId };
}

function buildSendQueueForTargetingChunkedTest_(targetingId, dateJst, targetingSql, ngCompaniesCsv) {
  const MAX_TOTAL = 10000;
  let total = 0;
  const shards = resolveShardCount_();
  let limit = CONFIG.CHUNK_LIMIT_INITIAL;
  const minLimit = CONFIG.CHUNK_LIMIT_MIN;
  let idWindow = CONFIG.CHUNK_ID_WINDOW_INITIAL;
  const minIdWindow = CONFIG.CHUNK_ID_WINDOW_MIN;
  const startedAll = Date.now();
  for (let stage = 1; stage <= 2; stage++) {
    let afterId = 0;
    let guard = 0;
    while (total < MAX_TOTAL && guard < 100) {
      if (Date.now() - startedAll > CONFIG.CHUNK_TIME_BUDGET_MS) {
        console.warn(JSON.stringify({ level: 'warning', event: 'queue_chunk_time_budget_exceeded_test', targeting_id: targetingId, stage, total, limit, idWindow }));
        return { success: true, inserted_total: total, targeting_id: targetingId, time_budget_exceeded: true };
      }
      guard++;
      const started = Date.now();
      try {
        const windowStart = afterId;
        const res = createQueueForTargetingStepTest(targetingId, dateJst, targetingSql, ngCompaniesCsv, shards, limit, windowStart, stage, idWindow);
        const elapsed = Date.now() - started;
        const inserted = Number((res && res[0] && res[0].inserted) || 0);
        const lastId = Number((res && res[0] && res[0].last_id) || windowStart);
        const hasMore = !!(res && res[0] && res[0].has_more);
        afterId = hasMore ? Math.max(lastId, windowStart) : (windowStart + idWindow);
        total += inserted;
        console.log(JSON.stringify({ level: 'info', event: 'queue_chunk_step_test', targeting_id: targetingId, stage, limit, after_id: afterId, inserted, total, elapsed_ms: elapsed, has_more: hasMore }));
        if (total >= MAX_TOTAL) break;
        if (!hasMore) {
          continue;
        }
        if (elapsed < 3000 && limit < 4000) {
          limit = Math.min(4000, Math.floor(limit * 1.25));
        }
      } catch (e) {
        const msg = String(e || '');
        const isStmtTimeout = /57014|statement timeout|canceling statement/i.test(msg);
        console.warn(JSON.stringify({ level: 'warning', event: 'queue_chunk_step_failed_test', targeting_id: targetingId, stage, limit, after_id: afterId, error: msg }));
        if (isStmtTimeout) {
          if (limit > minLimit) {
            limit = Math.max(minLimit, Math.floor(limit / 2));
            Utilities.sleep(500);
            continue;
          }
          if (idWindow > minIdWindow) {
            idWindow = Math.max(minIdWindow, Math.floor(idWindow / 2));
            Utilities.sleep(500);
            continue;
          }
        }
        return { success: false, error: msg, inserted_total: total, targeting_id: targetingId };
      }
    }
  }
  return { success: true, inserted_total: total, targeting_id: targetingId };
}
