/**
 * GASシーケンシャルバッチ処理システム（Form Finder用）
 * GitHub Actionsとの連携によるメインオーケストレーター
 */

// 設定定数
const CONFIG = {
  BATCH_SIZE: 120, // バッチサイズ（300以上はペイロードが大きすぎてエラーになる）
  MAX_RETRIES: 3,  // 最大リトライ回数
  RETRY_DELAY: 2000, // リトライ遅延（ミリ秒）
  TIMEOUT_MS: 60000, // APIタイムアウト（60秒）
  MIN_BATCH_SIZE: 10, // フォールバック時の最小バッチサイズ
  GITHUB_API_BASE: 'https://api.github.com',
  DEFAULT_TASK_TYPE: 'form_finder',
  // 企業テーブル設定（デフォルトはcompanies）
  COMPANY_TABLES: {
    PRIMARY: 'companies',
    EXTRA: 'companies_extra'
  },
  
  
  // 並列実行制限設定
  MAX_CONCURRENT_WORKFLOWS: 8,
  
  // API最適化設定
  API_CACHE_DURATION_MS: 120000, // 2分間のキャッシュ
  API_BACKOFF_INITIAL_DELAY_MS: 1000,
  API_BACKOFF_MAX_DELAY_MS: 30000,
  API_BACKOFF_MULTIPLIER: 2,
  
  // エラーハンドリング設定
  MAX_CONSECUTIVE_API_FAILURES: 3,
  GRACEFUL_DEGRADATION_ENABLED: true
};

/**
 * ★時間ベースのトリガーから呼び出すための関数
 * この関数をトリガーに設定してください
 * 並列実行数制御により、上限未満の場合のみ処理を実行します
 */
function startProcessingFromTrigger() {
  console.log('時間ベースのトリガーにより処理を開始します');
  
  try {
    // GitHub Actionsの並列実行数をチェック
    console.log('GitHub Actionsの並列実行数をチェックします');
    const workflowCheckResult = checkRunningFormFinderWorkflows();
    
    if (!workflowCheckResult.success) {
      console.error('並列実行数チェック失敗:', workflowCheckResult.error);
      // チェックに失敗した場合は安全のため処理をスキップ
      console.log('安全のため処理をスキップします');
      return { 
        success: false, 
        skipped: true, 
        reason: '並列実行数チェック失敗',
        error: workflowCheckResult.error
      };
    }
    
    const runningCount = workflowCheckResult.runningCount;
    console.log(`現在の並列実行数: ${runningCount}個 (上限: ${CONFIG.MAX_CONCURRENT_WORKFLOWS}個)`);
    
    if (runningCount >= CONFIG.MAX_CONCURRENT_WORKFLOWS) {
      console.log(`並列実行数が上限 (${CONFIG.MAX_CONCURRENT_WORKFLOWS}個) に達しているため、処理をスキップします`);
      console.log('次回実行時に再度チェックします');
      return { 
        success: true, 
        skipped: true, 
        reason: '並列実行数上限到達',
        runningCount: runningCount
      };
    }
    
    console.log(`並列実行数が上限未満のため、通常の処理を実行します (${runningCount}/${CONFIG.MAX_CONCURRENT_WORKFLOWS})`);
    
    // デフォルトのタスクタイプを指定して本体の関数を呼び出す
    const result = startProcessing(CONFIG.DEFAULT_TASK_TYPE);
    console.log('メイン処理完了:', result);
    
    return result;
    
  } catch (error) {
    console.error('startProcessingFromTrigger エラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * ★時間ベースのトリガー（companies_extra 用）
 * 並列実行数の上限を確認したうえで、companies_extra を対象に処理を開始
 */
function startProcessingFromTriggerExtra() {
  console.log('時間ベースのトリガーにより処理を開始します（extra）');
  try {
    const workflowCheckResult = checkRunningFormFinderWorkflows();
    if (!workflowCheckResult.success) {
      console.error('並列実行数チェック失敗:', workflowCheckResult.error);
      return { success: false, skipped: true, reason: '並列実行数チェック失敗', error: workflowCheckResult.error };
    }
    const runningCount = workflowCheckResult.runningCount;
    console.log(`現在の並列実行数: ${runningCount}個 (上限: ${CONFIG.MAX_CONCURRENT_WORKFLOWS}個)`);
    if (runningCount >= CONFIG.MAX_CONCURRENT_WORKFLOWS) {
      console.log(`並列実行数が上限 (${CONFIG.MAX_CONCURRENT_WORKFLOWS}個) に達しているため、処理をスキップします`);
      return { success: true, skipped: true, reason: '並列実行数上限到達', runningCount };
    }
    console.log(`並列実行数が上限未満のため、extraテーブル処理を実行します (${runningCount}/${CONFIG.MAX_CONCURRENT_WORKFLOWS})`);
    const result = startProcessing('form_finder_extra', null, { companyTable: CONFIG.COMPANY_TABLES.EXTRA });
    console.log('メイン処理完了(extra):', result);
    return result;
  } catch (error) {
    console.error('startProcessingFromTriggerExtra エラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * 初回実行エントリーポイント
 * @param {string} taskType タスクタイプ（'form_finder' など）
 * @param {number} limit 処理件数制限（オプション）
 */
function startProcessing(taskType = CONFIG.DEFAULT_TASK_TYPE, limit = null, options = {}) {
  try {
    console.log(`処理開始: taskType=${taskType}, limit=${limit}, options=${JSON.stringify(options)}`);
    
    // 最初のバッチデータを取得（リトライ機構付き）
    const batchData = getNextPendingBatchWithRetry(taskType, CONFIG.BATCH_SIZE, limit, CONFIG.MAX_RETRIES, options);
    
    if (!batchData || batchData.length === 0) {
      console.log('処理対象のデータが見つかりません');
      return { success: false, message: '処理対象なし' };
    }
    
    // GitHub Actions ワークフローを開始
    const result = triggerWorkflow(batchData, taskType, options);
    
    if (result.success) {
      console.log(`初回ワークフロー開始成功: batch_id=${result.batch_id}, 件数=${batchData.length}`);
      
      // 処理開始後、短い間隔を空けて負荷を軽減
      Utilities.sleep(1000);
      
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
 * GitHub Actions ワークフロー開始（リトライ機構付き）
 * @param {Array} batchData バッチデータ
 * @param {string} taskType タスクタイプ
 */
function triggerWorkflow(batchData, taskType, options = {}) {
  try {
    // バッチID生成
    const batch_id = generateBatchId(taskType);
    
    // Repository Dispatch イベント送信（リトライ機構付き）
    // 使用テーブル名を決定
    const companyTable = getCompanyTableFromOptions(taskType, options);
    // Repository Dispatch（使用テーブル情報を付与）
    const dispatchResult = sendRepositoryDispatchWithRetry(taskType, batch_id, batchData, CONFIG.MAX_RETRIES, { companyTable });
    
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
 * オプションから使用する企業テーブル名を決定
 * @param {string} taskType
 * @param {Object} options
 * @returns {string} companyTable
 */
function getCompanyTableFromOptions(taskType, options = {}) {
  try {
    if (options && typeof options.companyTable === 'string' && options.companyTable.trim()) {
      return options.companyTable.trim();
    }
    // taskTypeに'_extra'が含まれる場合はextraを使用
    if (String(taskType).toLowerCase().indexOf('extra') >= 0) {
      return CONFIG.COMPANY_TABLES.EXTRA;
    }
    return CONFIG.COMPANY_TABLES.PRIMARY;
  } catch (e) {
    console.warn('テーブル決定ロジックでエラー。デフォルトcompaniesを使用します:', e);
    return CONFIG.COMPANY_TABLES.PRIMARY;
  }
}

/**
 * batch_idからタスクタイプを抽出
 * @param {string} batchId バッチID
 */
function extractTaskTypeFromBatchId(batchId) {
  const parts = batchId.split('_');
  return parts.length > 0 ? parts[0] : CONFIG.DEFAULT_TASK_TYPE;
}


/**
 * 緊急停止・デバッグ用関数群
 */

/**
 * 手動バッチトリガー（テスト用）
 */
function manualTriggerTest() {
  const result = startProcessing('form_finder', 2);
  console.log('手動トリガー結果:', result);
  return result;
}

/**
 * 手動バッチトリガー（companies_extraを使用）
 */
function manualTriggerTestExtra() {
  const result = startProcessing('form_finder_extra', 2, { companyTable: CONFIG.COMPANY_TABLES.EXTRA });
  console.log('手動トリガー（extra）結果:', result);
  return result;
}

/**
 * Supabase接続とクエリパフォーマンステスト
 */
function testSupabasePerformance() {
  console.log('=== Supabase接続・パフォーマンステスト ===');
  
  try {
    // 1. 基本接続テスト
    console.log('1. Supabase接続テスト実行中...');
    const connectionResult = testSupabaseConnection();
    console.log('接続テスト結果:', connectionResult);
    
    if (!connectionResult.success) {
      console.error('接続テストに失敗しました');
      return { success: false, error: '接続テスト失敗' };
    }
    
    // 2. 最適化されたクエリテスト（5件）
    console.log('2. 最適化クエリテスト実行中（5件）...');
    const startTime = Date.now();
    
    const batchData = getNextPendingBatchWithRetry('form_finder', 5, null, 2);
    
    const endTime = Date.now();
    const executionTime = endTime - startTime;
    
    console.log(`クエリ実行時間: ${executionTime}ms`);
    console.log(`取得件数: ${batchData.length}件`);
    console.log('サンプルデータ:', batchData.slice(0, 2));
    
    // 3. 統計情報取得テスト
    console.log('3. 統計情報取得テスト実行中...');
    const statsResult = getProcessingStats('form_finder');
    console.log('統計情報:', statsResult);
    
    return {
      success: true,
      connection: connectionResult,
      query_execution_time: executionTime,
      data_count: batchData.length,
      stats: statsResult
    };
    
  } catch (error) {
    console.error('パフォーマンステストエラー:', error);
    return { success: false, error: error.toString() };
  }
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
 * 並列実行数チェックのテスト（手動実行用）
 */
function testConcurrentWorkflowCheck() {
  console.log('=== 並列実行数チェックテスト開始 ===');
  
  try {
    const checkResult = checkRunningFormFinderWorkflows();
    
    console.log('チェック結果:', JSON.stringify(checkResult, null, 2));
    
    if (checkResult.success) {
      console.log(`現在の並列実行数: ${checkResult.runningCount}個`);
      console.log(`設定上限: ${CONFIG.MAX_CONCURRENT_WORKFLOWS}個`);
      
      if (checkResult.runningCount >= CONFIG.MAX_CONCURRENT_WORKFLOWS) {
        console.log('⚠️ 上限に達しています - 新しい処理はスキップされます');
      } else {
        console.log('✅ 上限未満 - 新しい処理が実行可能です');
      }
      
      if (checkResult.runningWorkflows && checkResult.runningWorkflows.length > 0) {
        console.log('実行中のワークフロー詳細:');
        checkResult.runningWorkflows.forEach((workflow, index) => {
          console.log(`  ${index + 1}. ID: ${workflow.id}, 開始: ${workflow.created_at}`);
        });
      }
    } else {
      console.error('並列実行数チェック失敗:', checkResult.error);
    }
    
    return checkResult;
    
  } catch (error) {
    console.error('テスト実行エラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * 完全なトリガー実行フローのテスト（手動実行用）
 */
function testFullTriggerFlow() {
  console.log('=== 完全トリガーフローテスト開始 ===');
  
  try {
    // 再帰トリガーは設定しないテスト版
    const result = testTriggerFlowWithoutRecursive();
    
    console.log('=== 完全トリガーフローテスト完了 ===');
    console.log('結果:', JSON.stringify(result, null, 2));
    
    return result;
    
  } catch (error) {
    console.error('完全フローテストエラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * 再帰トリガーを設定しない版のstartProcessingFromTrigger（テスト用）
 */
function testTriggerFlowWithoutRecursive() {
  console.log('時間ベースのトリガーにより処理を開始します（テスト版）');
  
  try {
    const config = CONFIG;
    
    // GitHub Actionsの並列実行数をチェック
    console.log('GitHub Actionsの並列実行数をチェックします');
    const workflowCheckResult = checkRunningFormFinderWorkflows();
    
    if (!workflowCheckResult.success) {
      console.error('並列実行数チェック失敗:', workflowCheckResult.error);
      return { 
        success: false, 
        error: '並列実行数チェック失敗',
        details: workflowCheckResult.error
      };
    }
    
    const runningCount = workflowCheckResult.runningCount;
    console.log(`現在の並列実行数: ${runningCount}個 (上限: ${config.MAX_CONCURRENT_WORKFLOWS}個)`);
    
    if (runningCount >= config.MAX_CONCURRENT_WORKFLOWS) {
      console.log(`並列実行数が上限 (${config.MAX_CONCURRENT_WORKFLOWS}個) に達しているため、処理をスキップします`);
      return { 
        success: true, 
        skipped: true, 
        reason: '並列実行数上限到達',
        runningCount: runningCount,
        maxAllowed: config.MAX_CONCURRENT_WORKFLOWS
      };
    } else {
      console.log(`並列実行数が上限未満のため、通常の処理を実行します (${runningCount}/${config.MAX_CONCURRENT_WORKFLOWS})`);
      
      // テスト用に少量のデータで処理実行
      const result = startProcessing(config.DEFAULT_TASK_TYPE, 3);
      console.log('メイン処理完了:', result);
      
      return {
        success: true,
        processed: true,
        runningCount: runningCount,
        maxAllowed: config.MAX_CONCURRENT_WORKFLOWS,
        processingResult: result
      };
    }
    
  } catch (error) {
    console.error('testTriggerFlowWithoutRecursive エラー:', error);
    return { success: false, error: error.toString() };
  }
}
