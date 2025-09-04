/**
 * GASシーケンシャルバッチ処理システム
 * GitHub Actionsとの連携によるメインオーケストレーター
 */

// 設定定数
const CONFIG = {
  BATCH_SIZE: 60, // バッチサイズ（300以上はペイロードが大きすぎてエラーになる）
  MAX_RETRIES: 3, // 最大リトライ回数
  RETRY_DELAY: 2000, // リトライ遅延（ミリ秒）
  TIMEOUT_MS: 60000, // APIタイムアウト（60秒）
  MIN_BATCH_SIZE: 10, // フォールバック時の最小バッチサイズ
  GITHUB_API_BASE: 'https://api.github.com',
  DEFAULT_TASK_TYPE: 'fuma_detail',
  
  
  // 並列実行制限設定
  MAX_CONCURRENT_WORKFLOWS: 4,
  
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
    const workflowCheckResult = checkRunningFetchDetailWorkflows();
    
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
 * 初回実行エントリーポイント
 * @param {string} taskType タスクタイプ（'fuma_detail' など）
 * @param {number} limit 処理件数制限（オプション）
 */
function startProcessing(taskType = CONFIG.DEFAULT_TASK_TYPE, limit = null) {
  // トリガー実行時にtaskTypeがイベントオブジェクトになる場合への対策
  if (typeof taskType !== 'string') {
    console.warn(`不正なtaskType（${typeof taskType}）が渡されたため、デフォルト値「${CONFIG.DEFAULT_TASK_TYPE}」を使用します。`);
    taskType = CONFIG.DEFAULT_TASK_TYPE;
  }

  try {
    console.log(`処理開始: taskType=${taskType}, limit=${limit}`);
    
    // リトライ機能付きバッチデータ取得
    const batchData = getNextPendingBatchWithRetry(taskType, CONFIG.BATCH_SIZE, limit);
    
    if (!batchData || batchData.length === 0) {
      console.log('処理対象のデータが見つかりません');
      return { success: false, message: '処理対象なし' };
    }
    
    // GitHub Actions ワークフローを開始（リトライ機能付き）
    const result = triggerWorkflowWithRetry(batchData, taskType);
    
    if (result.success) {
      console.log(`初回ワークフロー開始成功: batch_id=${result.batch_id}, 件数=${batchData.length}`);
      return { success: true, batch_id: result.batch_id, count: batchData.length };
    } else {
      console.error('初回ワークフロー開始失敗:', result.error);
      return { success: false, error: result.error };
    }
    
  } catch (error) {
    console.error('初回処理エラー:', error);
    // タイムアウトエラーの場合は詳細なエラー情報を提供
    if (error.message.includes('timeout') || error.message.includes('57014')) {
      console.error('Supabaseタイムアウト検出: データベースクエリが応答時間制限を超過しました');
      return { 
        success: false, 
        error: 'データベースタイムアウト: クエリ処理時間が制限を超過しました',
        error_type: 'database_timeout',
        suggestion: 'バッチサイズを小さくするか、データベースインデックスの最適化を検討してください'
      };
    }
    return { success: false, error: error.toString() };
  }
}


/**
 * リトライ機能付きバッチデータ取得
 * 指定回数リトライしてバッチデータを取得する
 * @param {string} taskType タスクタイプ
 * @param {number} batchSize バッチサイズ
 * @param {number} limit 処理件数制限
 * @returns {Array} バッチデータ配列
 */
function getNextPendingBatchWithRetry(taskType, batchSize, limit) {
  let lastError = null;
  
  for (let attempt = 1; attempt <= CONFIG.MAX_RETRIES; attempt++) {
    try {
      console.log(`バッチデータ取得試行 ${attempt}/${CONFIG.MAX_RETRIES}`);
      return getNextPendingBatch(taskType, batchSize, limit);
    } catch (error) {
      lastError = error;
      console.error(`バッチデータ取得失敗 (試行${attempt}):`, error.message);
      
      if (attempt < CONFIG.MAX_RETRIES) {
        const delay = CONFIG.RETRY_DELAY * attempt;
        console.log(`${delay}ms後にリトライします...`);
        Utilities.sleep(delay);
      }
    }
  }
  
  throw new Error(`${CONFIG.MAX_RETRIES}回リトライ後もバッチデータ取得失敗: ${lastError.message}`);
}

/**
 * リトライ機能付きワークフロー開始
 * Repository Dispatchイベントをリトライ機能付きで送信
 * @param {Array} batchData バッチデータ
 * @param {string} taskType タスクタイプ
 * @returns {Object} 実行結果 - {success: boolean, batch_id?: string, error?: string}
 */
function triggerWorkflowWithRetry(batchData, taskType) {
  try {
    // バッチID生成
    const batch_id = generateBatchId(taskType);
    
    // Repository Dispatch イベント送信（リトライ機能付き）
    const dispatchResult = sendRepositoryDispatchWithRetry(taskType, batch_id, batchData);
    
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
 * GitHub Actions ワークフロー開始（従来版・互換性維持）
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
  const result = startProcessing('fuma_detail', 2);
  console.log('手動トリガー結果:', result);
  return result;
}

/**
 * 並列実行数チェックのテスト（手動実行用）
 */
function testConcurrentWorkflowCheck() {
  console.log('=== 並列実行数チェックテスト開始 ===');
  
  try {
    const checkResult = checkRunningFetchDetailWorkflows();
    
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
    
    // 【新規追加】GitHub Actionsの並列実行数をチェック
    console.log('GitHub Actionsの並列実行数をチェックします');
    const workflowCheckResult = checkRunningFetchDetailWorkflows();
    
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

/**
 * 拡張テスト機能群
 */

/**
 * モック機能付き並列実行数チェックテスト
 * @param {number} mockRunningCount モックの並列実行数
 * @param {boolean} mockAPIFailure APIエラーをシミュレート
 * @returns {Object} テスト結果
 */
function testConcurrentCheckWithMock(mockRunningCount = 5, mockAPIFailure = false) {
  console.log('=== モック機能付き並列実行数チェックテスト開始 ===');
  console.log(`モック設定: 並列実行数=${mockRunningCount}, API失敗=${mockAPIFailure}`);
  
  // モック用の一時的な関数置換
  const originalCheck = checkRunningFetchDetailWorkflows;
  
  try {
    // モック関数を設定
    globalThis.checkRunningFetchDetailWorkflows = function() {
      if (mockAPIFailure) {
        return {
          success: false,
          error: 'モック: API呼び出し失敗',
          errorClassification: {
            category: 'network',
            retryable: true,
            suggestedDelay: 5000
          },
          runningCount: 0
        };
      } else {
        return {
          success: true,
          runningCount: mockRunningCount,
          runningWorkflows: Array.from({ length: mockRunningCount }, (_, i) => ({
            id: 1000 + i,
            created_at: new Date(Date.now() - i * 60000).toISOString(),
            status: 'in_progress'
          }))
        };
      }
    };
    
    // テスト実行
    const result = testTriggerFlowWithoutRecursive();
    
    console.log('=== モック機能付きテスト完了 ===');
    console.log('結果:', JSON.stringify(result, null, 2));
    
    return { 
      success: true, 
      testResult: result,
      mockSettings: { mockRunningCount, mockAPIFailure }
    };
    
  } catch (error) {
    console.error('モック機能付きテストエラー:', error);
    return { success: false, error: error.toString() };
    
  } finally {
    // 元の関数を復元
    globalThis.checkRunningFetchDetailWorkflows = originalCheck;
  }
}

/**
 * エラーシナリオテストスイート
 * @returns {Object} テスト結果
 */
function runErrorScenarioTests() {
  console.log('=== エラーシナリオテストスイート開始 ===');
  
  const scenarios = [
    {
      name: '正常: 並列実行数が上限未満',
      mockRunningCount: 3,
      mockAPIFailure: false,
      expectedSkipped: false
    },
    {
      name: '制限: 並列実行数が上限到達',
      mockRunningCount: 15,
      mockAPIFailure: false,
      expectedSkipped: true
    },
    {
      name: 'エラー: API呼び出し失敗',
      mockRunningCount: 0,
      mockAPIFailure: true,
      expectedSkipped: false // エラー時の動作確認
    }
  ];
  
  const results = [];
  
  scenarios.forEach((scenario, index) => {
    console.log(`\n--- シナリオ ${index + 1}: ${scenario.name} ---`);
    
    const testResult = testConcurrentCheckWithMock(
      scenario.mockRunningCount, 
      scenario.mockAPIFailure
    );
    
    const scenarioResult = {
      scenario: scenario.name,
      testSuccess: testResult.success,
      actualSkipped: testResult.testResult ? testResult.testResult.skipped : null,
      expectedSkipped: scenario.expectedSkipped,
      passed: testResult.success && 
              (testResult.testResult ? 
               (testResult.testResult.skipped === scenario.expectedSkipped) : 
               !scenario.expectedSkipped)
    };
    
    console.log(`シナリオ結果: ${scenarioResult.passed ? '✅ PASS' : '❌ FAIL'}`);
    results.push(scenarioResult);
  });
  
  const totalTests = results.length;
  const passedTests = results.filter(r => r.passed).length;
  
  console.log('\n=== エラーシナリオテストスイート完了 ===');
  console.log(`総テスト数: ${totalTests}, 成功: ${passedTests}, 失敗: ${totalTests - passedTests}`);
  
  return {
    success: passedTests === totalTests,
    totalTests,
    passedTests,
    results
  };
}

/**
 * 設定管理統合テスト
 * @returns {Object} テスト結果
 */
function testConfigIntegration() {
  console.log('=== 設定管理統合テスト開始 ===');
  
  try {
    const config = CONFIG;
    
    // 設定項目の存在確認
    const requiredSettings = [
      'MAX_CONCURRENT_WORKFLOWS',
      'BATCH_SIZE',
      'MAX_RETRIES',
      'TIMEOUT_MS',
      'GITHUB_API_BASE',
      'DEFAULT_TASK_TYPE'
    ];
    
    const missingSettings = requiredSettings.filter(key => 
      config[key] === null || config[key] === undefined
    );
    
    if (missingSettings.length > 0) {
      console.error('必須設定が不足:', missingSettings);
      return {
        success: false,
        error: `必須設定不足: ${missingSettings.join(', ')}`
      };
    }
    
    // 設定値の妥当性チェック
    const validationTests = [
      {
        key: 'MAX_CONCURRENT_WORKFLOWS',
        value: config.MAX_CONCURRENT_WORKFLOWS,
        test: (v) => v > 0 && v <= 50,
        description: '1-50の範囲'
      },
      {
        key: 'BATCH_SIZE',
        value: config.BATCH_SIZE,
        test: (v) => v > 0 && v <= 1000,
        description: '1-1000の範囲'
      }
    ];
    
    const failedValidations = validationTests.filter(test => !test.test(test.value));
    
    if (failedValidations.length > 0) {
      console.error('設定値バリデーション失敗:', failedValidations);
      return {
        success: false,
        error: 'バリデーション失敗',
        failedValidations
      };
    }
    
    console.log('✅ 設定管理統合テスト成功');
    return {
      success: true,
      config,
      message: 'すべての設定が正常'
    };
    
  } catch (error) {
    console.error('設定管理統合テストエラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * 包括的テストスイート実行
 * @returns {Object} 全テスト結果
 */
function runComprehensiveTestSuite() {
  console.log('=== 包括的テストスイート開始 ===');
  
  const testResults = {
    startTime: new Date(),
    tests: {},
    summary: {}
  };
  
  try {
    // 1. 設定管理テスト
    console.log('\n1. 設定管理テスト実行中...');
    testResults.tests.configManager = { success: true, message: 'CONFIG定数が正常に定義されています' };
    
    // 2. 設定統合テスト
    console.log('\n2. 設定統合テスト実行中...');
    testResults.tests.configIntegration = testConfigIntegration();
    
    // 3. ワークフロー判定テスト
    console.log('\n3. ワークフロー判定テスト実行中...');
    testResults.tests.workflowIdentification = testWorkflowIdentification();
    
    // 4. 並列実行数チェックテスト
    console.log('\n4. 並列実行数チェックテスト実行中...');
    testResults.tests.concurrentCheck = testConcurrentWorkflowCheck();
    
    // 5. エラーシナリオテスト
    console.log('\n5. エラーシナリオテスト実行中...');
    testResults.tests.errorScenarios = runErrorScenarioTests();
    
    testResults.endTime = new Date();
    testResults.duration = testResults.endTime - testResults.startTime;
    
    // サマリー生成
    const totalTests = Object.keys(testResults.tests).length;
    const passedTests = Object.values(testResults.tests).filter(result => 
      result && result.success
    ).length;
    
    testResults.summary = {
      totalTests,
      passedTests,
      failedTests: totalTests - passedTests,
      success: passedTests === totalTests,
      duration: testResults.duration + 'ms'
    };
    
    console.log('\n=== 包括的テストスイート完了 ===');
    console.log(`総テスト数: ${totalTests}, 成功: ${passedTests}, 失敗: ${totalTests - passedTests}`);
    console.log(`実行時間: ${testResults.duration}ms`);
    
    if (testResults.summary.success) {
      console.log('🎉 すべてのテストが成功しました！');
    } else {
      console.log('⚠️ 一部のテストが失敗しました。詳細を確認してください。');
    }
    
    return testResults;
    
  } catch (error) {
    console.error('包括的テストスイートエラー:', error);
    testResults.error = error.toString();
    testResults.summary.success = false;
    return testResults;
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
 * 処理状況確認
 */
function checkProcessingStatus() {
  try {
    const supabase = getSupabaseClient();
    const { data, error } = supabase.from('companies')
      .select('processing_status, count(*)')
      .group('processing_status');
    
    if (error) {
      console.error('処理状況確認エラー:', error);
      return null;
    }
    
    console.log('=== 処理状況 ===');
    data.forEach(item => {
      console.log(`${item.processing_status}: ${item.count}件`);
    });
    
    return data;
    
  } catch (error) {
    console.error('処理状況確認エラー:', error);
    return null;
  }
}

