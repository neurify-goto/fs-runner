/**
 * 統計情報自動更新システム
 * Supabaseから統計データを取得してスプレッドシートに定期更新する
 */

// 設定定数
const CONFIG = {
  TRIGGER_INTERVAL_MINUTES: 1, // 実行間隔（分）
  SYSTEM_NAME: 'stats-updater',
  VERSION: '1.0.0'
};

/**
 * ★ 1分ごとの定期実行用エントリーポイント
 * この関数をGASトリガーに設定してください
 */
function startStatsUpdateTrigger() {
  console.log('定期実行トリガーにより統計更新処理を開始します');
  updateStats();
}

/**
 * 統計情報更新のメイン処理
 * Supabaseから統計を取得してスプレッドシートを更新
 * @returns {Object} 処理結果
 */
function updateStats() {
  try {
    const startTime = new Date();
    console.log(`統計更新処理開始: ${Utilities.formatDate(startTime, 'Asia/Tokyo', 'yyyy/MM/dd HH:mm:ss')}`);
    
    // 1. Supabaseから統計情報を取得
    const statsResult = getCompaniesStats();
    
    if (!statsResult.success) {
      console.error('統計情報取得失敗:', statsResult.error);
      return {
        success: false,
        error: `統計情報取得失敗: ${statsResult.error}`,
        timestamp: startTime
      };
    }
    
    console.log(`統計情報取得成功（${statsResult.queryType || 'UNKNOWN'}）: 処理時間 ${statsResult.processingTime || '不明'}ms`);
    console.log('統計情報:', statsResult.data);
    
    // 2. 現在時刻を取得（HH:MM形式）
    const currentTime = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'HH:mm');
    
    // 3. 1時間前のデータを取得
    const data1HourAgo = get1HourAgoData(currentTime);
    
    // 4. 24時間前のデータを取得
    const data24HourAgo = get24HourAgoData(currentTime);
    
    // 5. 1時間変化を計算
    const changes1Hour = calculate1HourChanges(statsResult.data, data1HourAgo);
    console.log('1時間変化:', changes1Hour);
    
    // 6. 24時間変化を計算
    const changes24Hour = calculateChanges(statsResult.data, data24HourAgo);
    console.log('24時間変化:', changes24Hour);
    
    // 7. 統計ログを更新
    const logResult = updateStatsLog(currentTime, statsResult.data);
    if (!logResult.success) {
      console.warn('統計ログ更新に失敗しましたが処理を継続します:', logResult.error);
    }
    
    // 8. スプレッドシートを更新（統計データ + 1時間変化 + 24時間変化）
    const updateResult = updateSpreadsheet(statsResult.data, changes1Hour, changes24Hour);
    
    if (!updateResult.success) {
      console.error('スプレッドシート更新失敗:', updateResult.error);
      return {
        success: false,
        error: `スプレッドシート更新失敗: ${updateResult.error}`,
        timestamp: startTime,
        stats: statsResult.data,
        changes1Hour: changes1Hour,
        changes24Hour: changes24Hour
      };
    }
    
    const endTime = new Date();
    const processingTime = endTime.getTime() - startTime.getTime();
    
    console.log(`統計更新処理完了: 処理時間 ${processingTime}ms`);
    
    return {
      success: true,
      message: '統計情報の更新が完了しました',
      processingTime: processingTime,
      stats: statsResult.data,
      changes1Hour: changes1Hour,
      changes24Hour: changes24Hour,
      currentTime: currentTime,
      has1HourData: data1HourAgo && data1HourAgo.hasData,
      has24HourData: data24HourAgo && data24HourAgo.hasData,
      updatedAt: updateResult.updatedAt,
      timestamp: startTime
    };
    
  } catch (error) {
    console.error('統計更新処理エラー:', error);
    return {
      success: false,
      error: error.toString(),
      timestamp: new Date()
    };
  }
}

/**
 * 定期実行トリガーをセットアップ
 * @returns {Object} セットアップ結果
 */
function setupTrigger() {
  try {
    console.log('定期実行トリガーセットアップ開始');
    
    // 既存のトリガーを削除
    deleteTriggers();
    
    // 新しいトリガーを作成（1分ごと）
    const trigger = ScriptApp.newTrigger('startStatsUpdateTrigger')
      .timeBased()
      .everyMinutes(CONFIG.TRIGGER_INTERVAL_MINUTES)
      .create();
    
    console.log(`定期実行トリガー作成完了: ID=${trigger.getUniqueId()}, 間隔=${CONFIG.TRIGGER_INTERVAL_MINUTES}分`);
    
    return {
      success: true,
      message: `定期実行トリガーを設定しました（${CONFIG.TRIGGER_INTERVAL_MINUTES}分ごと）`,
      triggerId: trigger.getUniqueId(),
      interval: CONFIG.TRIGGER_INTERVAL_MINUTES
    };
    
  } catch (error) {
    console.error('トリガーセットアップエラー:', error);
    return {
      success: false,
      error: error.toString()
    };
  }
}

/**
 * 定期実行トリガーを削除
 * @returns {Object} 削除結果
 */
function deleteTriggers() {
  try {
    console.log('既存トリガー削除開始');
    
    const triggers = ScriptApp.getProjectTriggers();
    let deletedCount = 0;
    
    triggers.forEach(trigger => {
      if (trigger.getHandlerFunction() === 'startStatsUpdateTrigger') {
        console.log(`トリガー削除: ID=${trigger.getUniqueId()}`);
        ScriptApp.deleteTrigger(trigger);
        deletedCount++;
      }
    });
    
    console.log(`トリガー削除完了: ${deletedCount}個削除`);
    
    return {
      success: true,
      message: `${deletedCount}個のトリガーを削除しました`,
      deletedCount: deletedCount
    };
    
  } catch (error) {
    console.error('トリガー削除エラー:', error);
    return {
      success: false,
      error: error.toString()
    };
  }
}

/**
 * 現在のトリガー状況を確認
 * @returns {Object} トリガー情報
 */
function checkTriggers() {
  try {
    console.log('=== トリガー状況確認 ===');
    
    const triggers = ScriptApp.getProjectTriggers();
    const statsTriggers = triggers.filter(trigger => 
      trigger.getHandlerFunction() === 'startStatsUpdateTrigger'
    );
    
    console.log(`統計更新トリガー数: ${statsTriggers.length}`);
    
    statsTriggers.forEach((trigger, index) => {
      const eventType = trigger.getEventType();
      const triggerId = trigger.getUniqueId();
      console.log(`${index + 1}. ID: ${triggerId}, Type: ${eventType}`);
    });
    
    return {
      success: true,
      totalTriggers: triggers.length,
      statsTriggers: statsTriggers.length,
      triggers: statsTriggers.map(trigger => ({
        id: trigger.getUniqueId(),
        eventType: trigger.getEventType(),
        handlerFunction: trigger.getHandlerFunction()
      }))
    };
    
  } catch (error) {
    console.error('トリガー確認エラー:', error);
    return {
      success: false,
      error: error.toString()
    };
  }
}

/**
 * システム全体のテスト実行
 * @returns {Object} テスト結果
 */
function runFullTest() {
  console.log('=== システム全体テスト開始 ===');
  
  const results = {
    supabaseConnection: null,
    spreadsheetConnection: null,
    statsRetrieval: null,
    spreadsheetUpdate: null,
    fullProcess: null
  };
  
  try {
    // 1. Supabase接続テスト
    console.log('1. Supabase接続テスト');
    results.supabaseConnection = testSupabaseConnection();
    
    // 2. スプレッドシート接続テスト
    console.log('2. スプレッドシート接続テスト');
    results.spreadsheetConnection = testSpreadsheetConnection();
    
    // 3. 統計情報取得テスト
    console.log('3. 統計情報取得テスト');
    results.statsRetrieval = testGetStats();
    
    // 4. スプレッドシート更新テスト
    console.log('4. スプレッドシート更新テスト');
    results.spreadsheetUpdate = testSpreadsheetUpdate();
    
    // 5. 全体処理テスト
    console.log('5. 全体処理テスト');
    results.fullProcess = updateStats();
    
    // テスト結果のサマリー
    const allSuccess = Object.values(results).every(result => result && result.success);
    
    console.log('=== システム全体テスト完了 ===');
    console.log(`全体結果: ${allSuccess ? '成功' : '一部失敗'}`);
    
    return {
      success: allSuccess,
      message: allSuccess ? '全テストに成功しました' : '一部のテストで失敗があります',
      results: results,
      timestamp: new Date()
    };
    
  } catch (error) {
    console.error('システム全体テストエラー:', error);
    return {
      success: false,
      error: error.toString(),
      results: results,
      timestamp: new Date()
    };
  }
}

/**
 * targeting submissions機能の統合テスト実行
 * @returns {Object} テスト結果
 */
function runTargetingSubmissionsFullTest() {
  console.log('=== targeting submissions機能 統合テスト開始（効率化版） ===');
  
  const results = {
    supabaseConnection: null,
    targetingQueries: null,
    spreadsheetConnection: null,
    targetingStatsUpdate: null
  };
  
  try {
    // 1. Supabase接続テスト
    console.log('1. Supabase接続テスト');
    results.supabaseConnection = testSupabaseConnection();
    
    // 2. targeting関連クエリテスト（効率化版含む）
    console.log('2. targeting関連クエリテスト（効率化版含む）');
    results.targetingQueries = testTargetingQueries();
    
    // 3. スプレッドシート接続テスト
    console.log('3. スプレッドシート接続テスト');
    results.spreadsheetConnection = testSpreadsheetConnection();
    
    // 4. targeting submissions統計更新テスト（効率化版）
    console.log('4. targeting submissions統計更新テスト（効率化版）');
    results.targetingStatsUpdate = testTargetingSubmissionsStatsUpdate();
    
    // テスト結果のサマリー
    const allSuccess = Object.values(results).every(result => result && result.success);
    
    console.log('=== targeting submissions機能 統合テスト完了（効率化版） ===');
    console.log(`全体結果: ${allSuccess ? '成功' : '一部失敗'}`);
    
    // 効率化の効果を表示
    if (results.targetingQueries && results.targetingQueries.success) {
      const queryResults = results.targetingQueries.results;
      if (queryResults.individualStatsComparison) {
        const comparison = queryResults.individualStatsComparison;
        console.log('効率化効果:');
        console.log(`- 一括取得時間: ${queryResults.batchStatsRetrieval.processing_time_ms}ms`);
        console.log(`- 個別取得時間: ${comparison.individual_time_ms}ms`);
        console.log(`- 処理時間短縮: ${comparison.individual_time_ms > 0 ? Math.round(((comparison.individual_time_ms - queryResults.batchStatsRetrieval.processing_time_ms) / comparison.individual_time_ms) * 100) : 0}%`);
        console.log(`- 結果一致性: ${comparison.results_match ? 'OK' : 'NG'}`);
      }
    }
    
    if (results.targetingStatsUpdate && results.targetingStatsUpdate.success) {
      console.log('targeting submissions統計更新結果:');
      console.log(`- 更新対象: ${results.targetingStatsUpdate.totalRows}行`);
      console.log(`- 成功: ${results.targetingStatsUpdate.successRows}行`);
      console.log(`- エラー: ${results.targetingStatsUpdate.errorRows}行`);
      console.log(`- 処理時間: ${results.targetingStatsUpdate.processingTime || '不明'}ms`);
    }
    
    return {
      success: allSuccess,
      message: allSuccess ? 'すべてのtargeting submissions機能テストに成功しました（効率化版）' : '一部のテストで失敗があります',
      results: results,
      timestamp: new Date()
    };
    
  } catch (error) {
    console.error('targeting submissions機能統合テストエラー:', error);
    return {
      success: false,
      error: error.toString(),
      results: results,
      timestamp: new Date()
    };
  }
}

/**
 * 手動実行用：統計更新を1回だけ実行
 * @returns {Object} 実行結果
 */
function runOnce() {
  console.log('=== 手動実行：統計更新1回実行 ===');
  const result = updateStats();
  
  if (result.success) {
    console.log('手動実行成功:', result.message);
  } else {
    console.error('手動実行失敗:', result.error);
  }
  
  return result;
}

/**
 * 設定確認
 * @returns {Object} 設定情報
 */
function checkConfiguration() {
  const properties = PropertiesService.getScriptProperties().getProperties();
  const requiredKeys = ['SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY'];
  
  console.log('=== 設定確認 ===');
  requiredKeys.forEach(key => {
    const hasValue = properties[key] ? 'OK' : 'MISSING';
    console.log(`${key}: ${hasValue}`);
  });
  
  return {
    hasAllRequired: requiredKeys.every(key => properties[key]),
    properties: Object.fromEntries(
      requiredKeys.map(key => [key, properties[key] ? '設定済み' : '未設定'])
    )
  };
}

/**
 * システム情報表示
 */
function showSystemInfo() {
  console.log('=== 統計情報自動更新システム ===');
  console.log(`システム名: ${CONFIG.SYSTEM_NAME}`);
  console.log(`バージョン: ${CONFIG.VERSION}`);
  console.log(`実行間隔: ${CONFIG.TRIGGER_INTERVAL_MINUTES}分ごと`);
  console.log(`対象スプレッドシート: ${SPREADSHEET_CONFIG.SHEET_ID}`);
  console.log(`対象シート: ${SPREADSHEET_CONFIG.SHEET_NAME}`);
  console.log('=====================================');
}

/**
 * targeting submissions統計更新のメイン実行関数
 * targetingテーブルの各行のid列を使ってM列・N列に統計を書き込む
 * @returns {Object} 実行結果
 */
function updateTargetingStats() {
  try {
    const startTime = new Date();
    console.log(`targeting submissions統計更新処理開始: ${Utilities.formatDate(startTime, 'Asia/Tokyo', 'yyyy/MM/dd HH:mm:ss')}`);
    
    // targeting submissions統計を更新
    const result = updateTargetingSubmissionsStats();
    
    const endTime = new Date();
    const processingTime = endTime.getTime() - startTime.getTime();
    
    if (result.success) {
      console.log(`targeting submissions統計更新処理完了: 処理時間 ${processingTime}ms`);
      return {
        success: true,
        message: 'targeting submissions統計の更新が完了しました',
        processingTime: processingTime,
        updatedAt: result.updatedAt,
        totalRows: result.totalRows,
        successRows: result.successRows,
        errorRows: result.errorRows,
        targetingIds: result.targetingIds,
        timestamp: startTime
      };
    } else {
      console.error(`targeting submissions統計更新処理失敗: ${result.error}`);
      return {
        success: false,
        error: `targeting submissions統計更新失敗: ${result.error}`,
        processingTime: processingTime,
        timestamp: startTime
      };
    }
    
  } catch (error) {
    console.error('targeting submissions統計更新処理エラー:', error);
    return {
      success: false,
      error: error.toString(),
      timestamp: new Date()
    };
  }
}

/**
 * 手動実行用：targeting submissions統計更新を1回だけ実行
 * @returns {Object} 実行結果
 */
function runTargetingStatsOnce() {
  console.log('=== 手動実行：targeting submissions統計更新1回実行 ===');
  const result = updateTargetingStats();
  
  if (result.success) {
    console.log('手動実行成功:', result.message);
    console.log(`処理サマリー: 対象${result.totalRows}行, 成功${result.successRows}行, エラー${result.errorRows}行`);
  } else {
    console.error('手動実行失敗:', result.error);
  }
  
  return result;
}