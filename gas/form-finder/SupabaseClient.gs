/**
 * Supabase統合モジュール（Form Finder用）
 * GAS用Supabaseクライアント実装
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
 * リトライ機構付きバッチデータ取得（推奨）
 * @param {string} taskType タスクタイプ ('form_finder' など)
 * @param {number} batchSize バッチサイズ
 * @param {number} limit 最大取得件数制限（オプション）
 * @param {number} maxRetries 最大リトライ回数
 * @returns {Array} バッチデータ配列
 */
function getNextPendingBatchWithRetry(taskType, batchSize = 20, limit = null, maxRetries = 3) {
  let lastError = null;
  
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      console.log(`バッチデータ取得試行 ${attempt}/${maxRetries}: taskType=${taskType}, batchSize=${batchSize}`);
      
      const result = getNextPendingBatch(taskType, batchSize, limit);
      
      if (attempt > 1) {
        console.log(`バッチデータ取得成功 (${attempt}回目で成功): ${result.length}件取得`);
      }
      
      return result;
      
    } catch (error) {
      lastError = error;
      
      // SQLタイムアウトエラー（57014）の特別なハンドリング
      if (error.toString().includes('57014') || error.toString().includes('statement timeout')) {
        console.error(`SQLタイムアウト発生 (試行${attempt}/${maxRetries}): ${error.toString()}`);
      } else {
        console.error(`バッチデータ取得エラー (試行${attempt}/${maxRetries}): ${error.toString()}`);
      }
      
      if (attempt < maxRetries) {
        const delay = Math.pow(2, attempt) * 1000; // 指数バックオフ: 2秒, 4秒, 8秒
        console.log(`${delay}ms後にリトライします...`);
        Utilities.sleep(delay);
      }
    }
  }
  
  console.error(`バッチデータ取得最終失敗 (${maxRetries}回リトライ後): ${lastError.toString()}`);
  throw new Error(`${maxRetries}回リトライ後も失敗: ${lastError.toString()}`);
}

/**
 * 次の処理対象バッチデータを取得（Form Finder用）
 * form_finder_queuedフラグを使用した重複防止機構
 * @param {string} taskType タスクタイプ ('form_finder' など)
 * @param {number} batchSize バッチサイズ
 * @param {number} limit 最大取得件数制限（オプション）
 * @returns {Array} バッチデータ配列
 */
function getNextPendingBatch(taskType, batchSize = 20, limit = null) {
  try {
    const supabase = getSupabaseClient();
    
    // タスクタイプに基づいてクエリを構築
    let query = '';
    let params = {
      limit: limit ? Math.min(batchSize, limit) : batchSize
    };
    
    switch (taskType) {
      case 'form_finder':
        // form_found is null and company_url is not null and form_finder_queued is null
        // form_finder_queuedフラグで重複防止
        // company_name除去、ORDER BY除去でパフォーマンス最適化
        query = `${supabase.url}/rest/v1/companies?select=id,company_url&form_found=is.null&company_url=not.is.null&form_finder_queued=is.null&limit=${params.limit}`;
        break;
        
      default:
        throw new Error(`未対応のタスクタイプ: ${taskType}`);
    }
    
    console.log(`Supabaseクエリ実行: ${query}`);
    
    // HTTPリクエスト実行（タイムアウト設定付き）
    const response = UrlFetchApp.fetch(query, {
      method: 'GET',
      headers: {
        ...supabase.headers,
        'Prefer': 'count=none'  // カウント処理をスキップして負荷軽減
      },
      muteHttpExceptions: true,
      timeout: 25000  // 25秒でタイムアウト（デフォルトより短縮）
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode !== 200 && responseCode !== 206) {  // 206 Partial Contentも成功として扱う
      console.error(`Supabaseクエリエラー: ${responseCode} - ${responseText}`);
      throw new Error(`Supabaseクエリ失敗: ${responseCode}`);
    }
    
    const data = JSON.parse(responseText);
    
    if (!Array.isArray(data)) {
      console.error('Supabaseレスポンス形式エラー:', data);
      throw new Error('不正なレスポンス形式');
    }
    
    console.log(`取得件数: ${data.length}件`);
    
    // データをGitHub Actions用の形式に変換（ワーカーが期待する形式）
    // company_nameは除去してパフォーマンス向上
    const batchData = data.map(item => ({
      record_id: item.id,        // ✅ 正しいフィールド名：データベースのidカラム（レコード識別子）
      company_url: item.company_url || null  // GitHub Actionsワーカーが期待するフィールド
    }));
    
    // 重複防止：取得したレコードのform_finder_queuedをtrueに更新
    if (batchData.length > 0) {
      const recordIds = batchData.map(item => item.record_id);
      const updateResult = updateFormFinderQueued(recordIds, true);
      
      if (updateResult.success) {
        console.log(`重複防止処理完了: ${updateResult.updated_count}件をqueued状態に更新`);
      } else {
        console.error('重複防止処理失敗:', updateResult.error);
        // 重複防止処理が失敗した場合も続行（ログのみ出力）
      }
    }
    
    return batchData;
    
  } catch (error) {
    console.error('バッチデータ取得エラー:', error);
    throw error;
  }
}

/**
 * form_finder_queuedステータス更新関数
 * 重複防止のためのキューステータス管理
 * @param {Array} recordIds 更新対象のレコードIDリスト
 * @param {boolean|null} status 設定するステータス（true: キュー済み, null: 未処理）
 * @returns {Object} 更新結果
 */
function updateFormFinderQueued(recordIds, status = true) {
  try {
    if (!Array.isArray(recordIds) || recordIds.length === 0) {
      console.log('更新対象のレコードIDがありません');
      return { success: true, message: '更新対象なし', updated_count: 0 };
    }
    
    console.log(`form_finder_queuedステータス更新開始: ${recordIds.length}件, status=${status}`);
    
    const supabase = getSupabaseClient();
    
    // レコードIDリストを文字列に変換（IN句用）
    const idList = recordIds.map(id => parseInt(id)).filter(id => !isNaN(id) && id > 0);
    
    if (idList.length === 0) {
      throw new Error('有効なレコードIDがありません');
    }
    
    // IN句を使用した効率的な一括更新
    const updateQuery = `${supabase.url}/rest/v1/companies?id=in.(${idList.join(',')})`;
    const updateData = { form_finder_queued: status };
    
    console.log(`更新クエリ実行: ${updateQuery}`);
    
    const response = UrlFetchApp.fetch(updateQuery, {
      method: 'PATCH',
      headers: supabase.headers,
      payload: JSON.stringify(updateData),
      muteHttpExceptions: true,
      timeout: 25000
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode === 204) {
      console.log(`form_finder_queuedステータス更新成功: ${idList.length}件`);
      return { 
        success: true, 
        message: 'ステータス更新完了', 
        updated_count: idList.length,
        status: status
      };
    } else {
      console.error(`form_finder_queuedステータス更新失敗: ${responseCode} - ${responseText}`);
      return { 
        success: false, 
        error: `HTTP ${responseCode}: ${responseText}`,
        updated_count: 0
      };
    }
    
  } catch (error) {
    console.error('form_finder_queuedステータス更新エラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      updated_count: 0
    };
  }
}

/**
 * 処理状況統計を取得（Form Finder用）
 * RPC関数を使用した効率的な単一クエリ統計取得
 * @param {string} taskType タスクタイプ
 * @returns {Object} 統計情報
 */
function getProcessingStats(taskType = 'form_finder') {
  try {
    const supabase = getSupabaseClient();
    
    // RPC関数で効率的な統計取得
    const rpcQuery = `${supabase.url}/rest/v1/rpc/get_form_finder_stats`;
    
    console.log('RPC統計クエリ実行:', rpcQuery);
    
    const response = UrlFetchApp.fetch(rpcQuery, {
      method: 'POST',
      headers: supabase.headers,
      payload: JSON.stringify({}),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode === 200) {
      const data = JSON.parse(responseText);
      
      if (Array.isArray(data) && data.length > 0) {
        const statsRow = data[0];
        
        const stats = {
          total: statsRow.total_companies || 0,
          form_found: statsRow.form_found_count || 0,
          pending: statsRow.pending_count || 0,
          progress_rate: parseFloat(statsRow.progress_rate) || 0,
          // 非推奨フィールド（後方互換性のため保持）
          form_finder_queued: 0,  // form_finder_queuedは廃止
          not_queued: statsRow.pending_count || 0,
          queued_rate: 0,  // form_finder_queuedは廃止
          usedRpc: true  // RPC使用フラグ
        };
        
        console.log('RPC統計取得成功:', stats);
        return stats;
      } else {
        console.error('RPC統計レスポンス形式エラー:', data);
        throw new Error('RPC統計レスポンスが空または不正な形式');
      }
    } else {
      console.error(`RPC統計クエリ失敗: ${responseCode} - ${responseText}`);
      throw new Error(`RPC統計クエリエラー: ${responseCode}`);
    }
    
  } catch (error) {
    console.error('RPC統計取得エラー、フォールバック処理実行:', error);
    return getProcessingStatsFallback();
  }
}

/**
 * 統計取得のフォールバック処理（従来方式）
 * RPC関数が利用できない場合の代替処理
 * @returns {Object} 統計情報
 */
function getProcessingStatsFallback() {
  try {
    const supabase = getSupabaseClient();
    
    console.log('フォールバック統計処理開始');
    
    // 簡略化された統計取得（企業URL存在企業のみ対象）
    const baseQuery = `${supabase.url}/rest/v1/companies?select=id,form_found&company_url=not.is.null&limit=10000`;
    
    const response = UrlFetchApp.fetch(baseQuery, {
      method: 'GET',
      headers: supabase.headers,
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200 || responseCode === 206) {
      const data = JSON.parse(response.getContentText());
      
      if (Array.isArray(data)) {
        const total = data.length;
        const formFound = data.filter(item => item.form_found === true).length;
        const pending = data.filter(item => item.form_found === null).length;
        
        const stats = {
          total: total,
          form_found: formFound,
          pending: pending,
          progress_rate: total > 0 ? Math.round((formFound / total) * 100) : 0,
          // 非推奨フィールド（後方互換性のため保持）
          form_finder_queued: 0,
          not_queued: pending,
          queued_rate: 0,
          usedFallback: true  // フォールバック使用フラグ
        };
        
        console.log('フォールバック統計取得成功:', stats);
        
        if (total === 10000) {
          console.warn('データが10000件制限に達している可能性があります');
          stats.warning = 'データ件数制限到達の可能性';
        }
        
        return stats;
      } else {
        throw new Error('フォールバック統計レスポンス形式エラー');
      }
    } else {
      throw new Error(`フォールバック統計クエリエラー: ${responseCode}`);
    }
    
  } catch (error) {
    console.error('フォールバック統計処理エラー:', error);
    return {
      total: 0,
      form_found: 0,
      pending: 0,
      form_finder_queued: 0,
      not_queued: 0,
      progress_rate: 0,
      queued_rate: 0,
      error: error.toString()
    };
  }
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
    
    if (responseCode === 200 || responseCode === 206) {  // 206 Partial Contentも成功
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
 * 緊急時データリセット（注意深く使用）
 * @param {string} confirmationToken 確認トークン
 * @returns {Object} リセット結果
 */
function resetFormFinderStatus(confirmationToken) {
  if (confirmationToken !== 'RESET_FORM_FINDER_STATUS') {
    throw new Error('不正な確認トークンです');
  }
  
  try {
    console.log('Form Finder処理状況リセット実行');
    
    const supabase = getSupabaseClient();
    
    // 全レコードのform_finder_queued、form_found、form_urlをクリア
    const resetQuery = `${supabase.url}/rest/v1/companies`;
    const resetData = {
      form_finder_queued: null,
      form_found: null,
      form_url: null
    };
    
    const response = UrlFetchApp.fetch(resetQuery, {
      method: 'PATCH',
      headers: supabase.headers,
      payload: JSON.stringify(resetData),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 204) {
      console.log('Form Finder処理状況リセット完了');
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
 * デバッグ用：実際のSupabaseデータ確認（Form Finder用）
 * @returns {Array} サンプルデータ配列
 */
function debugSupabaseData() {
  try {
    console.log('=== Supabase Form Finderデータ確認デバッグ ===');
    
    const supabase = getSupabaseClient();
    
    // 基本的なデータ取得テスト
    const query = `${supabase.url}/rest/v1/companies?select=id,company_name,company_url,form_finder_queued,form_found,form_url&limit=10`;
    console.log(`デバッグクエリ実行: ${query}`);
    
    const response = UrlFetchApp.fetch(query, {
      method: 'GET',
      headers: {
        ...supabase.headers,
        'Prefer': 'count=exact'
      },
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    const headers = response.getHeaders();
    
    console.log(`レスポンスコード: ${responseCode}`);
    console.log(`レスポンスヘッダー:`, headers);
    console.log(`Content-Range: ${headers['content-range'] || headers['Content-Range'] || headers['CONTENT-RANGE'] || 'なし'}`);
    
    if (responseCode === 200 || responseCode === 206) {  // 206 Partial Contentも成功
      const data = JSON.parse(responseText);
      console.log(`取得データ件数: ${data.length}`);
      console.log(`サンプルデータ:`, data.slice(0, 3));
      
      // form_finder_queuedの状態を分析
      const queuedNullCount = data.filter(item => item.form_finder_queued === null).length;
      const queuedTrueCount = data.filter(item => item.form_finder_queued === true).length;
      const queuedFalseCount = data.filter(item => item.form_finder_queued === false).length;
      
      // form_foundの状態を分析
      const foundTrueCount = data.filter(item => item.form_found === true).length;
      const foundNullCount = data.filter(item => item.form_found === null).length;
      const foundFalseCount = data.filter(item => item.form_found === false).length;
      
      // form_urlの状態を分析
      const urlNullCount = data.filter(item => item.form_url === null).length;
      const urlHasValueCount = data.filter(item => item.form_url && item.form_url !== '').length;
      
      console.log(`form_finder_queued分析 (サンプル10件中):`);
      console.log(`- NULL (未処理): ${queuedNullCount}件`);
      console.log(`- TRUE (キュー済み): ${queuedTrueCount}件`);
      console.log(`- FALSE: ${queuedFalseCount}件`);
      
      console.log(`form_found分析 (サンプル10件中):`);
      console.log(`- TRUE (発見): ${foundTrueCount}件`);
      console.log(`- NULL (未処理): ${foundNullCount}件`);
      console.log(`- FALSE (未発見): ${foundFalseCount}件`);
      
      console.log(`form_url分析 (サンプル10件中):`);
      console.log(`- NULL: ${urlNullCount}件`);
      console.log(`- 値あり: ${urlHasValueCount}件`);
      
      return {
        success: true,
        totalSample: data.length,
        sampleData: data.slice(0, 3),
        formFinderQueuedAnalysis: {
          queuedNull: queuedNullCount,
          queuedTrue: queuedTrueCount,
          queuedFalse: queuedFalseCount
        },
        formFoundAnalysis: {
          foundTrue: foundTrueCount,
          foundNull: foundNullCount,
          foundFalse: foundFalseCount
        },
        formUrlAnalysis: {
          urlNull: urlNullCount,
          urlHasValue: urlHasValueCount
        },
        headers: headers,
        contentRange: headers['content-range'] || headers['Content-Range'] || headers['CONTENT-RANGE']
      };
    } else {
      console.error(`デバッグクエリ失敗: ${responseCode} - ${responseText}`);
      return {
        success: false,
        error: `HTTP ${responseCode}: ${responseText}`,
        headers: headers
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
 * form_finder_queuedステータスのみをリセット（部分的なリセット機能）
 * @param {string} confirmationToken 確認トークン
 * @returns {Object} リセット結果
 */
function resetFormFinderQueuedStatus(confirmationToken) {
  if (confirmationToken !== 'RESET_FORM_FINDER_QUEUED_ONLY') {
    throw new Error('不正な確認トークンです');
  }
  
  try {
    console.log('form_finder_queuedステータスのみリセット実行');
    
    const supabase = getSupabaseClient();
    
    // 全レコードのform_finder_queuedのみをクリア
    const resetQuery = `${supabase.url}/rest/v1/companies`;
    const resetData = {
      form_finder_queued: null
    };
    
    const response = UrlFetchApp.fetch(resetQuery, {
      method: 'PATCH',
      headers: supabase.headers,
      payload: JSON.stringify(resetData),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 204) {
      console.log('form_finder_queuedステータスのみリセット完了');
      return { success: true, message: 'form_finder_queuedリセット完了' };
    } else {
      const errorText = response.getContentText();
      console.error(`form_finder_queuedリセット失敗: ${responseCode} - ${errorText}`);
      return { success: false, error: `HTTP ${responseCode}: ${errorText}` };
    }
    
  } catch (error) {
    console.error('form_finder_queuedリセットエラー:', error);
    return { success: false, error: error.toString() };
  }
}