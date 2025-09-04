/**
 * Supabase統合モジュール
 * GAS用Supabaseクライアント実装
 * 
 * 【データベース最適化推奨事項】
 * パフォーマンス向上のため、以下のインデックス作成を推奨：
 * 
 * -- 複合インデックス (established_year, id)
 * CREATE INDEX CONCURRENTLY idx_companies_established_year_id 
 * ON companies (established_year, id) 
 * WHERE established_year IS NULL;
 * 
 * -- 単一インデックス (established_year) ※既存の場合は不要
 * CREATE INDEX CONCURRENTLY idx_companies_established_year 
 * ON companies (established_year) 
 * WHERE established_year IS NULL;
 * 
 * これにより、`established_year IS NULL`のクエリパフォーマンスが大幅に改善されます。
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
 * 次の処理対象バッチデータを取得（原子的操作版）
 * @param {string} taskType タスクタイプ ('fuma_detail' など)
 * @param {number} batchSize バッチサイズ
 * @param {number} limit 最大取得件数制限（オプション）
 * @returns {Array} バッチデータ配列
 */
function getNextPendingBatch(taskType, batchSize = 20, limit = null) {
  try {
    const supabase = getSupabaseClient();
    
    // タスクタイプに基づいてクエリを構築
    let selectQuery = '';
    let updateQuery = '';
    let params = {
      limit: limit ? Math.min(batchSize, limit) : batchSize
    };
    
    /**
     * タイムアウト発生時の段階的フォールバック機能
     * @param {number} currentBatchSize 現在のバッチサイズ
     * @returns {Array} バッチデータ配列
     */
    const attemptBatchWithFallback = (currentBatchSize) => {
      console.log(`フォールバック実行開始: バッチサイズ=${currentBatchSize}`);
      try {
        return executeBatchQuery(supabase, taskType, Math.min(currentBatchSize, params.limit));
      } catch (error) {
        console.log(`フォールバック内エラー検出: ${error.message}`);
        console.log(`エラータイプ: ${typeof error}, エラー詳細:`, error);
        
        // より広範囲のタイムアウト検出
        const isTimeout = error.message.includes('timeout') || 
                         error.message.includes('57014') ||
                         error.message.includes('statement_timeout') ||
                         error.message.includes('タイムアウト');
        
        if (isTimeout && currentBatchSize > CONFIG.MIN_BATCH_SIZE) {
          const newSize = Math.floor(currentBatchSize / 2);
          console.log(`タイムアウト検出！バッチサイズを${currentBatchSize}→${newSize}に縮小してリトライ`);
          return attemptBatchWithFallback(newSize);
        } else {
          console.log(`フォールバック条件不適合: timeout=${isTimeout}, size=${currentBatchSize}, minSize=${CONFIG.MIN_BATCH_SIZE}`);
        }
        throw error;
      }
    };
    
    // フォールバック機能を使用してバッチ処理を実行
    return attemptBatchWithFallback(batchSize);
    
  } catch (error) {
    console.error('バッチデータ取得エラー:', error);
    throw error;
  }
}

/**
 * バッチクエリ実行（内部関数）
 * @param {Object} supabase Supabaseクライアント
 * @param {string} taskType タスクタイプ
 * @param {number} currentBatchSize 現在のバッチサイズ
 * @returns {Array} バッチデータ配列
 */
function executeBatchQuery(supabase, taskType, currentBatchSize) {
    let selectQuery = '';
    
    switch (taskType) {
      case 'fuma_detail':
        // 必要最小限のフィールドのみ取得（転送量削減）
        // 重複処理防止: established_yearがnullかつfetch_detail_queuedがnullのレコードのみ取得
        selectQuery = `${supabase.url}/rest/v1/companies?select=id,company_name,detail_page&fetch_detail_queued=is.null&established_year=is.null&limit=${currentBatchSize}`;
        break;
        
      default:
        throw new Error(`未対応のタスクタイプ: ${taskType}`);
    }
    
    console.log(`最適化Supabaseクエリ実行 (バッチサイズ: ${currentBatchSize}): ${selectQuery}`);
    
    // データ取得（タイムアウト設定追加）
    const response = UrlFetchApp.fetch(selectQuery, {
      method: 'GET',
      headers: supabase.headers,
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode !== 200 && responseCode !== 206) {
      console.error(`Supabaseクエリエラー: ${responseCode} - ${responseText}`);
      // より堅牢なタイムアウトエラー判定
      const isTimeout = responseText.includes('timeout') || 
                       responseText.includes('57014') ||
                       responseText.includes('statement_timeout') ||
                       responseCode === 408;
      
      if (isTimeout) {
        throw new Error(`Supabaseクエリタイムアウト: ${responseCode}`);
      }
      throw new Error(`Supabaseクエリ失敗: ${responseCode}`);
    }
    
    const data = JSON.parse(responseText);
    
    if (!Array.isArray(data)) {
      console.error('Supabaseレスポンス形式エラー:', data);
      throw new Error('不正なレスポンス形式');
    }
    
    console.log(`取得件数: ${data.length}件`);
    
    if (data.length > 0) {
      // 重複防止：取得したレコードのfetch_detail_queuedをtrueに更新
      const recordIds = data.map(item => item.id);
      const updateResult = updateFetchDetailQueued(recordIds, true);
      
      if (updateResult.success) {
        console.log(`重複防止処理完了: ${updateResult.updated_count}件をqueued状態に更新`);
      } else {
        console.error('重複防止処理失敗:', updateResult.error);
        // 重複防止処理が失敗した場合も続行（ログのみ出力）
      }
    }
    
    // GitHub Actions用の形式に変換（最小限のデータのみ）
    const batchData = data.map(item => ({
      record_id: item.id,
      company_name: item.company_name || '',
      detail_page: item.detail_page || null,
      current_company_url: null  // 初回処理時は常にnull
    }));
    
    return batchData;
}

/**
 * fetch_detail_queuedステータス更新関数
 * 重複防止のためのキューステータス管理
 * @param {Array} recordIds 更新対象のレコードIDリスト
 * @param {boolean|null} status 設定するステータス（true: キュー済み, null: 未処理）
 * @returns {Object} 更新結果
 */
function updateFetchDetailQueued(recordIds, status = true) {
  try {
    if (!Array.isArray(recordIds) || recordIds.length === 0) {
      console.log('更新対象のレコードIDがありません');
      return { success: true, message: '更新対象なし', updated_count: 0 };
    }
    
    console.log(`fetch_detail_queuedステータス更新開始: ${recordIds.length}件, status=${status}`);
    
    const supabase = getSupabaseClient();
    
    // レコードIDリストを文字列に変換（IN句用）
    const idList = recordIds.map(id => parseInt(id)).filter(id => !isNaN(id) && id > 0);
    
    if (idList.length === 0) {
      throw new Error('有効なレコードIDがありません');
    }
    
    // IN句を使用した効率的な一括更新
    const updateQuery = `${supabase.url}/rest/v1/companies?id=in.(${idList.join(',')})`;
    const updateData = { fetch_detail_queued: status };
    
    console.log(`更新クエリ実行: ${updateQuery}`);
    
    const response = UrlFetchApp.fetch(updateQuery, {
      method: 'PATCH',
      headers: supabase.headers,
      payload: JSON.stringify(updateData),
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode === 204) {
      console.log(`fetch_detail_queuedステータス更新成功: ${idList.length}件`);
      return { 
        success: true, 
        message: 'ステータス更新完了', 
        updated_count: idList.length,
        status: status
      };
    } else {
      console.error(`fetch_detail_queuedステータス更新失敗: ${responseCode} - ${responseText}`);
      return { 
        success: false, 
        error: `HTTP ${responseCode}: ${responseText}`,
        updated_count: 0
      };
    }
    
  } catch (error) {
    console.error('fetch_detail_queuedステータス更新エラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      updated_count: 0
    };
  }
}


/**
 * 処理状況統計を取得（I/O最適化版）
 * @param {string} taskType タスクタイプ
 * @returns {Object} 統計情報
 */
function getProcessingStats(taskType = 'fuma_detail') {
  try {
    const supabase = getSupabaseClient();
    
    // RPC関数を使用してすべての統計を一度に取得
    const aggregateQuery = `${supabase.url}/rest/v1/rpc/get_processing_stats`;
    
    console.log('集約統計クエリ実行');
    const aggregateResponse = UrlFetchApp.fetch(aggregateQuery, {
      method: 'POST',
      headers: supabase.headers,
      payload: JSON.stringify({}),
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = aggregateResponse.getResponseCode();
    
    // RPC関数が利用可能な場合
    if (responseCode === 200) {
      const aggregateData = JSON.parse(aggregateResponse.getContentText());
      
      if (aggregateData && aggregateData.length > 0) {
        const stats = aggregateData[0];
        
        const optimizedStats = {
          total: stats.total_count || 0,
          completed: stats.completed_count || 0,
          pending: (stats.total_count || 0) - (stats.completed_count || 0),
          fetch_detail_queued: stats.queued_count || 0,
          not_queued: (stats.total_count || 0) - (stats.queued_count || 0),
          progress_rate: (stats.total_count || 0) > 0 ? Math.round(((stats.completed_count || 0) / stats.total_count) * 100) : 0,
          queue_rate: (stats.total_count || 0) > 0 ? Math.round(((stats.queued_count || 0) / stats.total_count) * 100) : 0,
          usedAggregateFunction: true
        };
        
        console.log('集約統計取得成功:', optimizedStats);
        return optimizedStats;
      }
    }
    
    // フォールバック: 最適化された並列クエリ
    console.log('RPC関数利用不可、最適化された並列クエリに切り替え');
    
    // 小さなlimitで高速化したContent-Rangeベースの統計
    const queries = [
      { name: 'total', query: `${supabase.url}/rest/v1/companies?select=id&limit=1` },
      { name: 'completed', query: `${supabase.url}/rest/v1/companies?select=id&established_year=not.is.null&limit=1` },
      { name: 'queued', query: `${supabase.url}/rest/v1/companies?select=id&fetch_detail_queued=is.true&limit=1` }
    ];
    
    const results = {};
    
    // UrlFetchApp.fetchAllで並列実行（GASの制約内で最適化）
    try {
      const requests = queries.map(queryInfo => ({
        url: queryInfo.query,
        method: 'GET',
        headers: {
          ...supabase.headers,
          'Prefer': 'count=exact'
        },
        muteHttpExceptions: true,
        timeout: CONFIG.TIMEOUT_MS
      }));
      
      const responses = UrlFetchApp.fetchAll(requests);
      
      queries.forEach((queryInfo, index) => {
        try {
          const response = responses[index];
          const headers = response.getHeaders();
          const contentRange = headers['content-range'] || headers['Content-Range'] || headers['CONTENT-RANGE'] || '0-0/0';
          const count = parseInt(contentRange.split('/')[1]) || 0;
          
          results[queryInfo.name] = count;
          
        } catch (error) {
          console.error(`統計取得エラー (${queryInfo.name}):`, error);
          results[queryInfo.name] = 0;
        }
      });
      
    } catch (batchError) {
      console.error('並列クエリエラー、順次実行にフォールバック:', batchError);
      
      // 並列実行に失敗した場合は順次実行にフォールバック
      for (const queryInfo of queries) {
        try {
          const response = UrlFetchApp.fetch(queryInfo.query, {
            method: 'GET',
            headers: {
              ...supabase.headers,
              'Prefer': 'count=exact'
            },
            muteHttpExceptions: true,
            timeout: CONFIG.TIMEOUT_MS
          });
          
          const headers = response.getHeaders();
          const contentRange = headers['content-range'] || headers['Content-Range'] || headers['CONTENT-RANGE'] || '0-0/0';
          const count = parseInt(contentRange.split('/')[1]) || 0;
          
          results[queryInfo.name] = count;
          
        } catch (error) {
          console.error(`統計取得エラー (${queryInfo.name}):`, error);
          results[queryInfo.name] = 0;
        }
      }
    }
    
    // 最終フォールバック: 段階的データ取得
    if (results.total === 0) {
      console.log('Content-Range取得失敗、段階的フォールバック処理実行');
      results = getFallbackStats(supabase);
    }
    
    // 統計情報の構築
    const stats = {
      total: results.total || 0,
      completed: results.completed || 0,
      pending: (results.total || 0) - (results.completed || 0),
      fetch_detail_queued: results.queued || 0,
      not_queued: (results.total || 0) - (results.queued || 0),
      progress_rate: (results.total || 0) > 0 ? Math.round(((results.completed || 0) / results.total) * 100) : 0,
      queue_rate: (results.total || 0) > 0 ? Math.round(((results.queued || 0) / results.total) * 100) : 0,
      usedOptimizedQuery: true
    };
    
    console.log('最適化統計:', stats);
    return stats;
    
  } catch (error) {
    console.error('処理統計取得エラー:', error);
    return {
      total: 0,
      completed: 0,
      pending: 0,
      fetch_detail_queued: 0,
      not_queued: 0,
      progress_rate: 0,
      queue_rate: 0,
      error: error.toString()
    };
  }
}

/**
 * フォールバック統計取得（段階的データ取得）
 * @param {Object} supabase Supabaseクライアント
 * @returns {Object} 統計結果
 */
function getFallbackStats(supabase) {
  const batchSize = 1000; // 段階的取得サイズ
  const results = { total: 0, completed: 0, queued: 0 };
  
  try {
    // 段階的に全データをチェック（最大10,000件まで）
    for (let offset = 0; offset < 10000; offset += batchSize) {
      const query = `${supabase.url}/rest/v1/companies?select=id,established_year,fetch_detail_queued&offset=${offset}&limit=${batchSize}`;
      
      const response = UrlFetchApp.fetch(query, {
        method: 'GET',
        headers: supabase.headers,
        muteHttpExceptions: true,
        timeout: CONFIG.TIMEOUT_MS
      });
      
      if (response.getResponseCode() === 200) {
        const data = JSON.parse(response.getContentText());
        
        if (data.length === 0) break; // データ終了
        
        results.total += data.length;
        results.completed += data.filter(item => item.established_year !== null && item.established_year !== '').length;
        results.queued += data.filter(item => item.fetch_detail_queued === true).length;
        
        if (data.length < batchSize) break; // 最後のバッチ
      } else {
        break;
      }
    }
    
    console.log(`フォールバック統計完了: 全体=${results.total}, 完了=${results.completed}（established_year基準）, キューイング済み=${results.queued}`);
    
  } catch (error) {
    console.error('フォールバック統計エラー:', error);
  }
  
  return results;
}

/**
 * 特定バッチの処理結果を確認
 * @param {string} batchId バッチID
 * @returns {Object} バッチ処理結果
 */
function getBatchResults(batchId) {
  // processing_logテーブルが存在しないため、処理結果確認機能は無効化
  console.log(`バッチ結果確認はサポートされていません: ${batchId}`);
  return null;
}

/**
 * データベース接続テスト（最適化版）
 * @returns {Object} テスト結果
 */
function testSupabaseConnection() {
  try {
    console.log('Supabase接続テスト開始（最適化版）');
    
    const supabase = getSupabaseClient();
    
    // 最小限のクエリでテスト（idフィールドのみ、limit=1）
    const testQuery = `${supabase.url}/rest/v1/companies?select=id&limit=1`;
    
    const response = UrlFetchApp.fetch(testQuery, {
      method: 'GET',
      headers: supabase.headers,
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200 || responseCode === 206) {
      const data = JSON.parse(response.getContentText());
      console.log(`Supabase接続テスト成功（データ存在: ${data.length > 0 ? 'あり' : 'なし'}）`);
      return { 
        success: true, 
        message: `Supabase接続成功（データ存在: ${data.length > 0 ? 'あり' : 'なし'}）`,
        optimized: true
      };
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
 * 緊急時データリセット（注意深く使用）
 * @param {string} confirmationToken 確認トークン
 * @returns {Object} リセット結果
 */
function resetProcessingStatus(confirmationToken) {
  if (confirmationToken !== 'RESET_PROCESSING_STATUS') {
    throw new Error('不正な確認トークンです');
  }
  
  try {
    console.log('処理状況リセット実行');
    
    const supabase = getSupabaseClient();
    
    // 全レコードのcompany_url、processing_error、fetch_detail_queuedをクリア（company_urlは変更せず）
    const resetQuery = `${supabase.url}/rest/v1/companies`;
    const resetData = {
      company_url: null,
      processing_error: null,
      fetch_detail_queued: null
    };
    
    const response = UrlFetchApp.fetch(resetQuery, {
      method: 'PATCH',
      headers: supabase.headers,
      payload: JSON.stringify(resetData),
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 204) {
      console.log('処理状況リセット完了');
      return { success: true, message: 'リセット完了' };
    } else {
      const errorText = response.getContentText();
      console.error(`リセット失敗: ${responseCode} - ${errorText}`);
      return { success: false, error: `HTTP ${responseCode}: ${errorText}` };
    }
    
  } catch (error) {
    console.error('リセットエラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * デバッグ用：実際のSupabaseデータ確認（最適化版）
 * @returns {Array} サンプルデータ配列
 */
function debugSupabaseData() {
  try {
    console.log('=== Supabaseデータ確認デバッグ（最適化版） ===');
    
    const supabase = getSupabaseClient();
    
    // 最小限のフィールドのみ取得してデータ転送量を削減
    const query = `${supabase.url}/rest/v1/companies?select=id,company_name,established_year,fetch_detail_queued&limit=5`;
    console.log(`最適化デバッグクエリ実行: ${query}`);
    
    const response = UrlFetchApp.fetch(query, {
      method: 'GET',
      headers: supabase.headers,  // count=exactを除去してI/O削減
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    console.log(`レスポンスコード: ${responseCode}`);
    
    if (responseCode === 200 || responseCode === 206) {
      const data = JSON.parse(responseText);
      console.log(`取得データ件数: ${data.length}`);
      console.log(`サンプルデータ（最初の2件）:`, data.slice(0, 2));
      
      // 効率的な状態分析（フィルター処理を1回で済ませる）
      const analysis = data.reduce((acc, item) => {
        // established_year分析
        if (item.established_year === null) acc.establishedYear.null++;
        else if (item.established_year === '') acc.establishedYear.empty++;
        else acc.establishedYear.hasValue++;
        
        // fetch_detail_queued分析
        if (item.fetch_detail_queued === true) acc.fetchDetailQueued.queuedTrue++;
        else if (item.fetch_detail_queued === null) acc.fetchDetailQueued.queuedNull++;
        else acc.fetchDetailQueued.queuedFalse++;
        
        return acc;
      }, {
        establishedYear: { null: 0, empty: 0, hasValue: 0 },
        fetchDetailQueued: { queuedTrue: 0, queuedNull: 0, queuedFalse: 0 }
      });
      
      console.log(`established_year分析 (サンプル${data.length}件中):`);
      console.log(`- NULL: ${analysis.establishedYear.null}件`);
      console.log(`- 空文字: ${analysis.establishedYear.empty}件`);
      console.log(`- 値あり: ${analysis.establishedYear.hasValue}件`);
      
      console.log(`fetch_detail_queued分析 (サンプル${data.length}件中):`);
      console.log(`- TRUE (キュー済み): ${analysis.fetchDetailQueued.queuedTrue}件`);
      console.log(`- NULL (未処理): ${analysis.fetchDetailQueued.queuedNull}件`);
      console.log(`- FALSE: ${analysis.fetchDetailQueued.queuedFalse}件`);
      
      return {
        success: true,
        totalSample: data.length,
        sampleData: data.slice(0, 2),  // サンプル数を削減
        establishedYearAnalysis: analysis.establishedYear,
        fetchDetailQueuedAnalysis: analysis.fetchDetailQueued,
        optimized: true
      };
    } else {
      console.error(`デバッグクエリ失敗: ${responseCode} - ${responseText}`);
      return {
        success: false,
        error: `HTTP ${responseCode}: ${responseText}`
      };
    }
    
  } catch (error) {
    console.error('デバッグ実行エラー:', error);
    return {
      success: false,
      error: error.toString()
    };
  }
}

/**
 * fetch_detail_queuedステータスのみをリセット（部分的なリセット機能）
 * @param {string} confirmationToken 確認トークン
 * @returns {Object} リセット結果
 */
function resetFetchDetailQueuedStatus(confirmationToken) {
  if (confirmationToken !== 'RESET_FETCH_DETAIL_QUEUED_ONLY') {
    throw new Error('不正な確認トークンです');
  }
  
  try {
    console.log('fetch_detail_queuedステータスのみリセット実行');
    
    const supabase = getSupabaseClient();
    
    // 全レコードのfetch_detail_queuedのみをクリア
    const resetQuery = `${supabase.url}/rest/v1/companies`;
    const resetData = {
      fetch_detail_queued: null
    };
    
    const response = UrlFetchApp.fetch(resetQuery, {
      method: 'PATCH',
      headers: supabase.headers,
      payload: JSON.stringify(resetData),
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 204) {
      console.log('fetch_detail_queuedステータスのみリセット完了');
      return { success: true, message: 'fetch_detail_queuedリセット完了' };
    } else {
      const errorText = response.getContentText();
      console.error(`fetch_detail_queuedリセット失敗: ${responseCode} - ${errorText}`);
      return { success: false, error: `HTTP ${responseCode}: ${errorText}` };
    }
    
  } catch (error) {
    console.error('fetch_detail_queuedリセットエラー:', error);
    return { success: false, error: error.toString() };
  }
}
