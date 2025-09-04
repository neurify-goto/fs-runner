/**
 * GASシーケンシャルバッチ処理システム（Form Analyzer用）
 * GitHub Actionsとの連携によるメインオーケストレーター
 * 
 * 📝 設定変更方法:
 *   1. 下記のクイック設定（BATCH_SIZE等）を直接変更
 *   2. ファイルを保存
 *   3. 次回実行時から新しい設定値が適用
 */

// ==========================================
// 🔧 **設定値 (手動調整可能)**
// ==========================================

// 🚀 **クイック設定 (よく変更される値)**
const BATCH_SIZE = 50;                  // ★ バッチサイズ（処理件数/回）推奨: 10-100
const STUCK_QUEUE_TIMEOUT_HOURS = 1;    // ★ スタックキュータイムアウト（時間）推奨: 1-24

// 📋 **詳細設定**
const FORM_ANALYZER_CONFIG = {
  // バッチ処理設定（上部のクイック設定を参照）
  BATCH_SIZE: BATCH_SIZE,                    // ★ バッチサイズ（処理件数/回）
  BULK_BATCH_SIZE: 100,                      // バルク更新バッチサイズ
  MAX_RETRIES: 3,                            // 最大リトライ回数
  RETRY_DELAY: 2000,                         // リトライ間隔（ミリ秒）
  
  // タイムアウト・時間設定
  GROQ_TIMEOUT: 120,                         // Groq APIタイムアウト（秒）
  STUCK_QUEUE_TIMEOUT_HOURS: STUCK_QUEUE_TIMEOUT_HOURS, // ★ スタックキュータイムアウト（時間）
  CACHE_TTL_MINUTES: 5,                      // キャッシュ有効期限（分）
  
  // 外部API設定
  GITHUB_API_BASE: 'https://api.github.com',
  DEFAULT_TASK_TYPE: 'form_analyzer',
  
  // 高度な設定
  EXPONENTIAL_BACKOFF: {
    initial_delay_ms: 1000,
    max_delay_ms: 30000,
    multiplier: 2,
    max_retries: 5
  }
};

// クリーンアップ失敗追跡用の定数
const CLEANUP_FAILURE_CONFIG = {
  MAX_CONSECUTIVE_FAILURES: 3,        // 連続失敗の上限
  FAILURE_TRACKING_KEY: 'cleanup_consecutive_failures', // PropertiesServiceキー
  LAST_FAILURE_TIME_KEY: 'cleanup_last_failure_time'   // 最終失敗時刻キー
};

/**
 * 設定値の取得（上部のFORM_ANALYZER_CONFIGを返す）
 * @returns {Object} 設定オブジェクト
 */
function loadConfig() {
  console.log('✅ Form Analyzer設定値読み込み完了');
  return FORM_ANALYZER_CONFIG;
}

/**
 * ★【追加】時間ベースのトリガーから呼び出すための関数
 * この関数をトリガーに設定してください
 */
function startProcessingFromTrigger() {
  console.log('時間ベースのトリガーにより処理を開始します');
  const config = loadConfig();
  // デフォルトのタスクタイプを指定して本体の関数を呼び出す
  startProcessing(config.DEFAULT_TASK_TYPE); 
}

/**
 * 初回実行エントリーポイント
 * @param {string} taskType タスクタイプ（'form_analyzer' など）
 * @param {number} limit 処理件数制限（オプション）
 */
function startProcessing(taskType = null, limit = null) {
  try {
    const config = loadConfig();
    if (taskType === null) {
      taskType = config.DEFAULT_TASK_TYPE;
    }
    console.log(`処理開始: taskType=${taskType}, limit=${limit}`);
    
    // 最初のバッチデータを取得
    const batchData = getNextPendingBatch(taskType, config.BATCH_SIZE, limit);
    
    if (!batchData || batchData.length === 0) {
      console.log('処理対象のデータが見つかりません');
      return { success: false, message: '処理対象なし' };
    }
    
    // GitHub Actions ワークフローを開始
    const result = triggerWorkflow(batchData, taskType);
    
    if (result.success) {
      console.log(`初回ワークフロー開始成功: batch_id=${result.batch_id}, 件数=${batchData.length}`);
      return { success: true, batch_id: result.batch_id, count: batchData.length };
    } else {
      console.error('初回ワークフロー開始失敗:', result.error);
      return { success: false, error: result.error };
    }
    
  } catch (error) {
    console.error('初回処理エラー:', error);
    return { success: false, error: error.toString() };
  }
}


/**
 * GitHub Actions ワークフロー開始
 * @param {Array} batchData バッチデータ
 * @param {string} taskType タスクタイプ
 */
function triggerWorkflow(batchData, taskType) {
  try {
    // バッチID生成
    const batch_id = generateBatchId(taskType);
    
    // Repository Dispatch イベント送信
    const dispatchResult = sendRepositoryDispatch(taskType, batch_id, batchData);
    
    if (dispatchResult.success) {
      return { success: true, batch_id: batch_id };
    } else {
      return { success: false, error: dispatchResult.error };
    }
    
  } catch (error) {
    console.error('ワークフロー開始エラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * バッチID生成
 * @param {string} taskType タスクタイプ
 */
function generateBatchId(taskType) {
  const now = new Date();
  const timestamp = Utilities.formatDate(now, 'Asia/Tokyo', 'yyyyMMdd_HHmmss');
  const random = Math.floor(Math.random() * 1000).toString().padStart(3, '0');
  return `${taskType}_${timestamp}_${random}`;
}

/**
 * batch_idからタスクタイプを抽出
 * @param {string} batchId バッチID
 */
function extractTaskTypeFromBatchId(batchId) {
  const config = loadConfig();
  const parts = batchId.split('_');
  return parts.length > 0 ? parts[0] : config.DEFAULT_TASK_TYPE;
}


/**
 * 緊急停止・デバッグ用関数群
 */

/**
 * 手動バッチトリガー（テスト用）
 */
function manualTriggerTest() {
  const result = startProcessing('form_analyzer', 2);
  console.log('手動トリガー結果:', result);
  return result;
}

/**
 * 設定確認
 */
function checkConfiguration() {
  const properties = PropertiesService.getScriptProperties().getProperties();
  const requiredKeys = ['GITHUB_TOKEN', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'WEBHOOK_AUTH_TOKEN'];
  
  console.log('=== 設定確認 ===');
  requiredKeys.forEach(key => {
    const hasValue = properties[key] ? 'OK' : 'MISSING';
    console.log(`${key}: ${hasValue}`);
  });
  
  return properties;
}

/**
 * 処理状況確認
 */
function checkProcessingStatus() {
  try {
    const supabase = getSupabaseClient();
    const stats = getProcessingStats('form_analyzer');
    
    console.log('=== Form Analyzer処理状況 ===');
    console.log(`フォーム有り企業総数: ${stats.total_with_forms}`);
    console.log(`指示書生成済み: ${stats.instruction_generated}`);
    console.log(`キューイング済み: ${stats.form_analyzer_queued}`);
    console.log(`スタック状態: ${stats.stuck_queued}件 (${stats.stuck_rate}%)`);
    console.log(`未処理: ${stats.pending}`);
    console.log(`進捗率: ${stats.progress_rate}%`);
    
    return stats;
    
  } catch (error) {
    console.error('処理状況確認エラー:', error);
    return null;
  }
}

/**
 * 【後方互換性】旧メイン関数：フォーム解析バッチ処理を開始
 * @deprecated startProcessing()を使用してください
 */
function startFormAnalyzerBatch(batchSize = null) {
  const config = loadConfig();
  if (batchSize === null) {
    batchSize = config.BATCH_SIZE;
  }
  return startProcessing(config.DEFAULT_TASK_TYPE, batchSize);
}

/**
 * 【後方互換性】旧定期実行関数：フォーム解析バッチの自動実行
 * @deprecated startProcessingFromTrigger()を使用してください
 */
function scheduledFormAnalyzerExecution() {
  return startProcessingFromTrigger();
}

/**
 * テスト用：小バッチでのフォーム解析実行
 */
function testFormAnalyzerBatch() {
  console.log('=== テスト用フォーム解析バッチ実行 ===');
  const config = loadConfig();
  return startProcessing(config.DEFAULT_TASK_TYPE, 3);
}

/**
 * フォーム解析処理の統計情報を取得（後方互換性）
 */
function getFormAnalyzerStatistics() {
  return getProcessingStats('form_analyzer');
}

/**
 * バッチ処理の結果確認と後処理（後方互換性）
 * GitHub Actions完了後に手動実行またはWebhookから呼び出し
 */
function checkFormAnalyzerBatchResult(batchId) {
  return getBatchResults(batchId);
}

/**
 * 企業のform_analyzer_queuedを更新（後方互換性）
 */
function updateCompaniesQueueStatus(companyIds, queued) {
  return updateFormAnalyzerQueued(companyIds, queued);
}

/**
 * Web App エンドポイント（オプション）
 * 外部からHTTPリクエストでバッチ処理を開始する場合
 */
function doPost(e) {
  try {
    const requestData = JSON.parse(e.postData.contents);
    const action = requestData.action;
    
    let result;
    switch (action) {
      case 'start_batch':
        const config = loadConfig();
        const batchSize = requestData.batch_size || config.BATCH_SIZE;
        result = startProcessing(config.DEFAULT_TASK_TYPE, batchSize);
        break;
      case 'check_result':
        const batchId = requestData.batch_id;
        result = checkFormAnalyzerBatchResult(batchId);
        break;
      case 'get_stats':
        result = getFormAnalyzerStatistics();
        break;
      default:
        result = { success: false, error: 'Unknown action' };
    }
    
    return ContentService
      .createTextOutput(JSON.stringify(result))
      .setMimeType(ContentService.MimeType.JSON);
      
  } catch (error) {
    console.error('Web App エラー:', error);
    return ContentService
      .createTextOutput(JSON.stringify({ success: false, error: error.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

/**
 * 連続失敗回数を追跡・管理
 * @param {boolean} isSuccess 今回の処理が成功したかどうか
 * @returns {Object} 失敗追跡情報
 */
function trackCleanupFailures(isSuccess) {
  const properties = PropertiesService.getScriptProperties();
  
  if (isSuccess) {
    // 成功時は失敗カウンターをリセット
    properties.deleteProperty(CLEANUP_FAILURE_CONFIG.FAILURE_TRACKING_KEY);
    properties.deleteProperty(CLEANUP_FAILURE_CONFIG.LAST_FAILURE_TIME_KEY);
    return { consecutive_failures: 0, should_alert: false };
  } else {
    // 失敗時は失敗カウンターを増加
    const currentFailures = parseInt(properties.getProperty(CLEANUP_FAILURE_CONFIG.FAILURE_TRACKING_KEY)) || 0;
    const newFailureCount = currentFailures + 1;
    const currentTime = new Date().toISOString();
    
    properties.setProperty(CLEANUP_FAILURE_CONFIG.FAILURE_TRACKING_KEY, newFailureCount.toString());
    properties.setProperty(CLEANUP_FAILURE_CONFIG.LAST_FAILURE_TIME_KEY, currentTime);
    
    const shouldAlert = newFailureCount >= CLEANUP_FAILURE_CONFIG.MAX_CONSECUTIVE_FAILURES;
    
    return {
      consecutive_failures: newFailureCount,
      should_alert: shouldAlert,
      last_failure_time: currentTime
    };
  }
}

/**
 * 重大なクリーンアップ失敗時のアラート処理
 * @param {Object} failureInfo 失敗情報
 * @param {Object} result クリーンアップ結果
 */
function handleCleanupAlert(failureInfo, result) {
  if (!failureInfo.should_alert) return;
  
  const alertMessage = `🚨 Form Analyzer クリーンアップ連続失敗アラート\n\n` +
    `連続失敗回数: ${failureInfo.consecutive_failures}回\n` +
    `最終失敗時刻: ${failureInfo.last_failure_time}\n` +
    `エラー内容: ${result.error || 'Unknown error'}\n\n` +
    `対処が必要です。手動でスキーママイグレーションの確認や、` +
    `manualCleanupStuckQueue()の実行を検討してください。`;
  
  console.error(alertMessage);
  
  // 将来的にはSlackやメール通知を追加可能
  // 現在はログ出力のみ
}

/**
 * ★【定期実行用】スタック状態のform_analyzer_queuedレコードクリーンアップ（強化版）
 * 時間ベースのトリガーから呼び出される関数
 * 連続失敗の検出とアラート機能付き
 */
function cleanupStuckQueueFromTrigger() {
  try {
    console.log('定期実行トリガーによるスタックキュークリーンアップ開始');
    
    const config = loadConfig();
    const timeoutHours = config.STUCK_QUEUE_TIMEOUT_HOURS || 1;
    
    console.log(`設定値: タイムアウト時間=${timeoutHours}時間`);
    
    const result = cleanupStuckFormAnalyzerQueued(timeoutHours);
    
    // 失敗追跡と成功・失敗に応じた処理
    const failureInfo = trackCleanupFailures(result.success);
    
    if (result.success) {
      const summary = result.batches_processed ? 
        `${result.cleaned_count}件をリセット (${result.batches_processed}バッチ処理)` :
        `${result.cleaned_count}件をリセット`;
      
      console.log(`✅ クリーンアップ完了: ${summary}`);
      
      // 詳細情報がある場合は追加ログ
      if (result.total_found !== undefined) {
        console.log(`  - 発見総件数: ${result.total_found}件`);
      }
    } else {
      console.error(`❌ クリーンアップ失敗: ${result.error}`);
      console.error(`連続失敗回数: ${failureInfo.consecutive_failures}回`);
      
      // 重大な失敗時のアラート処理
      handleCleanupAlert(failureInfo, result);
    }
    
    // 失敗追跡情報を結果に追加
    result.failure_tracking = failureInfo;
    
    return result;
    
  } catch (error) {
    console.error('スタックキュークリーンアップエラー:', error);
    
    const criticalResult = { 
      success: false, 
      error: error.toString(), 
      cleaned_count: 0 
    };
    
    // 例外エラーも失敗として追跡
    const failureInfo = trackCleanupFailures(false);
    handleCleanupAlert(failureInfo, criticalResult);
    
    criticalResult.failure_tracking = failureInfo;
    return criticalResult;
  }
}

/**
 * 手動でスタックキュークリーンアップを実行（テスト用・緊急対応用）
 * @param {number|null} timeoutHours タイムアウト時間（時間）、nullの場合は設定値を使用
 * @returns {Object} クリーンアップ結果 - { success: boolean, cleaned_count: number, total_found?: number, batches_processed?: number, timeout_hours: number, timeout_date: string, stuck_records_sample?: Array }
 */
function manualCleanupStuckQueue(timeoutHours = null) {
  console.log('=== 手動スタックキュークリーンアップ実行 ===');
  
  const config = loadConfig();
  const actualTimeoutHours = timeoutHours || config.STUCK_QUEUE_TIMEOUT_HOURS || 1;
  
  console.log(`手動クリーンアップ実行: タイムアウト=${actualTimeoutHours}時間`);
  
  const result = cleanupStuckFormAnalyzerQueued(actualTimeoutHours);
  console.log('手動クリーンアップ結果:', result);
  
  return result;
}