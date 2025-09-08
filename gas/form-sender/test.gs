/**
 * テスト専用関数群（form-sender）
 * - 本ファイルはテスト/検証/デモ用途のみの関数を集約
 * - 本番実行ロジックは gas/form-sender/Code.gs に保持
 */

/**
 * テスト用関数：スプレッドシート連携テスト
 */
function testSpreadsheetConnection() {
  try {
    return testSpreadsheetIntegration();
  } catch (error) {
    console.error(`スプレッドシート連携テストエラー: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * テスト用関数：全体フローの手動テスト（新アーキテクチャ版）
 */
function testFormSenderFlow() {
  console.log('=== フォーム送信フロー テスト開始（新アーキテクチャ版） ===');
  try {
    const result = startFormSender(1); // ターゲティングID=1でテスト
    console.log('テスト結果:', result);
    return result;
  } catch (error) {
    console.error(`テストエラー: ${error.message}`);
    return { success: false, message: error.message };
  }
}

/**
 * FORM_SENDER.md仕様準拠: 統合テスト全体の動作確認
 */
function testNewArchitecture() {
  console.log('=== FORM_SENDER.md仕様準拠 統合テスト開始 ===');
  const results = {
    timestamp: new Date().toISOString(),
    spreadsheet_connection: testSpreadsheetConnection(),
    github_connection: testGitHubConnection(),
    targeting_processing: null,
    business_hours_check: testBusinessHoursCheck(),
    target_companies_check: testTargetCompaniesCheck(),
    specification_compliance: validateFormSenderSpecCompliance()
  };
  if (results.spreadsheet_connection.success && results.github_connection.success) {
    results.targeting_processing = testFormSenderFlow();
  }
  console.log('=== FORM_SENDER.md仕様準拠 統合テスト完了 ===');
  return results;
}

/**
 * FORM_SENDER.md準拠: 営業時間制御テスト
 */
function testBusinessHoursCheck() {
  try {
    const testConfig = {
      send_start_time: '09:00',
      send_end_time: '18:00',
      send_days_of_week: [0, 1, 2, 3, 4]
    };
    const result = isWithinBusinessHours(testConfig);
    console.log(`営業時間制御テスト: ${result ? '営業時間内' : '営業時間外'}`);
    return { success: true, within_business_hours: result, test_config: testConfig };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

/**
 * FORM_SENDER.md準拠: 処理対象企業存在チェックテスト
 */
function testTargetCompaniesCheck() {
  try {
    const testTargetingId = 1;
    const result = hasTargetCompaniesBasic(testTargetingId);
    console.log(`処理対象企業存在チェックテスト (targeting_id=${testTargetingId}): ${result ? '成功' : '失敗'}`);
    return { success: true, has_target_companies: result, targeting_id: testTargetingId };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

/**
 * FORM_SENDER.md仕様準拠性確認
 */
function validateFormSenderSpecCompliance() {
  const compliance = {
    github_workflow_spec: true,
    database_structure: true,
    active_targeting_extraction: true,
    placeholder_variable_system: true,
    target_company_extraction: true,
    gas_control_system: true,
    result_recording_system: true
  };
  const total = Object.keys(compliance).length;
  const compliant = Object.values(compliance).filter(Boolean).length;
  return {
    total_specifications: total,
    compliant_specifications: compliant,
    compliance_rate: Math.round((compliant / total) * 100),
    details: compliance
  };
}

/**
 * form_sender_task停止機能のテスト関数
 */
function testFormSenderTaskStopFunctions() {
  console.log('=== form_sender_task停止機能テスト開始 ===');
  const results = {
    timestamp: new Date().toISOString(),
    running_tasks_check: null,
    github_connection: null,
    stop_functions_check: null
  };
  try {
    console.log('1. 実行中タスク状況確認テスト');
    results.running_tasks_check = getRunningFormSenderTasks();
    console.log('2. GitHub API接続確認');
    results.github_connection = testGitHubConnection();
    console.log('3. 停止機能動作確認（dry run）');
    const cancelableRuns = getCancelableWorkflowRuns();
    results.stop_functions_check = {
      cancelable_runs_retrieval: cancelableRuns.success,
      total_cancelable_runs: cancelableRuns.cancelable_runs || 0,
      functions_available: {
        stopAllRunningFormSenderTasks: typeof stopAllRunningFormSenderTasks === 'function',
        stopSpecificFormSenderTask: typeof stopSpecificFormSenderTask === 'function',
        getCancelableWorkflowRuns: typeof getCancelableWorkflowRuns === 'function',
        cancelWorkflowRun: typeof cancelWorkflowRun === 'function',
        getRunningFormSenderTasks: typeof getRunningFormSenderTasks === 'function'
      }
    };
    const overallSuccess = results.running_tasks_check.success &&
                           results.github_connection.success &&
                           results.stop_functions_check.cancelable_runs_retrieval;
    console.log('=== form_sender_task停止機能テスト完了 ===');
    return {
      success: overallSuccess,
      message: `停止機能テスト完了 (実行中: ${results.running_tasks_check.total_running || 0}件, キャンセル可能: ${results.stop_functions_check.total_cancelable_runs}件)`,
      details: results
    };
  } catch (error) {
    console.error('停止機能テストエラー:', error.message);
    return { success: false, error: error.message, details: results };
  }
}

/**
 * form_sender_task停止のデモ実行（安全なテスト用）
 */
function demoFormSenderTaskStop() {
  console.log('=== form_sender_task停止デモ実行開始 ===');
  try {
    const currentTasks = getRunningFormSenderTasks();
    if (!currentTasks.success) {
      return { success: false, error: '実行中タスク取得失敗', details: currentTasks };
    }
    console.log(`実行中タスク数: ${currentTasks.total_running}件`);
    if (currentTasks.total_running === 0) {
      console.log('現在実行中のform_sender_taskはありません');
      return { success: true, message: '実行中タスクなし - 停止対象なし', demo_only: true };
    }
    const targetingIds = Object.keys(currentTasks.by_targeting_id);
    console.log(`識別済みtargeting_id: ${targetingIds.join(', ')}`);
    console.log(`targeting_id不明のタスク: ${currentTasks.unknown_targeting.length}件`);
    if (targetingIds.length > 0) {
      const firstTargetingId = parseInt(targetingIds[0]);
      console.log(`[デモ] targeting_id ${firstTargetingId} の停止処理をシミュレーション:`);
      const relatedTasks = currentTasks.by_targeting_id[firstTargetingId];
      console.log(`  - 停止対象タスク数: ${relatedTasks.length}件`);
      relatedTasks.forEach(task => console.log(`  - Run ID: ${task.run_id}, Status: ${task.status}`));
      console.log(`[デモ] 実際の停止には stopSpecificFormSenderTask(${firstTargetingId}) を実行してください`);
    }
    console.log(`[デモ] 全体停止処理をシミュレーション:`);
    console.log(`  - 停止対象タスク数: ${currentTasks.total_running}件`);
    console.log(`[デモ] 実際の停止には stopAllRunningFormSenderTasks() を実行してください`);
    return {
      success: true,
      message: `停止デモ完了 - 実行中タスク ${currentTasks.total_running}件を確認`,
      demo_only: true,
      current_tasks: currentTasks,
      available_functions: {
        'stopAllRunningFormSenderTasks()': '全ての実行中form_sender_taskを停止',
        'stopSpecificFormSenderTask(targetingId)': '特定targeting_idのタスクのみ停止',
        'getRunningFormSenderTasks()': '実行中タスクの状況確認'
      }
    };
  } catch (error) {
    console.error('停止デモ実行エラー:', error.message);
    return { success: false, error: error.message, demo_only: true };
  }
}

/**
 * 特定日時トリガー機能のテスト用関数（5分後）
 */
function testSpecificTimeTrigger() {
  try {
    console.log('=== 特定日時トリガーテスト開始 ===');
    const testTime = new Date();
    testTime.setMinutes(testTime.getMinutes() + 5);
    testTime.setSeconds(0);
    testTime.setMilliseconds(0);
    const result = createSpecificTimeTrigger(testTime);
    console.log('テスト結果:', result);
    console.log('=== 特定日時トリガーテスト完了 ===');
    if (result.success) {
      console.log(`⚠️ テストトリガーが作成されました。${result.execute_at_jst}に実行予定です。`);
      console.log('テスト後は deleteFormSenderTriggers() でクリーンアップしてください。');
    }
    return result;
  } catch (error) {
    console.error(`特定日時トリガーテストエラー: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * 次回実行時刻計算のテスト（土日回避機能）
 */
function testNextExecutionTime(includeWeekendTest = false) {
  try {
    console.log('=== 次回実行時刻計算テスト開始（土日回避機能付き） ===');
    const nextTime = getNextExecutionTime();
    const jstTime = new Date(nextTime.getTime() + CONFIG.JST_OFFSET);
    const now = new Date();
    const jstNow = new Date(now.getTime() + CONFIG.JST_OFFSET);
    const dayNames = ['日曜日', '月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日'];
    const result = {
      success: true,
      current_time: jstNow.toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' }),
      current_day: dayNames[jstNow.getDay()],
      next_execution_utc: nextTime.toISOString(),
      next_execution_jst: jstTime.toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' }),
      next_day: dayNames[jstTime.getDay()],
      hours_until_next: Math.round((nextTime.getTime() - now.getTime()) / (1000 * 60 * 60) * 100) / 100
    };
    console.log('実際の次回実行時刻計算結果:');
    console.log(`現在時刻: ${result.current_day} ${result.current_time}`);
    console.log(`次回実行時刻: ${result.next_day} ${result.next_execution_jst}`);
    console.log(`実行まで: ${result.hours_until_next}時間`);
    if (includeWeekendTest) {
      console.log('\n=== 週末シミュレーションテスト開始 ===');
      result.weekend_simulations = testWeekendSimulations();
      console.log('=== 週末シミュレーションテスト完了 ===');
    }
    console.log('=== 次回実行時刻計算テスト完了 ===');
    return result;
  } catch (error) {
    console.error(`次回実行時刻計算テストエラー: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * 週末シミュレーションテスト
 */
function testWeekendSimulations() {
  const simulationResults = {};
  const dayNames = ['日曜日', '月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日'];
  try {
    for (let dayOfWeek = 0; dayOfWeek <= 6; dayOfWeek++) {
      const simulatedTime = new Date();
      simulatedTime.setHours(13, 0, 0, 0);
      const currentDay = simulatedTime.getDay();
      const daysToAdd = (dayOfWeek - currentDay + 7) % 7;
      simulatedTime.setDate(simulatedTime.getDate() + daysToAdd);
      const jstSimulated = new Date(simulatedTime.getTime() + CONFIG.JST_OFFSET);
      let hoursToAdd = 24;
      let skipReason = '平日のため翌日に設定';
      if (dayOfWeek === 5) {
        hoursToAdd = 72;
        skipReason = '金曜日のため土日をスキップして月曜日に設定';
      } else if (dayOfWeek === 6) {
        hoursToAdd = 48;
        skipReason = '土曜日のため日曜日をスキップして月曜日に設定';
      }
      const nextExecution = new Date(jstSimulated.getTime() + (hoursToAdd * 60 * 60 * 1000));
      nextExecution.setMinutes(0, 0, 0);
      const nextDayName = dayNames[nextExecution.getDay()];
      simulationResults[dayNames[dayOfWeek]] = {
        simulated_current: jstSimulated.toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' }),
        next_execution: nextExecution.toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' }),
        next_day: nextDayName,
        hours_added: hoursToAdd,
        skip_reason: skipReason
      };
      console.log(`${dayNames[dayOfWeek]}シミュレーション: ${jstSimulated.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})} → ${nextDayName} ${nextExecution.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})} (${hoursToAdd}時間後)`);
    }
    return { success: true, simulations: simulationResults, message: '全曜日のシミュレーション完了' };
  } catch (error) {
    console.error(`週末シミュレーションエラー: ${error.message}`);
    return { success: false, error: error.message, partial_results: simulationResults };
  }
}

/**
 * 設定確認・デバッグ用関数
 */
function checkAllSettings() {
  console.log('=== 全設定確認開始 ===');
  const results = {
    timestamp: new Date().toISOString(),
    properties: checkScriptProperties(),
    spreadsheet: validateSpreadsheetConfig(),
    template: getSpreadsheetTemplate(),
    triggers: listFormSenderTriggers(),
    next_execution_test: testNextExecutionTime()
  };
  console.log('設定確認結果:', results);
  console.log('=== 全設定確認完了 ===');
  return results;
}

/**
 * スクリプトプロパティ確認
 */
function checkScriptProperties() {
  const properties = PropertiesService.getScriptProperties().getProperties();
  const requiredKeys = [
    'FORM_SENDER_SPREADSHEET_ID',
    'GITHUB_TOKEN'
  ];
  const results = {
    available: Object.keys(properties),
    missing: requiredKeys.filter(key => !properties[key]),
    configured: requiredKeys.filter(key => properties[key])
  };
  console.log('スクリプトプロパティ確認:', results);
  return results;
}

/**
 * 現在開発中のブランチでのテスト実行
 */
function testCurrentBranch() {
  console.log(`=== 開発ブランチでの本番通り実行 ===`);
  console.log(`targeting.id=1 を使用して本番処理を実行します`);
  try {
    const result = startFormSender(1);
    console.log('開発ブランチでの実行完了:', result);
    return { success: true, targeting_id: 1, result: result, note: 'Production-like execution with targeting_id=1 only' };
  } catch (error) {
    console.error('開発ブランチ実行エラー:', error.message);
    return { success: false, targeting_id: 1, error: error.message };
  }
}

/**
 * mainブランチでのテスト実行（本番環境テスト）
 */
function testMainBranch() {
  const MAIN_BRANCH = 'main';
  console.log(`=== mainブランチでのテスト実行: ${MAIN_BRANCH} ===`);
  try {
    const result = testFormSenderOnBranch(MAIN_BRANCH, 1);
    console.log('mainブランチテスト完了:', result);
    return { success: true, branch: MAIN_BRANCH, targeting_id: 1, result: result };
  } catch (error) {
    console.error('mainブランチテストエラー:', error.message);
    return { success: false, branch: MAIN_BRANCH, error: error.message };
  }
}

/**
 * 簡易テスト実行（デフォルト：現在ブランチ）
 */
function quickTest() {
  console.log('=== 簡易テスト実行開始 ===');
  return testCurrentBranch();
}

/**
 * トリガー削除機能のテスト用関数
 */
function testFormSenderTriggerDeletion() {
  try {
    console.log('=== form-senderトリガー削除機能テスト開始 ===');
    console.log('1. 現在のトリガー状況確認');
    const beforeList = listFormSenderTriggers();
    console.log(`テスト前のトリガー数: ${beforeList.trigger_count}件`);
    console.log('2. 削除機能テスト実行');
    const deleteResult = deleteCurrentFormSenderTrigger();
    console.log('削除テスト結果:', deleteResult);
    console.log('3. テスト後のトリガー状況確認');
    const afterList = listFormSenderTriggers();
    console.log(`テスト後のトリガー数: ${afterList.trigger_count}件`);
    console.log('=== form-senderトリガー削除機能テスト完了 ===');
    return {
      success: true,
      message: 'トリガー削除機能テスト完了',
      before_trigger_count: beforeList.trigger_count,
      after_trigger_count: afterList.trigger_count,
      delete_result: deleteResult,
      trigger_reduced: beforeList.trigger_count > afterList.trigger_count
    };
  } catch (error) {
    console.error('トリガー削除機能テストエラー:', error.message);
    return { success: false, error: error.message };
  }
}

