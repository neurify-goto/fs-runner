/**
 * Supabase統計情報取得モジュール
 * companiesテーブルの統計データを取得する機能
 */

/**
 * Supabaseクライアント取得
 * @returns {Object} Supabaseクライアントオブジェクト
 */
function getSupabaseClient() {
  const supabaseUrl = PropertiesService.getScriptProperties().getProperty('SUPABASE_URL');
  const supabaseKey = PropertiesService.getScriptProperties().getProperty('SUPABASE_SERVICE_ROLE_KEY');
  
  if (!supabaseUrl || !supabaseKey) {
    throw new Error('Supabase設定が不正です: URL またはキーが設定されていません');
  }
  
  return {
    url: supabaseUrl,
    key: supabaseKey,
    headers: {
      'apikey': supabaseKey,
      'Authorization': `Bearer ${supabaseKey}`,
      'Content-Type': 'application/json'
    }
  };
}

/**
 * companiesテーブルの統計情報を取得（RPC一括集計版）
 * 従来の7回のクエリを1回のRPC呼び出しに統合してSupabase負荷を軽減
 * @returns {Object} 統計情報オブジェクト
 */
function getCompaniesStats() {
  try {
    console.log('統計情報取得開始（RPC一括集計版）');
    const supabase = getSupabaseClient();
    
    // RPC関数を呼び出して一括で全統計を取得
    const rpcUrl = `${supabase.url}/rest/v1/rpc/get_companies_stats_all`;
    
    console.log('RPC関数呼び出し: get_companies_stats_all');
    const startTime = new Date();
    
    const response = UrlFetchApp.fetch(rpcUrl, {
      method: 'POST',
      headers: {
        ...supabase.headers,
        'Content-Type': 'application/json'
      },
      payload: JSON.stringify({}), // パラメータなし
      muteHttpExceptions: true
    });
    
    const endTime = new Date();
    const processingTime = endTime.getTime() - startTime.getTime();
    
    const responseCode = response.getResponseCode();
    
    if (responseCode !== 200) {
      const errorContent = response.getContentText();
      console.error(`RPC実行エラー: ${responseCode} - ${errorContent}`);
      
      // エラーの詳細分類
      let errorType = 'UNKNOWN_ERROR';
      if (responseCode === 404) {
        errorType = 'RPC_FUNCTION_NOT_FOUND';
      } else if (responseCode === 400) {
        errorType = 'BAD_REQUEST';
      } else if (responseCode >= 500) {
        errorType = 'SERVER_ERROR';
      } else if (responseCode === 401 || responseCode === 403) {
        errorType = 'AUTH_ERROR';
      }
      
      throw new Error(`${errorType}: HTTP ${responseCode}: ${errorContent}`);
    }
    
    const data = JSON.parse(response.getContentText());
    
    if (!data || data.length === 0) {
      throw new Error('RPC関数から統計データが返されませんでした');
    }
    
    // RPC結果を既存の形式に変換
    const rpcResult = data[0]; // RPC関数は1行の結果を返す
    const stats = {
      totalCount: parseInt(rpcResult.total_count) || 0,
      withCompanyUrl: parseInt(rpcResult.with_company_url_count) || 0,
      formNotExplored: parseInt(rpcResult.form_not_explored_count) || 0,
      withFormUrl: parseInt(rpcResult.with_form_url_count) || 0,
      formNotAnalyzed: parseInt(rpcResult.form_not_analyzed_count) || 0,
      formAnalyzed: parseInt(rpcResult.form_analyzed_count) || 0,
      validInstruction: parseInt(rpcResult.valid_instruction_count) || 0
    };
    
    console.log(`統計情報取得完了（RPC版）: 処理時間 ${processingTime}ms`);
    console.log('統計情報:', stats);
    
    return {
      success: true,
      data: stats,
      processingTime: processingTime,
      queryType: 'RPC_BATCH',
      timestamp: new Date()
    };
    
  } catch (error) {
    console.error('統計情報取得エラー（RPC版）:', error);
    return {
      success: false,
      error: error.toString(),
      queryType: 'RPC_BATCH',
      timestamp: new Date()
    };
  }
}

/**
 * 指定条件でのレコード数を取得
 * @param {Object} supabase Supabaseクライアント
 * @param {string} whereClause WHERE句の条件
 * @returns {number} レコード数
 */
function getCount(supabase, whereClause) {
  try {
    let query = `${supabase.url}/rest/v1/companies?select=id&limit=0`;
    if (whereClause) {
      query += `&${whereClause}`;
    }
    
    console.log(`クエリ実行: ${whereClause || '全件'}`);
    
    const response = UrlFetchApp.fetch(query, {
      method: 'GET',
      headers: {
        ...supabase.headers,
        'Prefer': 'count=exact'
      },
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode !== 200 && responseCode !== 206) {
      console.error(`クエリエラー: ${responseCode} - ${response.getContentText()}`);
      throw new Error(`HTTP ${responseCode}: ${response.getContentText()}`);
    }
    
    // Content-Rangeヘッダーから件数を取得
    const headers = response.getHeaders();
    const contentRange = headers['content-range'] || headers['Content-Range'] || headers['CONTENT-RANGE'] || '0-0/0';
    const count = parseInt(contentRange.split('/')[1]) || 0;
    
    console.log(`条件「${whereClause || '全件'}」: ${count}件`);
    return count;
    
  } catch (error) {
    console.error(`件数取得エラー (条件: ${whereClause}):`, error);
    throw error;
  }
}

/**
 * データベース接続テスト
 * @returns {Object} テスト結果
 */
function testSupabaseConnection() {
  try {
    console.log('Supabase接続テスト開始');
    
    const supabase = getSupabaseClient();
    
    // シンプルなクエリでテスト
    const testQuery = `${supabase.url}/rest/v1/companies?select=id&limit=1`;
    
    const response = UrlFetchApp.fetch(testQuery, {
      method: 'GET',
      headers: supabase.headers,
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200 || responseCode === 206) {
      const data = JSON.parse(response.getContentText());
      console.log(`Supabase接続テスト成功 (${data.length}件のレコードが存在)`);
      return { success: true, message: `Supabase接続成功 (${data.length}件確認)` };
    } else {
      const errorText = response.getContentText();
      console.error(`Supabase接続テスト失敗: ${responseCode} - ${errorText}`);
      return { success: false, error: `HTTP ${responseCode}: ${errorText}` };
    }
    
  } catch (error) {
    console.error('Supabase接続テストエラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * 統計情報取得のテスト実行（RPC版）
 * @returns {Object} テスト結果
 */
function testGetStats() {
  console.log('=== 統計情報取得テスト（RPC一括集計版） ===');
  const result = getCompaniesStats();
  
  if (result.success) {
    console.log('統計情報取得成功（RPC版）:');
    console.log(`処理時間: ${result.processingTime || '不明'}ms`);
    console.log(`クエリタイプ: ${result.queryType || 'UNKNOWN'}`);
    Object.entries(result.data).forEach(([key, value]) => {
      console.log(`${key}: ${value}`);
    });
    
    // パフォーマンス情報の表示
    if (result.processingTime) {
      console.log(`パフォーマンス: 従来の7クエリから1クエリに削減, 処理時間 ${result.processingTime}ms`);
    }
  } else {
    console.error('統計情報取得失敗（RPC版）:', result.error);
    console.error(`クエリタイプ: ${result.queryType || 'UNKNOWN'}`);
  }
  
  return result;
}



/**
 * すべてのtargeting_idの統計を取得（RPC一括集計版）
 * PostgreSQL関数を使用して1回のクエリで全targeting_idの統計を取得
 * @param {Array} targetingIds targeting_idの配列
 * @returns {Object} targeting_idをキーとした統計オブジェクト
 */
function getAllSubmissionsStatsByTargeting(targetingIds) {
  try {
    // 入力バリデーション
    if (!targetingIds) {
      console.log('targeting_id配列がnullまたはundefinedです');
      return {};
    }
    if (!Array.isArray(targetingIds)) {
      console.error('targeting_idは配列である必要があります:', typeof targetingIds);
      return {};
    }
    if (targetingIds.length === 0) {
      console.log('targeting_id配列が空です');
      return {};
    }
    
    console.log(`一括統計取得開始 (RPC版): ${targetingIds.length}件のtargeting_id (${targetingIds.join(', ')})`);
    const supabase = getSupabaseClient();
    
    // RPC関数を呼び出して一括で統計を取得
    const rpcUrl = `${supabase.url}/rest/v1/rpc/get_targeting_submissions_stats_all`;
    
    console.log('RPC関数呼び出し: get_targeting_submissions_stats_all');
    
    const response = UrlFetchApp.fetch(rpcUrl, {
      method: 'POST',
      headers: {
        ...supabase.headers,
        'Content-Type': 'application/json'
      },
      payload: JSON.stringify({
        targeting_ids: targetingIds.map(id => parseInt(id))
      }),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode !== 200) {
      const errorContent = response.getContentText();
      console.error(`RPC実行エラー: ${responseCode} - ${errorContent}`);
      
      // エラーの詳細分類
      let errorType = 'UNKNOWN_ERROR';
      if (responseCode === 404) {
        errorType = 'RPC_FUNCTION_NOT_FOUND';
      } else if (responseCode === 400) {
        errorType = 'BAD_REQUEST';
      } else if (responseCode >= 500) {
        errorType = 'SERVER_ERROR';
      } else if (responseCode === 401 || responseCode === 403) {
        errorType = 'AUTH_ERROR';
      }
      
      throw new Error(`${errorType}: HTTP ${responseCode}: ${errorContent}`);
    }
    
    const data = JSON.parse(response.getContentText());
    console.log(`RPC実行成功: ${data.length}件の統計を取得`);
    
    // 結果をマップ形式に変換
    const statsMap = {};
    
    // 初期化（すべてのtargeting_idを0で初期化）
    targetingIds.forEach(id => {
      statsMap[id] = { total_count: 0, success_count: 0 };
    });
    
    // RPCの結果でマップを更新
    data.forEach(row => {
      const targetingId = row.targeting_id;
      statsMap[targetingId] = {
        total_count: parseInt(row.total_count) || 0,
        success_count: parseInt(row.success_count) || 0
      };
    });
    
    console.log(`一括統計取得完了 (RPC版): ${Object.keys(statsMap).length}件のtargeting_id統計を集計`);
    
    // ログ出力（デバッグ用）
    Object.entries(statsMap).forEach(([id, stats]) => {
      console.log(`targeting_id ${id}: total=${stats.total_count}, success=${stats.success_count}`);
    });
    
    return statsMap;
    
  } catch (error) {
    console.error('RPC一括統計取得エラー:', error);
    throw error;
  }
}


/**
 * すべてのtargeting_idの本日のみの統計を取得（RPC一括集計版）
 * PostgreSQL関数を使用して1回のクエリで全targeting_idの本日統計を取得
 * submitted_at（UTC）を日本時間（JST）に変換して本日判定を行う
 * @param {Array} targetingIds targeting_idの配列
 * @returns {Object} targeting_idをキーとした本日統計オブジェクト
 */
function getAllSubmissionsStatsByTargetingToday(targetingIds) {
  try {
    // 入力バリデーション
    if (!targetingIds) {
      console.log('targeting_id配列がnullまたはundefinedです');
      return {};
    }
    if (!Array.isArray(targetingIds)) {
      console.error('targeting_idは配列である必要があります:', typeof targetingIds);
      return {};
    }
    if (targetingIds.length === 0) {
      console.log('targeting_id配列が空です');
      return {};
    }
    
    console.log(`本日統計一括取得開始 (RPC版): ${targetingIds.length}件のtargeting_id (${targetingIds.join(', ')})`);
    const supabase = getSupabaseClient();
    
    // RPC関数を呼び出して本日の統計を一括取得
    const rpcUrl = `${supabase.url}/rest/v1/rpc/get_targeting_submissions_stats_today`;
    
    console.log('RPC関数呼び出し: get_targeting_submissions_stats_today (UTC→JST変換対応)');
    
    const response = UrlFetchApp.fetch(rpcUrl, {
      method: 'POST',
      headers: {
        ...supabase.headers,
        'Content-Type': 'application/json'
      },
      payload: JSON.stringify({
        targeting_ids: targetingIds.map(id => parseInt(id))
      }),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode !== 200) {
      const errorContent = response.getContentText();
      console.error(`本日統計RPC実行エラー: ${responseCode} - ${errorContent}`);
      
      // エラーの詳細分類
      let errorType = 'UNKNOWN_ERROR';
      if (responseCode === 404) {
        errorType = 'RPC_FUNCTION_NOT_FOUND';
      } else if (responseCode === 400) {
        errorType = 'BAD_REQUEST';
      } else if (responseCode >= 500) {
        errorType = 'SERVER_ERROR';
      } else if (responseCode === 401 || responseCode === 403) {
        errorType = 'AUTH_ERROR';
      }
      
      throw new Error(`TODAY_STATS_${errorType}: HTTP ${responseCode}: ${errorContent}`);
    }
    
    const data = JSON.parse(response.getContentText());
    console.log(`本日統計RPC実行成功: ${data.length}件の本日統計を取得`);
    
    // 結果をマップ形式に変換
    const todayStatsMap = {};
    
    // 初期化（すべてのtargeting_idを0で初期化）
    targetingIds.forEach(id => {
      todayStatsMap[id] = { total_count_today: 0, success_count_today: 0 };
    });
    
    // RPCの結果でマップを更新
    data.forEach(row => {
      const targetingId = row.targeting_id;
      todayStatsMap[targetingId] = {
        total_count_today: parseInt(row.total_count_today) || 0,
        success_count_today: parseInt(row.success_count_today) || 0
      };
    });
    
    console.log(`本日統計一括取得完了 (RPC版): ${Object.keys(todayStatsMap).length}件のtargeting_id本日統計を集計`);
    
    // ログ出力（デバッグ用）
    Object.entries(todayStatsMap).forEach(([id, stats]) => {
      console.log(`targeting_id ${id} (本日): total=${stats.total_count_today}, success=${stats.success_count_today}`);
    });
    
    return todayStatsMap;
    
  } catch (error) {
    console.error('RPC本日統計一括取得エラー:', error);
    throw error;
  }
}

/**
 * targeting関連のクエリ機能テスト
 * @returns {Object} テスト結果
 */
function testTargetingQueries() {
  console.log('=== targeting関連クエリテスト開始（単一クエリ版） ===');
  
  const results = {
    batchStatsRetrieval: null,
    batchTodayStatsRetrieval: null
  };
  
  try {
    // テスト用のtargeting_idサンプル
    const testTargetingIds = [1, 2, 3, 4, 5];
    console.log(`テスト用targeting_id: [${testTargetingIds.join(', ')}]`);
    
    // 一括統計取得テスト（通算統計）
    console.log('一括統計取得テスト（通算統計）');
    const batchStartTime = new Date();
    const batchStats = getAllSubmissionsStatsByTargeting(testTargetingIds);
    const batchEndTime = new Date();
    const batchTime = batchEndTime.getTime() - batchStartTime.getTime();
    
    results.batchStatsRetrieval = {
      success: true,
      targeting_ids: testTargetingIds,
      stats_count: Object.keys(batchStats).length,
      processing_time_ms: batchTime,
      stats: batchStats
    };
    
    console.log(`通算統計取得結果: ${Object.keys(batchStats).length}件, 処理時間: ${batchTime}ms`);
    
    // 一括本日統計取得テスト
    console.log('一括本日統計取得テスト（UTC→JST変換対応）');
    const todayBatchStartTime = new Date();
    const todayBatchStats = getAllSubmissionsStatsByTargetingToday(testTargetingIds);
    const todayBatchEndTime = new Date();
    const todayBatchTime = todayBatchEndTime.getTime() - todayBatchStartTime.getTime();
    
    results.batchTodayStatsRetrieval = {
      success: true,
      targeting_ids: testTargetingIds,
      stats_count: Object.keys(todayBatchStats).length,
      processing_time_ms: todayBatchTime,
      stats: todayBatchStats
    };
    
    console.log(`本日統計取得結果: ${Object.keys(todayBatchStats).length}件, 処理時間: ${todayBatchTime}ms`);
    
    // 結果の詳細表示
    testTargetingIds.forEach(id => {
      const stats = batchStats[id] || { total_count: 0, success_count: 0 };
      const todayStats = todayBatchStats[id] || { total_count_today: 0, success_count_today: 0 };
      console.log(`targeting_id ${id}: 通算(total=${stats.total_count}, success=${stats.success_count}) 本日(total=${todayStats.total_count_today}, success=${todayStats.success_count_today})`);
    });
    
    const allSuccess = Object.values(results).every(result => result && result.success);
    
    console.log('=== targeting関連クエリテスト完了（単一クエリ版） ===');
    console.log(`全体結果: ${allSuccess ? '成功' : '一部失敗'}`);
    
    return {
      success: allSuccess,
      message: allSuccess ? 'targeting関連クエリテストに成功しました（通算+本日統計対応）' : 'クエリテストで失敗があります',
      results: results,
      timestamp: new Date()
    };
    
  } catch (error) {
    console.error('targeting関連クエリテストエラー:', error);
    return {
      success: false,
      error: error.toString(),
      results: results,
      timestamp: new Date()
    };
  }
}