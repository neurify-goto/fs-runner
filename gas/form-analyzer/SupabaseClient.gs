/**
 * Supabase統合モジュール（Form Analyzer用）
 * GAS用Supabaseクライアント実装
 */

/**
 * リトライ設定を設定ファイルから取得
 * @returns {Object} リトライ設定
 */
function getRetryConfig() {
  try {
    // 設定ファイルをロード（Code.gsの関数を利用）
    const config = typeof loadConfig === 'function' ? loadConfig() : null;
    if (config && config.EXPONENTIAL_BACKOFF) {
      return {
        MAX_RETRIES: config.EXPONENTIAL_BACKOFF.max_retries || 3,
        INITIAL_DELAY: config.EXPONENTIAL_BACKOFF.initial_delay_ms || 1000,
        MAX_DELAY: config.EXPONENTIAL_BACKOFF.max_delay_ms || 30000,
        MULTIPLIER: config.EXPONENTIAL_BACKOFF.multiplier || 2
      };
    }
  } catch (error) {
    console.warn(`⚠️ 設定ファイル読み込み失敗、デフォルト設定使用: ${error}`);
  }
  
  // フォールバック設定
  return {
    MAX_RETRIES: 3,
    INITIAL_DELAY: 1000,
    MAX_DELAY: 30000,
    MULTIPLIER: 2
  };
}

/**
 * データベースエラーの分類
 * @param {string} errorMessage エラーメッセージ
 * @param {number} statusCode HTTPステータスコード
 * @returns {Object} エラー分類情報
 */
function classifyDatabaseError(errorMessage, statusCode) {
  const message = errorMessage.toLowerCase();
  
  // リトライ可能なエラーパターン
  const retryablePatterns = [
    'timeout',
    'connection',
    'deadlock',
    'lock_timeout',
    'serialization_failure',
    'temporary failure',
    'service unavailable'
  ];
  
  // メモリ不足や設定エラーなどの致命的エラー
  const fatalPatterns = [
    'out of memory',
    'configuration error',
    'authentication',
    'permission denied',
    'invalid api key'
  ];
  
  // パターンマッチング
  const isRetryable = retryablePatterns.some(pattern => message.includes(pattern)) ||
                     [429, 500, 502, 503, 504, 408].includes(statusCode);
                     
  const isFatal = fatalPatterns.some(pattern => message.includes(pattern)) ||
                 [401, 403].includes(statusCode);
  
  return {
    isRetryable: isRetryable && !isFatal,
    isFatal: isFatal,
    category: isFatal ? 'FATAL' : (isRetryable ? 'RETRYABLE' : 'CLIENT_ERROR'),
    shouldLog: true
  };
}

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
 * リトライ機能付きSupabase HTTPリクエスト実行
 * @param {string} url リクエストURL
 * @param {Object} options UrlFetchAppのオプション
 * @param {string} operation 操作名（ログ用）
 * @returns {Object} レスポンスオブジェクトまたはエラー情報
 */
function supabaseRequestWithRetry(url, options = {}, operation = 'Supabase操作') {
  const retryConfig = getRetryConfig();
  
  for (let attempt = 1; attempt <= retryConfig.MAX_RETRIES; attempt++) {
    try {
      const response = UrlFetchApp.fetch(url, {
        ...options,
        muteHttpExceptions: true
      });
      
      const responseCode = response.getResponseCode();
      const responseText = response.getContentText();
      
      // 成功レスポンス
      if (responseCode >= 200 && responseCode < 300) {
        return {
          success: true,
          response: response,
          data: responseText ? JSON.parse(responseText) : null,
          status_code: responseCode
        };
      }
      
      // データベースエラーの分類
      const errorClassification = classifyDatabaseError(responseText, responseCode);
      
      if (errorClassification.isRetryable && attempt < retryConfig.MAX_RETRIES) {
        const baseDelay = Math.min(
          retryConfig.INITIAL_DELAY * Math.pow(retryConfig.MULTIPLIER, attempt - 1),
          retryConfig.MAX_DELAY
        );
        
        // ジッター追加でデッドロック回避（±25%のランダム要素）
        const jitter = Math.floor(baseDelay * 0.25 * (Math.random() - 0.5));
        const delay = Math.max(100, baseDelay + jitter);
        
        console.warn(`${operation} リトライ (${attempt}/${retryConfig.MAX_RETRIES}): HTTP ${responseCode} [${errorClassification.category}] - ${delay}ms待機`);
        
        // メモリ使用量の監視（警告レベル）
        if (attempt >= Math.ceil(retryConfig.MAX_RETRIES * 0.8)) {
          console.warn(`⚠️ 高頻度リトライ検出: ${operation} - メモリ使用量に注意`);
        }
        
        Utilities.sleep(delay);
        continue;
      }
      
      // リトライしないエラー
      if (errorClassification.isFatal) {
        console.error(`❌ 致命的エラー検出 (${operation}): HTTP ${responseCode} - ${responseText}`);
      }
      
      return {
        success: false,
        error: `HTTP ${responseCode}: ${responseText}`,
        status_code: responseCode,
        retry_attempts: attempt,
        error_category: errorClassification.category
      };
      
    } catch (error) {
      // 接続エラーや例外の場合
      const errorClassification = classifyDatabaseError(error.toString(), 0);
      
      if (errorClassification.isRetryable && attempt < retryConfig.MAX_RETRIES) {
        const baseDelay = Math.min(
          retryConfig.INITIAL_DELAY * Math.pow(retryConfig.MULTIPLIER, attempt - 1),
          retryConfig.MAX_DELAY
        );
        
        // ジッター追加でデッドロック回避（±25%のランダム要素）
        const jitter = Math.floor(baseDelay * 0.25 * (Math.random() - 0.5));
        const delay = Math.max(100, baseDelay + jitter);
        
        console.warn(`${operation} 接続エラーリトライ (${attempt}/${retryConfig.MAX_RETRIES}): ${error} [${errorClassification.category}] - ${delay}ms待機`);
        Utilities.sleep(delay);
        continue;
      }
      
      // リトライしない例外またはリトライ上限に達した場合
      return {
        success: false,
        error: error.toString(),
        retry_attempts: attempt
      };
    }
  }
  
  // この行に到達することはないが、安全のため
  return {
    success: false,
    error: 'リトライ処理で予期しないエラーが発生しました',
    retry_attempts: retryConfig.MAX_RETRIES
  };
}

/**
 * 次の処理対象バッチデータを取得（Form Analyzer用）
 * @param {string} taskType タスクタイプ ('form_analyzer' など)
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
      case 'form_analyzer':
        // 第1優先：instruction_jsonがnullの企業を取得（負荷軽減版）
        // ORDER BYを除去し、prohibition_detected条件を最適化
        query = `${supabase.url}/rest/v1/companies?select=id,company_name,form_url,company_url&form_url=not.is.null&instruction_json=is.null&form_analyzer_queued=is.null&prohibition_detected=not.is.true&limit=${params.limit}`;
        break;
        
      default:
        throw new Error(`未対応のタスクタイプ: ${taskType}`);
    }
    
    console.log(`Supabaseクエリ実行: ${query}`);
    
    // リトライ機能付きHTTPリクエスト実行（statement timeout対策）
    const result = supabaseRequestWithRetry(query, {
      method: 'GET',
      headers: supabase.headers
    }, `第1優先クエリ (instruction_json=null)`);
    
    if (!result.success) {
      console.error(`第1優先Supabaseクエリエラー: ${result.error}`);
      throw new Error(`第1優先Supabaseクエリ失敗: ${result.error}`);
    }
    
    let data = result.data;
    
    if (!Array.isArray(data)) {
      console.error('Supabaseレスポンス形式エラー:', data);
      throw new Error('不正なレスポンス形式');
    }
    
    console.log(`第1優先取得件数: ${data.length}件`);
    
    // 第1優先で十分な数が取得できなかった場合、第2優先を実行
    if (data.length < batchSize) {
      const remainingSize = batchSize - data.length;
      
      // 第2優先：instruction_valid = falseの企業を取得（負荷軽減版）
      // ORDER BYを除去し、prohibition_detected条件を最適化
      const query2 = `${supabase.url}/rest/v1/companies?select=id,company_name,form_url,company_url&form_url=not.is.null&instruction_valid=eq.false&form_analyzer_queued=is.null&prohibition_detected=not.is.true&limit=${Math.min(remainingSize, 50)}`;
      
      console.log(`第2優先クエリ実行: ${query2}`);
      
      // リトライ機能付きクエリ実行に変更（statement timeout対策）
      const result2 = supabaseRequestWithRetry(query2, {
        method: 'GET',
        headers: supabase.headers
      }, `第2優先クエリ (instruction_valid=false)`);
      
      if (result2.success) {
        const data2 = result2.data;
        if (Array.isArray(data2)) {
          data = data.concat(data2);
          console.log(`第2優先取得件数: ${data2.length}件 (合計: ${data.length}件)`);
        }
      } else {
        console.error(`第2優先クエリエラー: ${result2.error}`);
        // 第2優先でエラーが発生しても第1優先の結果は返す
      }
    }
    
    if (data.length > 0) {
      // 取得したレコードのform_analyzer_queuedをtrueに更新（重複処理回避）
      const recordIds = data.map(item => item.id);
      const updateResult = updateFormAnalyzerQueued(recordIds, true);
      
      if (updateResult.success) {
        console.log(`form_analyzer_queuedステータス更新完了: ${recordIds.length}件`);
      } else {
        console.error(`form_analyzer_queuedステータス更新失敗: ${updateResult.error}`);
        // エラーが発生した場合は処理を中止
        throw new Error(`ステータス更新エラー: ${updateResult.error}`);
      }
    }
    
    // デバッグ: 抽出したレコードの詳細情報をログ出力
    console.log('=== 抽出レコード詳細情報 ===');
    console.log(`総抽出件数: ${data.length}件`);
    
    if (data.length > 0) {
      console.log('最初の3件のサンプルデータ:');
      data.slice(0, 3).forEach((item, index) => {
        console.log(`[${index + 1}] id: ${item.id} (type: ${typeof item.id})`);
        console.log(`    company_name: ${item.company_name}`);
        console.log(`    form_url: ${item.form_url}`);
        console.log(`    company_url: ${item.company_url}`);
        console.log(`    Raw item keys: ${Object.keys(item).join(', ')}`);
        console.log(`    Raw item:`, item);
      });
      
      // idフィールドの検証
      const idsAnalysis = data.map(item => item.id);
      const nullIds = idsAnalysis.filter(id => id == null);
      const undefinedIds = idsAnalysis.filter(id => id === undefined);
      const validIds = idsAnalysis.filter(id => id != null && id !== undefined);
      
      console.log('ID分析:');
      console.log(`- 有効なID: ${validIds.length}件`);
      console.log(`- NULLのID: ${nullIds.length}件`);
      console.log(`- undefinedのID: ${undefinedIds.length}件`);
      console.log(`- 有効IDのサンプル: [${validIds.slice(0, 5).join(', ')}]`);
    } else {
      console.log('抽出されたレコードがありません');
    }
    
    // データをGitHub Actions用の形式に変換（ワーカーが期待する形式）
    const batchData = data.map((item, index) => {
      const mappedItem = {
        record_id: item.id,           // GitHub Actionsワーカーが期待するフィールド名
        company_name: item.company_name,
        form_url: item.form_url,
        company_url: item.company_url || null
      };
      
      // record_idがnull/undefinedの場合は警告出力
      if (mappedItem.record_id == null || mappedItem.record_id === undefined) {
        console.warn(`警告: [${index + 1}] record_idが無効です - item.id: ${item.id} (type: ${typeof item.id})`);
        console.warn(`該当アイテム:`, item);
      }
      
      return mappedItem;
    });
    
    // 最終結果の検証
    console.log(`変換後のbatchData件数: ${batchData.length}件`);
    const invalidRecordIds = batchData.filter(item => item.record_id == null || item.record_id === undefined);
    if (invalidRecordIds.length > 0) {
      console.error(`❌ 重大エラー: ${invalidRecordIds.length}件のrecord_idが無効です`);
      console.error('無効なrecord_idを持つアイテム:', invalidRecordIds.slice(0, 3));
    } else {
      console.log(`✅ 全${batchData.length}件のrecord_idが有効です`);
    }
    
    return batchData;
    
  } catch (error) {
    console.error('バッチデータ取得エラー:', error);
    throw error;
  }
}

/**
 * バルク更新用の汎用メソッド（I/O負荷軽減版 + セキュリティ強化 + Performance Monitoring）
 * @param {Array} updateRecords 更新レコード配列 [{id, field1, field2, ...}, ...]
 * @param {string} tableName テーブル名（デフォルト: 'companies'）
 * @param {number} batchSize バッチサイズ（デフォルト: 100）
 * @returns {Object} 更新結果（パフォーマンスメトリクス付き）
 */
function bulkUpdateRecords(updateRecords, tableName = 'companies', batchSize = 100) {
  const startTime = new Date().getTime(); // 📊 Performance Monitoring
  
  try {
    // 🔒 Input Validation - セキュリティ強化
    
    // 1. updateRecords検証
    if (!updateRecords || !Array.isArray(updateRecords) || updateRecords.length === 0) {
      return { success: true, updated_count: 0, message: '更新対象なし' };
    }
    
    // 2. tableName SQL injection対策
    if (typeof tableName !== 'string') {
      throw new Error(`❌ Security: Invalid tableName type: ${typeof tableName}`);
    }
    
    // テーブル名の正規表現検証（英数字とアンダースコアのみ許可）
    const tableNamePattern = /^[a-zA-Z_][a-zA-Z0-9_]*$/;
    if (!tableNamePattern.test(tableName)) {
      throw new Error(`❌ Security: Invalid tableName format: ${tableName}. Only alphanumeric and underscore allowed.`);
    }
    
    // 許可されたテーブル名のホワイトリスト
    const allowedTables = ['companies', 'batch_request'];
    if (!allowedTables.includes(tableName)) {
      throw new Error(`❌ Security: Unauthorized table access: ${tableName}. Allowed tables: ${allowedTables.join(', ')}`);
    }
    
    // 3. batchSize境界値チェック
    if (typeof batchSize !== 'number' || batchSize <= 0 || batchSize > 1000) {
      throw new Error(`❌ Validation: Invalid batchSize: ${batchSize}. Must be number between 1-1000.`);
    }
    
    // 4. updateRecords構造検証（設定ファイルベースのメモリ制限）
    const config = getRetryConfig(); 
    const maxRecords = (typeof loadConfig === 'function' && loadConfig().BATCH_PROCESSING) ? 
                      loadConfig().batch_processing.max_memory_limit_records || 5000 : 5000;
    
    if (updateRecords.length > maxRecords) {
      console.error(`🚨 Memory Limit Warning: ${updateRecords.length}件のレコード処理要求（上限: ${maxRecords}件）`);
      console.warn(`⚠️ メモリ使用量が制限値に近づいています - chunking処理を検討してください`);
      throw new Error(`❌ Validation: Too many records: ${updateRecords.length}. Maximum allowed: ${maxRecords}`);
    }
    
    // メモリ使用量の警告ログ（80%到達時）
    const memoryWarningThreshold = Math.floor(maxRecords * 0.8);
    if (updateRecords.length > memoryWarningThreshold) {
      console.warn(`⚠️ Memory Warning: ${updateRecords.length}/${maxRecords}件 (${Math.round(updateRecords.length/maxRecords*100)}%) - メモリ制限に近づいています`);
    }
    
    // 各レコードの基本構造検証
    for (let i = 0; i < Math.min(updateRecords.length, 10); i++) { // 最初の10件のみチェック
      const record = updateRecords[i];
      if (!record || typeof record !== 'object' || !record.id) {
        throw new Error(`❌ Validation: Invalid record structure at index ${i}. Must have 'id' field.`);
      }
      if (typeof record.id !== 'number' && typeof record.id !== 'string') {
        throw new Error(`❌ Validation: Invalid id type at index ${i}: ${typeof record.id}. Must be number or string.`);
      }
    }
    
    console.log(`✅ Input Validation完了: ${updateRecords.length}件のレコード, テーブル: ${tableName}, バッチサイズ: ${batchSize}`);
    
    // バリデーション完了後の処理継続...
    
    const supabase = getSupabaseClient();
    let totalUpdated = 0;
    let errorCount = 0;
    
    // 📊 Performance Monitoring 初期化
    const performanceMetrics = {
      start_time: startTime,
      batch_count: Math.ceil(updateRecords.length / batchSize),
      total_records: updateRecords.length,
      batch_timings: [],
      api_calls: 0,
      total_wait_time: 0
    };
    
    console.log(`📊 バルク更新開始: ${updateRecords.length}件 (${tableName}テーブル, ${performanceMetrics.batch_count}バッチ)`);
    
    // バッチに分割して処理（Supabase APIの制限対策 + Performance Monitoring）
    for (let i = 0; i < updateRecords.length; i += batchSize) {
      const batchStartTime = new Date().getTime();
      const batch = updateRecords.slice(i, i + batchSize);
      
      try {
        // upsert操作でバルク更新
        const upsertQuery = `${supabase.url}/rest/v1/${tableName}?on_conflict=id`;
        const result = supabaseRequestWithRetry(upsertQuery, {
          method: 'POST',
          headers: {
            ...supabase.headers,
            'Prefer': 'resolution=merge-duplicates'
          },
          payload: JSON.stringify(batch)
        }, `${tableName}バルク更新 (${i + 1}～${i + batch.length}件)`);
        
        performanceMetrics.api_calls++; // 📊 API呼び出し回数カウント
        
        const batchEndTime = new Date().getTime();
        const batchDuration = batchEndTime - batchStartTime;
        
        if (result.success) {
          totalUpdated += batch.length;
          console.log(`📊 バッチ ${Math.floor(i / batchSize) + 1}: ${batch.length}件更新完了 (${batchDuration}ms)`);
        } else {
          errorCount += batch.length;
          console.error(`📊 バッチ ${Math.floor(i / batchSize) + 1} 更新失敗: ${result.error} (${batchDuration}ms)`);
        }
        
        // 📊 バッチタイミング記録
        performanceMetrics.batch_timings.push({
          batch_index: Math.floor(i / batchSize) + 1,
          records_count: batch.length,
          duration_ms: batchDuration,
          success: result.success
        });
        
        // 次のバッチ処理前に待機（レート制限・デッドロック回避）
        if (i + batchSize < updateRecords.length) {
          // ランダムな待機時間でデッドロック回避（100-500ms）
          const randomDelay = 100 + Math.floor(Math.random() * 400);
          const waitStartTime = new Date().getTime();
          Utilities.sleep(randomDelay);
          performanceMetrics.total_wait_time += randomDelay; // 📊 待機時間累計
        }
        
      } catch (batchError) {
        errorCount += batch.length;
        console.error(`バッチ ${Math.floor(i / batchSize) + 1} 処理エラー:`, batchError);
      }
    }
    
    const endTime = new Date().getTime();
    const totalDuration = endTime - startTime;
    
    // 📊 Performance Metrics 完成
    performanceMetrics.end_time = endTime;
    performanceMetrics.total_duration = totalDuration;
    performanceMetrics.actual_processing_time = totalDuration - performanceMetrics.total_wait_time;
    performanceMetrics.throughput_records_per_sec = totalUpdated > 0 ? Math.round((totalUpdated * 1000) / performanceMetrics.actual_processing_time) : 0;
    performanceMetrics.avg_batch_duration = performanceMetrics.batch_timings.length > 0 ? 
      Math.round(performanceMetrics.batch_timings.reduce((sum, batch) => sum + batch.duration_ms, 0) / performanceMetrics.batch_timings.length) : 0;
    
    // 📊 詳細パフォーマンスログ出力
    console.log(`📊 ========== バルク更新パフォーマンス統計 ==========`);
    console.log(`📊 処理結果: 成功=${totalUpdated}件, 失敗=${errorCount}件`);
    console.log(`📊 総実行時間: ${totalDuration}ms (${Math.round(totalDuration/1000)}秒)`);
    console.log(`📊 実処理時間: ${performanceMetrics.actual_processing_time}ms (待機除外)`);
    console.log(`📊 待機時間: ${performanceMetrics.total_wait_time}ms`);
    console.log(`📊 スループット: ${performanceMetrics.throughput_records_per_sec}件/秒`);
    console.log(`📊 API呼び出し数: ${performanceMetrics.api_calls}回`);
    console.log(`📊 平均バッチ時間: ${performanceMetrics.avg_batch_duration}ms`);
    console.log(`📊 バッチサイズ効率性: ${Math.round((totalUpdated / performanceMetrics.api_calls) * 100) / 100}件/API呼び出し`);
    
    // 🔄 Cache Coherency: バルク更新完了時に統計キャッシュを無効化
    let cacheInvalidated = false;
    if (totalUpdated > 0) {
      const cacheResult = invalidateStatsCache('form_analyzer');
      if (cacheResult.success) {
        cacheInvalidated = true;
        console.log(`🔄 バルク更新に伴う統計キャッシュ無効化完了: ${tableName}テーブル`);
      } else {
        console.warn(`🔄 統計キャッシュ無効化失敗（処理は継続）: ${cacheResult.error}`);
      }
    }
    
    return {
      success: errorCount === 0,
      updated_count: totalUpdated,
      error_count: errorCount,
      total_records: updateRecords.length,
      cache_invalidated: cacheInvalidated,
      performance_metrics: performanceMetrics  // 📊 パフォーマンスメトリクス追加
    };
    
  } catch (error) {
    console.error('バルク更新エラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      updated_count: 0
    };
  }
}

/**
 * form_analyzer_queuedステータスを更新（バルク版）
 * @param {Array} recordIds 更新対象のレコードIDリスト
 * @param {boolean} status 設定するステータス（true: キュー済み, null: 未処理）
 * @returns {Object} 更新結果
 */
function updateFormAnalyzerQueued(recordIds, status = true) {
  try {
    if (!recordIds || recordIds.length === 0) {
      return { success: false, error: 'レコードIDが指定されていません' };
    }
    
    console.log(`form_analyzer_queuedステータス更新実行: ${recordIds.length}件 -> ${status}`);
    
    // analyzer_queued_atカラム存在確認
    const hasAnalyzerQueuedAt = checkAnalyzerQueuedAtColumnExists();
    
    // バルク更新用のレコード配列を構築
    const updateRecords = recordIds.map(id => {
      const record = {
        id: id,
        form_analyzer_queued: status
      };
      
      // analyzer_queued_atも同時に更新（カラムが存在する場合のみ）
      if (hasAnalyzerQueuedAt) {
        if (status === true) {
          // キューイング時は現在時刻を設定
          record.analyzer_queued_at = new Date().toISOString();
        } else if (status === null) {
          // リセット時は時刻もクリア
          record.analyzer_queued_at = null;
        }
      }
      
      return record;
    });
    
    // バルク更新を実行
    const result = bulkUpdateRecords(updateRecords, 'companies', 50);
    
    if (result.success) {
      console.log(`form_analyzer_queuedバルク更新成功: ${result.updated_count}件`);
      return { 
        success: true, 
        updated_count: result.updated_count,
        record_ids: recordIds 
      };
    } else {
      console.error(`form_analyzer_queuedバルク更新失敗: ${result.error}`);
      return { 
        success: false, 
        error: result.error,
        record_ids: recordIds
      };
    }
    
  } catch (error) {
    console.error('form_analyzer_queuedステータス更新エラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      record_ids: recordIds 
    };
  }
}

/**
 * 📊 統計情報キャッシュ設定を設定ファイルから取得
 * @returns {Object} キャッシュ設定
 */
function getCacheConfig() {
  try {
    const config = typeof loadConfig === 'function' ? loadConfig() : null;
    if (config && config.CACHE_TTL_MINUTES) {
      return {
        CACHE_DURATION: config.CACHE_TTL_MINUTES * 60 * 1000,
        CACHE_KEY_PREFIX: 'form_analyzer_stats_',
        JITTER_MAX_MS: 1000  // レースコンディション対策用ジッター
      };
    }
  } catch (error) {
    console.warn(`⚠️ キャッシュ設定読み込み失敗: ${error}`);
  }
  
  // フォールバック設定
  return {
    CACHE_DURATION: 5 * 60 * 1000,
    CACHE_KEY_PREFIX: 'form_analyzer_stats_',
    JITTER_MAX_MS: 1000
  };
}

/**
 * 統計情報キャッシュのキー生成
 * @param {string} taskType タスクタイプ
 * @returns {string} キャッシュキー
 */
function getStatsCacheKey(taskType) {
  const config = getCacheConfig();
  return `${config.CACHE_KEY_PREFIX}${taskType}`;
}

/**
 * 統計情報をキャッシュから取得
 * @param {string} taskType タスクタイプ
 * @returns {Object|null} キャッシュされた統計情報または null
 */
function getCachedStats(taskType) {
  try {
    const config = getCacheConfig();
    const cache = CacheService.getScriptCache();
    const cacheKey = getStatsCacheKey(taskType);
    
    // レースコンディション対策: 小さなランダム遅延
    const jitter = Math.floor(Math.random() * 50); // 0-50msのジッター
    if (jitter > 25) {
      Utilities.sleep(jitter - 25);
    }
    
    const cachedData = cache.get(cacheKey);
    
    if (cachedData) {
      const parsedData = JSON.parse(cachedData);
      const now = new Date().getTime();
      
      // TTLチェック
      if (parsedData.timestamp && (now - parsedData.timestamp) < config.CACHE_DURATION) {
        console.log(`🎯 Cache Hit: ${taskType}統計情報をキャッシュから取得`);
        
        const cachedStats = { ...parsedData.stats, cached: true };
        return cachedStats;
      } else {
        console.log(`⏰ Cache Expired: ${taskType}統計情報のキャッシュが期限切れ`);
        // 期限切れキャッシュを即座削除（レースコンディション対策）
        cache.remove(cacheKey);
      }
    }
    
    return null;
  } catch (error) {
    console.error('統計キャッシュ取得エラー:', error);
    return null;
  }
}

/**
 * 統計情報をキャッシュに保存
 * @param {string} taskType タスクタイプ
 * @param {Object} stats 統計情報
 */
function setCachedStats(taskType, stats) {
  try {
    const config = getCacheConfig();
    const cache = CacheService.getScriptCache();
    const cacheKey = getStatsCacheKey(taskType);
    
    // レースコンディション対策: キャッシュ更新前に小さな遅延
    const jitter = Math.floor(Math.random() * config.JITTER_MAX_MS * 0.1); // 0-100msのジッター
    if (jitter > 50) {
      Utilities.sleep(jitter - 50);
    }
    
    const cacheData = {
      timestamp: new Date().getTime(),
      stats: stats,
      process_id: Utilities.getUuid().substring(0, 8)  // レースコンディション検出用
    };
    
    // 21600秒（6時間）でキャッシュ期限切れ
    cache.put(cacheKey, JSON.stringify(cacheData), 21600);
    console.log(`💾 Cache Set: ${taskType}統計情報をキャッシュに保存 [PID: ${cacheData.process_id}]`);
  } catch (error) {
    console.error('統計キャッシュ保存エラー:', error);
  }
}

/**
 * 🔄 統計キャッシュを無効化（Cache Coherency改善）
 * @param {string} taskType タスクタイプ
 */
function invalidateStatsCache(taskType = 'form_analyzer') {
  try {
    const cache = CacheService.getScriptCache();
    const cacheKey = getStatsCacheKey(taskType);
    cache.remove(cacheKey);
    console.log(`🔄 統計キャッシュを無効化: ${taskType}`);
    return { success: true };
  } catch (error) {
    console.error(`🔄 統計キャッシュ無効化エラー: ${error}`);
    return { success: false, error: error.toString() };
  }
}

/**
 * 🔄 複数タスクタイプの統計キャッシュを一括無効化
 * @param {Array} taskTypes タスクタイプ配列
 */
function invalidateMultipleStatsCache(taskTypes = ['form_analyzer']) {
  try {
    const cache = CacheService.getScriptCache();
    const cacheKeys = taskTypes.map(taskType => getStatsCacheKey(taskType));
    
    // GASのremoveAllは配列を受け取れないため、個別に削除
    let successCount = 0;
    let errorCount = 0;
    
    cacheKeys.forEach((cacheKey, index) => {
      try {
        cache.remove(cacheKey);
        successCount++;
        console.log(`🔄 統計キャッシュ無効化: ${taskTypes[index]}`);
      } catch (error) {
        errorCount++;
        console.error(`🔄 統計キャッシュ無効化エラー (${taskTypes[index]}): ${error}`);
      }
    });
    
    console.log(`🔄 統計キャッシュ一括無効化完了: 成功=${successCount}, 失敗=${errorCount}`);
    return { success: errorCount === 0, success_count: successCount, error_count: errorCount };
  } catch (error) {
    console.error(`🔄 統計キャッシュ一括無効化エラー: ${error}`);
    return { success: false, error: error.toString() };
  }
}

/**
 * 処理状況統計を取得（キャッシュ機能付き）
 * @param {string} taskType タスクタイプ
 * @param {boolean} forceRefresh キャッシュを無視して最新データを取得
 * @returns {Object} 統計情報
 */
function getProcessingStats(taskType = 'form_analyzer', forceRefresh = false) {
  try {
    // キャッシュから統計情報を取得（強制リフレッシュ時以外）
    if (!forceRefresh) {
      const cachedStats = getCachedStats(taskType);
      if (cachedStats) {
        return cachedStats;
      }
    }
    
    console.log(`統計情報を新規取得中: ${taskType}`);
    const supabase = getSupabaseClient();
    
    // フォーム有り企業総数取得（Content-Rangeヘッダー使用）
    const totalQuery = `${supabase.url}/rest/v1/companies?select=id&form_url=not.is.null&limit=0`;
    const totalResponse = UrlFetchApp.fetch(totalQuery, {
      method: 'GET',
      headers: {
        ...supabase.headers,
        'Prefer': 'count=exact'  // Content-Rangeヘッダーを強制取得
      },
      muteHttpExceptions: true
    });
    
    // デバッグ: ヘッダー情報をログ出力
    const totalHeaders = totalResponse.getHeaders();
    console.log('フォーム有り企業統計 Response Headers:', totalHeaders);
    
    // Content-Rangeヘッダーから総件数を取得
    const totalContentRange = totalHeaders['content-range'] || totalHeaders['Content-Range'] || totalHeaders['CONTENT-RANGE'] || '0-0/0';
    console.log('フォーム有り企業統計 Content-Range:', totalContentRange);
    const totalCount = parseInt(totalContentRange.split('/')[1]) || 0;
    
    // 指示書生成済み統計取得（instruction_json is not null and instruction_valid is not false）
    const generatedQuery = `${supabase.url}/rest/v1/companies?select=id&form_url=not.is.null&instruction_json=not.is.null&not.instruction_valid=eq.false&limit=0`;
    const generatedResponse = UrlFetchApp.fetch(generatedQuery, {
      method: 'GET',
      headers: {
        ...supabase.headers,
        'Prefer': 'count=exact'  // Content-Rangeヘッダーを強制取得
      },
      muteHttpExceptions: true
    });
    
    // デバッグ: ヘッダー情報をログ出力
    const generatedHeaders = generatedResponse.getHeaders();
    console.log('指示書生成済み統計 Response Headers:', generatedHeaders);
    
    // Content-Rangeヘッダーから生成済み件数を取得
    const generatedContentRange = generatedHeaders['content-range'] || generatedHeaders['Content-Range'] || generatedHeaders['CONTENT-RANGE'] || '0-0/0';
    console.log('指示書生成済み統計 Content-Range:', generatedContentRange);
    const generatedCount = parseInt(generatedContentRange.split('/')[1]) || 0;
    
    // キューイング済み統計取得（form_analyzer_queued = true）
    const queuedQuery = `${supabase.url}/rest/v1/companies?select=id&form_analyzer_queued=eq.true&limit=0`;
    const queuedResponse = UrlFetchApp.fetch(queuedQuery, {
      method: 'GET',
      headers: {
        ...supabase.headers,
        'Prefer': 'count=exact'  // Content-Rangeヘッダーを強制取得
      },
      muteHttpExceptions: true
    });
    
    // デバッグ: ヘッダー情報をログ出力
    const queuedHeaders = queuedResponse.getHeaders();
    console.log('キューイング済み統計 Response Headers:', queuedHeaders);
    
    // Content-Rangeヘッダーからキューイング済み件数を取得
    const queuedContentRange = queuedHeaders['content-range'] || queuedHeaders['Content-Range'] || queuedHeaders['CONTENT-RANGE'] || '0-0/0';
    console.log('キューイング済み統計 Content-Range:', queuedContentRange);
    const queuedCount = parseInt(queuedContentRange.split('/')[1]) || 0;
    
    // スタック状態統計取得（analyzer_queued_atが1時間以上古いレコード）
    const timeoutDate = new Date();
    timeoutDate.setHours(timeoutDate.getHours() - 1); // 1時間前を基準
    const timeoutDateISO = timeoutDate.toISOString();
    
    const stuckQuery = `${supabase.url}/rest/v1/companies?select=id&form_analyzer_queued=eq.true&analyzer_queued_at=lt.${timeoutDateISO}&limit=0`;
    const stuckResponse = UrlFetchApp.fetch(stuckQuery, {
      method: 'GET',
      headers: {
        ...supabase.headers,
        'Prefer': 'count=exact'  // Content-Rangeヘッダーを強制取得
      },
      muteHttpExceptions: true
    });
    
    // デバッグ: ヘッダー情報をログ出力
    const stuckHeaders = stuckResponse.getHeaders();
    console.log('スタック状態統計 Response Headers:', stuckHeaders);
    
    // Content-Rangeヘッダーからスタック件数を取得
    const stuckContentRange = stuckHeaders['content-range'] || stuckHeaders['Content-Range'] || stuckHeaders['CONTENT-RANGE'] || '0-0/0';
    console.log('スタック状態統計 Content-Range:', stuckContentRange);
    const stuckCount = parseInt(stuckContentRange.split('/')[1]) || 0;
    
    // フォールバック処理：Content-Rangeが取得できない場合（メモリ使用量最適化版）
    let finalTotalCount = totalCount;
    let finalGeneratedCount = generatedCount;
    let finalQueuedCount = queuedCount;
    let finalStuckCount = stuckCount;
    
    if (totalCount === 0) {
      console.log('Content-Rangeから件数取得失敗、メモリ効率的フォールバック処理実行');
      
      try {
        // 🧠 Memory Usage最適化: チャンク処理による統計取得
        const chunkSize = 1000; // チャンクサイズを1000件に制限
        const maxChunks = 10; // 最大10チャンク = 10,000件まで
        
        // 1. 全体件数のチャンク取得
        finalTotalCount = getCountWithChunking(supabase, 
          'form_url=not.is.null', chunkSize, maxChunks, 'フォーム有り企業');
        
        // 2. 指示書生成済み件数のチャンク取得  
        finalGeneratedCount = getCountWithChunking(supabase,
          'form_url=not.is.null&instruction_json=not.is.null&not.instruction_valid=eq.false',
          chunkSize, maxChunks, '指示書生成済み企業');
        
        // 3. キューイング済み件数のチャンク取得
        finalQueuedCount = getCountWithChunking(supabase,
          'form_analyzer_queued=eq.true', chunkSize, maxChunks, 'キューイング済み企業');
        
        // 4. スタック状態件数のチャンク取得
        const timeoutDateISO = new Date(Date.now() - 60 * 60 * 1000).toISOString(); // 1時間前
        finalStuckCount = getCountWithChunking(supabase,
          `form_analyzer_queued=eq.true&analyzer_queued_at=lt.${timeoutDateISO}`, chunkSize, maxChunks, 'スタック状態企業');
        
        console.log(`🧠 メモリ効率的フォールバック完了 - 全体: ${finalTotalCount}, 生成済み: ${finalGeneratedCount}, キューイング済み: ${finalQueuedCount}, スタック: ${finalStuckCount}`);
        
      } catch (fallbackError) {
        console.error('メモリ効率的フォールバック処理エラー:', fallbackError);
        // フォールバック失敗時は概算値を使用
        finalTotalCount = 0;
        finalGeneratedCount = 0;
        finalQueuedCount = 0;
        finalStuckCount = 0;
      }
    }
    
    // 未処理統計算出
    const pendingCount = finalTotalCount - finalGeneratedCount;
    const notQueuedCount = finalTotalCount - finalQueuedCount; // form_analyzer_queued未処理
    
    const stats = {
      total_with_forms: finalTotalCount,
      instruction_generated: finalGeneratedCount,
      pending: pendingCount,
      form_analyzer_queued: finalQueuedCount,  // キューイング済み件数
      not_queued: notQueuedCount,       // キューイング未済件数
      stuck_queued: finalStuckCount,    // スタック状態件数（1時間以上古いキューイング）
      progress_rate: finalTotalCount > 0 ? Math.round((finalGeneratedCount / finalTotalCount) * 100) : 0,
      queued_rate: finalTotalCount > 0 ? Math.round((finalQueuedCount / finalTotalCount) * 100) : 0,
      stuck_rate: finalQueuedCount > 0 ? Math.round((finalStuckCount / finalQueuedCount) * 100) : 0,
      usedFallback: totalCount === 0,  // フォールバック使用フラグ
      cached: false,  // 新規取得フラグ
      last_updated: new Date().toISOString()  // 最終更新時刻
    };
    
    console.log('Form Analyzer処理統計:', stats);
    
    // 統計情報をキャッシュに保存
    setCachedStats(taskType, stats);
    
    return stats;
    
  } catch (error) {
    console.error('処理統計取得エラー:', error);
    return {
      total_with_forms: 0,
      instruction_generated: 0,
      pending: 0,
      form_analyzer_queued: 0,
      not_queued: 0,
      progress_rate: 0,
      queued_rate: 0,
      error: error.toString()
    };
  }
}

/**
 * 🧠 メモリ効率的なチャンク処理による件数取得（Memory Usage最適化）
 * @param {Object} supabase Supabaseクライアント
 * @param {string} condition 検索条件
 * @param {number} chunkSize チャンクサイズ
 * @param {number} maxChunks 最大チャンク数
 * @param {string} description 説明（ログ用）
 * @returns {number} 件数
 */
function getCountWithChunking(supabase, condition, chunkSize = 1000, maxChunks = 10, description = '') {
  try {
    console.log(`🧠 チャンク処理開始: ${description} (チャンクサイズ: ${chunkSize}, 最大: ${maxChunks}チャンク)`);
    
    let totalCount = 0;
    let offset = 0;
    let chunkIndex = 0;
    let hasMore = true;
    
    while (hasMore && chunkIndex < maxChunks) {
      // チャンクごとにデータを取得
      const query = `${supabase.url}/rest/v1/companies?select=id&${condition}&limit=${chunkSize}&offset=${offset}`;
      
      try {
        const response = UrlFetchApp.fetch(query, {
          method: 'GET',
          headers: supabase.headers,
          muteHttpExceptions: true
        });
        
        if (response.getResponseCode() === 200 || response.getResponseCode() === 206) {
          const chunkData = JSON.parse(response.getContentText());
          const chunkCount = chunkData.length;
          totalCount += chunkCount;
          
          console.log(`🧠 チャンク ${chunkIndex + 1}: ${chunkCount}件 (累計: ${totalCount}件)`);
          
          // 取得件数がチャンクサイズ未満の場合は終了
          if (chunkCount < chunkSize) {
            hasMore = false;
          } else {
            offset += chunkSize;
            chunkIndex++;
            
            // 次のチャンク取得前に少し待機（レート制限回避）
            Utilities.sleep(100);
          }
        } else {
          console.error(`🧠 チャンク取得エラー: HTTP ${response.getResponseCode()}`);
          hasMore = false;
        }
      } catch (chunkError) {
        console.error(`🧠 チャンク処理エラー (${chunkIndex}): ${chunkError}`);
        hasMore = false;
      }
    }
    
    if (chunkIndex >= maxChunks) {
      console.warn(`🧠 最大チャンク数に到達: ${description} (${totalCount}+ 件、実際にはより多い可能性)`);
    }
    
    console.log(`🧠 チャンク処理完了: ${description} = ${totalCount}件 (${chunkIndex + 1}チャンク処理)`);
    return totalCount;
    
  } catch (error) {
    console.error(`🧠 チャンク処理全体エラー (${description}): ${error}`);
    return 0;
  }
}

/**
 * 特定バッチの処理結果を確認
 * @param {string} batchId バッチID
 * @returns {Object} バッチ処理結果
 */
function getBatchResults(batchId) {
  try {
    // batch_requestテーブルから確認
    const supabase = getSupabaseClient();
    const query = `${supabase.url}/rest/v1/batch_request?select=*&batch_id=eq.${batchId}`;
    
    const response = UrlFetchApp.fetch(query, {
      method: 'GET',
      headers: supabase.headers,
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200) {
      const data = JSON.parse(response.getContentText());
      return data.length > 0 ? data[0] : null;
    } else if (responseCode === 404) {
      // batch_requestテーブルが存在しない場合
      console.log('batch_requestテーブルが存在しないため、バッチ結果確認をスキップ');
      return null;
    } else {
      console.error(`バッチ結果確認エラー: ${responseCode}`);
      return null;
    }
    
  } catch (error) {
    console.error('バッチ結果確認エラー:', error);
    return null;
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
function resetFormAnalyzerStatus(confirmationToken) {
  if (confirmationToken !== 'RESET_FORM_ANALYZER_STATUS') {
    throw new Error('不正な確認トークンです');
  }
  
  try {
    console.log('Form Analyzer処理状況リセット実行');
    
    const supabase = getSupabaseClient();
    
    // 全レコードのform_analyzer_queued、instruction_json、instruction_validをクリア
    const resetQuery = `${supabase.url}/rest/v1/companies`;
    const resetData = {
      form_analyzer_queued: null,
      instruction_json: null,
      instruction_valid: null
    };
    
    const response = UrlFetchApp.fetch(resetQuery, {
      method: 'PATCH',
      headers: supabase.headers,
      payload: JSON.stringify(resetData),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 204) {
      console.log('Form Analyzer処理状況リセット完了');
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
 * デバッグ用：実際のSupabaseデータ確認（Form Analyzer用）
 * @returns {Array} サンプルデータ配列
 */
function debugSupabaseData() {
  try {
    console.log('=== Supabase Form Analyzerデータ確認デバッグ ===');
    
    const supabase = getSupabaseClient();
    
    // 基本的なデータ取得テスト
    const query = `${supabase.url}/rest/v1/companies?select=id,company_name,form_url,form_analyzer_queued,instruction_json,instruction_valid&limit=10`;
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
      
      // form_analyzer_queuedの状態を分析
      const queuedNullCount = data.filter(item => item.form_analyzer_queued === null).length;
      const queuedTrueCount = data.filter(item => item.form_analyzer_queued === true).length;
      const queuedFalseCount = data.filter(item => item.form_analyzer_queued === false).length;
      
      // instruction_jsonの状態を分析
      const instructionNullCount = data.filter(item => item.instruction_json === null).length;
      const instructionHasValueCount = data.filter(item => item.instruction_json && item.instruction_json !== '').length;
      
      // instruction_validの状態を分析
      const validTrueCount = data.filter(item => item.instruction_valid === true).length;
      const validNullCount = data.filter(item => item.instruction_valid === null).length;
      const validFalseCount = data.filter(item => item.instruction_valid === false).length;
      
      console.log(`form_analyzer_queued分析 (サンプル10件中):`);
      console.log(`- NULL (未処理): ${queuedNullCount}件`);
      console.log(`- TRUE (キュー済み): ${queuedTrueCount}件`);
      console.log(`- FALSE: ${queuedFalseCount}件`);
      
      console.log(`instruction_json分析 (サンプル10件中):`);
      console.log(`- NULL (未生成): ${instructionNullCount}件`);
      console.log(`- 値あり: ${instructionHasValueCount}件`);
      
      console.log(`instruction_valid分析 (サンプル10件中):`);
      console.log(`- TRUE (有効): ${validTrueCount}件`);
      console.log(`- NULL: ${validNullCount}件`);
      console.log(`- FALSE (無効): ${validFalseCount}件`);
      
      return {
        success: true,
        totalSample: data.length,
        sampleData: data.slice(0, 3),
        formAnalyzerQueuedAnalysis: {
          queuedNull: queuedNullCount,
          queuedTrue: queuedTrueCount,
          queuedFalse: queuedFalseCount
        },
        instructionJsonAnalysis: {
          instructionNull: instructionNullCount,
          instructionHasValue: instructionHasValueCount
        },
        instructionValidAnalysis: {
          validTrue: validTrueCount,
          validNull: validNullCount,
          validFalse: validFalseCount
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
 * form_analyzer_queuedステータスのみをリセット（部分的なリセット機能）
 * @param {string} confirmationToken 確認トークン
 * @returns {Object} リセット結果
 */
function resetFormAnalyzerQueuedStatus(confirmationToken) {
  if (confirmationToken !== 'RESET_FORM_ANALYZER_QUEUED_ONLY') {
    throw new Error('不正な確認トークンです');
  }
  
  try {
    console.log('form_analyzer_queuedステータスのみリセット実行');
    
    const supabase = getSupabaseClient();
    
    // 全レコードのform_analyzer_queuedのみをクリア
    const resetQuery = `${supabase.url}/rest/v1/companies`;
    const resetData = {
      form_analyzer_queued: null
    };
    
    const response = UrlFetchApp.fetch(resetQuery, {
      method: 'PATCH',
      headers: supabase.headers,
      payload: JSON.stringify(resetData),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 204) {
      console.log('form_analyzer_queuedステータスのみリセット完了');
      return { success: true, message: 'form_analyzer_queuedリセット完了' };
    } else {
      const errorText = response.getContentText();
      console.error(`form_analyzer_queuedリセット失敗: ${responseCode} - ${errorText}`);
      return { success: false, error: `HTTP ${responseCode}: ${errorText}` };
    }
    
  } catch (error) {
    console.error('form_analyzer_queuedリセットエラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * analyzer_queued_atカラムの存在確認
 * @returns {boolean} カラムが存在する場合はtrue
 */
function checkAnalyzerQueuedAtColumnExists() {
  try {
    const supabase = getSupabaseClient();
    
    // analyzer_queued_atカラムを含むクエリを実行してエラーチェック
    const testQuery = `${supabase.url}/rest/v1/companies?select=id,analyzer_queued_at&limit=1`;
    
    const response = UrlFetchApp.fetch(testQuery, {
      method: 'GET',
      headers: supabase.headers,
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200) {
      console.log('✅ analyzer_queued_atカラム存在確認: OK');
      return true;
    } else if (responseCode === 400) {
      const errorText = response.getContentText();
      if (errorText.includes('analyzer_queued_at')) {
        console.warn('⚠️ analyzer_queued_atカラムが存在しません。スキーママイグレーションが必要です。');
        return false;
      }
    }
    
    console.warn('⚠️ analyzer_queued_atカラム存在確認で予期しないレスポンス:', responseCode);
    return false;
    
  } catch (error) {
    console.error('analyzer_queued_atカラム存在確認エラー:', error);
    return false;
  }
}

// スタックレコードクリーンアップ用定数
const STUCK_CLEANUP_CONFIG = {
  BATCH_SIZE: 100,        // 1回のクエリで取得する最大件数
  MAX_TOTAL_RECORDS: 1000, // 処理する最大総件数（無限ループ防止）
  UPDATE_BATCH_SIZE: 50    // 更新処理のバッチサイズ
};

/**
 * スタック状態のform_analyzer_queuedレコードの自動クリーンアップ（バッチ処理対応版）
 * analyzer_queued_atから指定時間経過したレコードを自動でnullにリセット
 * @param {number} timeoutHours タイムアウト時間（時間）、デフォルト1時間
 * @returns {Object} クリーンアップ結果 - { success: boolean, cleaned_count: number, total_found: number, batches_processed: number, timeout_hours: number, timeout_date: string }
 */
function cleanupStuckFormAnalyzerQueued(timeoutHours = 1) {
  try {
    const supabase = getSupabaseClient();
    
    console.log(`スタック状態のform_analyzer_queuedクリーンアップ開始: タイムアウト=${timeoutHours}時間 (バッチ処理対応)`);
    
    // analyzer_queued_atカラム存在確認
    const hasAnalyzerQueuedAt = checkAnalyzerQueuedAtColumnExists();
    if (!hasAnalyzerQueuedAt) {
      console.warn('analyzer_queued_atカラムが存在しないため、クリーンアップをスキップします');
      return {
        success: false,
        error: 'analyzer_queued_atカラムが存在しません。スキーママイグレーションが必要です。',
        cleaned_count: 0
      };
    }
    
    // タイムアウト時刻を計算（JST）
    const timeoutDate = new Date();
    timeoutDate.setHours(timeoutDate.getHours() - timeoutHours);
    const timeoutDateISO = timeoutDate.toISOString();
    
    console.log(`タイムアウト基準時刻: ${timeoutDateISO}`);
    console.log(`バッチ設定 - バッチサイズ: ${STUCK_CLEANUP_CONFIG.BATCH_SIZE}件, 最大処理件数: ${STUCK_CLEANUP_CONFIG.MAX_TOTAL_RECORDS}件`);
    
    let offset = 0;
    let totalCleanedCount = 0;
    let totalFoundCount = 0;
    let batchesProcessed = 0;
    let allStuckRecords = [];
    
    // バッチ処理ループ
    while (offset < STUCK_CLEANUP_CONFIG.MAX_TOTAL_RECORDS) {
      console.log(`バッチ ${batchesProcessed + 1} 処理開始 (offset: ${offset})`);
      
      // スタック状態のレコードを検索
      const searchQuery = `${supabase.url}/rest/v1/companies?select=id,company_name,analyzer_queued_at&form_analyzer_queued=eq.true&analyzer_queued_at=lt.${timeoutDateISO}&limit=${STUCK_CLEANUP_CONFIG.BATCH_SIZE}&offset=${offset}&order=id`;
      
      const searchResult = supabaseRequestWithRetry(searchQuery, {
        method: 'GET',
        headers: supabase.headers
      }, `スタックレコード検索 (バッチ ${batchesProcessed + 1})`);
      
      if (!searchResult.success) {
        console.error(`バッチ ${batchesProcessed + 1} 検索失敗: ${searchResult.error}`);
        break;
      }
      
      const batchStuckRecords = searchResult.data || [];
      
      if (batchStuckRecords.length === 0) {
        console.log(`バッチ ${batchesProcessed + 1}: スタックレコードなし、処理終了`);
        break;
      }
      
      console.log(`バッチ ${batchesProcessed + 1}: ${batchStuckRecords.length}件のスタックレコード発見`);
      totalFoundCount += batchStuckRecords.length;
      allStuckRecords = allStuckRecords.concat(batchStuckRecords);
      
      // バルクリセット実行
      const recordIds = batchStuckRecords.map(record => record.id);
      const cleanupResult = updateFormAnalyzerQueued(recordIds, null);
      
      if (cleanupResult.success) {
        const cleanedInBatch = cleanupResult.updated_count;
        totalCleanedCount += cleanedInBatch;
        console.log(`✅ バッチ ${batchesProcessed + 1} クリーンアップ完了: ${cleanedInBatch}件`);
      } else {
        console.error(`❌ バッチ ${batchesProcessed + 1} クリーンアップ失敗: ${cleanupResult.error}`);
        // 失敗しても次のバッチを処理（部分的成功も記録）
      }
      
      batchesProcessed++;
      offset += STUCK_CLEANUP_CONFIG.BATCH_SIZE;
      
      // 最後のバッチの場合は終了
      if (batchStuckRecords.length < STUCK_CLEANUP_CONFIG.BATCH_SIZE) {
        console.log('最後のバッチ処理完了');
        break;
      }
      
      // 次のバッチまで少し待機（API負荷軽減）
      if (batchesProcessed < Math.ceil(STUCK_CLEANUP_CONFIG.MAX_TOTAL_RECORDS / STUCK_CLEANUP_CONFIG.BATCH_SIZE)) {
        Utilities.sleep(500); // 0.5秒待機
      }
    }
    
    // 処理結果サマリー
    console.log(`=== クリーンアップ結果サマリー ===`);
    console.log(`処理バッチ数: ${batchesProcessed}`);
    console.log(`発見総件数: ${totalFoundCount}件`);
    console.log(`クリーンアップ総件数: ${totalCleanedCount}件`);
    console.log(`成功率: ${totalFoundCount > 0 ? Math.round((totalCleanedCount / totalFoundCount) * 100) : 0}%`);
    
    // 詳細ログ出力（最初の10件のみ）
    if (allStuckRecords.length > 0) {
      const recordsToLog = allStuckRecords.slice(0, 10);
      console.log(`スタックレコード詳細 (最初の${recordsToLog.length}件):`);
      recordsToLog.forEach(record => {
        const queuedAt = new Date(record.analyzer_queued_at);
        const elapsedHours = (new Date() - queuedAt) / (1000 * 60 * 60);
        console.log(`  - ID:${record.id}, 会社:${record.company_name || 'N/A'}, キュー時刻:${record.analyzer_queued_at}, 経過:${elapsedHours.toFixed(1)}時間`);
      });
      
      if (allStuckRecords.length > 10) {
        console.log(`  ... 他${allStuckRecords.length - 10}件`);
      }
    }
    
    return {
      success: totalCleanedCount > 0 || totalFoundCount === 0,
      cleaned_count: totalCleanedCount,
      total_found: totalFoundCount,
      batches_processed: batchesProcessed,
      timeout_hours: timeoutHours,
      timeout_date: timeoutDateISO,
      stuck_records_sample: allStuckRecords.slice(0, 5) // サンプルとして最初の5件
    }
    
  } catch (error) {
    console.error('スタックレコードクリーンアップエラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      cleaned_count: 0
    };
  }
}

/**
 * Supabaseクライアント作成（後方互換性のためのエイリアス）
 * @param {string} url Supabase URL
 * @param {string} key Service Role Key
 * @returns {Object} 簡易クライアントオブジェクト
 */
function createClient(url, key) {
  return {
    url: url,
    key: key,
    from: function(table) {
      const client = this;
      return {
        select: function(columns) {
          return {
            eq: function(column, value) {
              return {
                single: function() {
                  const query = `${client.url}/rest/v1/${table}?select=${columns}&${column}=eq.${value}&limit=1`;
                  
                  const response = UrlFetchApp.fetch(query, {
                    method: 'GET',
                    headers: {
                      'apikey': client.key,
                      'Authorization': `Bearer ${client.key}`,
                      'Content-Type': 'application/json'
                    },
                    muteHttpExceptions: true
                  });
                  
                  const responseCode = response.getResponseCode();
                  const responseText = response.getContentText();
                  
                  if (responseCode === 200 || responseCode === 206) {
                    const data = JSON.parse(responseText);
                    return { data: data.length > 0 ? data[0] : null, error: null };
                  } else {
                    return { data: null, error: { message: `HTTP ${responseCode}: ${responseText}` } };
                  }
                }
              };
            }
          };
        },
        update: function(updateData) {
          return {
            eq: function(column, value) {
              const query = `${client.url}/rest/v1/${table}?${column}=eq.${value}`;
              
              const response = UrlFetchApp.fetch(query, {
                method: 'PATCH',
                headers: {
                  'apikey': client.key,
                  'Authorization': `Bearer ${client.key}`,
                  'Content-Type': 'application/json'
                },
                payload: JSON.stringify(updateData),
                muteHttpExceptions: true
              });
              
              const responseCode = response.getResponseCode();
              const responseText = response.getContentText();
              
              if (responseCode === 204) {
                return { data: null, error: null };
              } else {
                return { data: null, error: { message: `HTTP ${responseCode}: ${responseText}` } };
              }
            }
          };
        }
      };
    }
  };
}