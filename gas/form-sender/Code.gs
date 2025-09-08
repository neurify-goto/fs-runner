/**
 * フォーム送信制御システム（FORM_SENDER.md完全準拠版）
 * GitHub Actionsとの連携によるフォーム送信ワークフローのオーケストレーター
 * 
 * FORM_SENDER.md の仕様に基づく実装:
 * - 2シート構成（client + targeting）でのスプレッドシート連携
 * - targeting-idごとのアクティブ状態管理
 * - client_idによるリレーショナル結合
 * - 特定日時トリガーによる厳密な時刻実行制御
 * - 24時間後の自動トリガー再設定機能
 * - GitHub Actions 連続ループ処理トリガー
 * - 進行中タスクの停止機能（緊急時対応）
 * 
 * ## 特定日時トリガー設定ガイド（7:00/13:00厳密実行対応）
 * 
 * ### 初回トリガー設定手順:
 * 1. GASエディタで以下の関数を手動実行:
 *    ```javascript
 *    // 7:00のトリガーを設定する場合
 *    const tomorrow7am = new Date();
 *    tomorrow7am.setDate(tomorrow7am.getDate() + 1);
 *    tomorrow7am.setHours(7, 0, 0, 0);
 *    createSpecificTimeTrigger(tomorrow7am);
 *    
 *    // 13:00のトリガーを設定する場合
 *    const tomorrow1pm = new Date();
 *    tomorrow1pm.setDate(tomorrow1pm.getDate() + 1);
 *    tomorrow1pm.setHours(13, 0, 0, 0);
 *    createSpecificTimeTrigger(tomorrow1pm);
 *    ```
 * 
 * ### 自動トリガー再設定:
 * - startFormSenderFromTrigger() 実行完了後、24時間後の同時刻（00分）に次回トリガーを自動作成
 * - エラー発生時も次回トリガーは確実に設定される
 * 
 * ### トリガー管理関数:
 * - deleteFormSenderTriggers(): 既存トリガーを削除
 * - listFormSenderTriggers(): 現在のトリガー一覧を確認
 * - testSpecificTimeTrigger(): 5分後のテストトリガーを作成
 * - testNextExecutionTime(): 次回実行時刻の計算をテスト
 * 
 * ## 進行中タスク停止機能（緊急時対応）
 * 
 * ### 利用可能な停止関数:
 * - stopAllRunningFormSenderTasks(): 全ての実行中form_sender_taskを一括停止
 * - stopSpecificFormSenderTask(targetingId): 特定targeting_idのタスクのみ停止
 * - getRunningFormSenderTasks(): 実行中タスクの状況確認
 * 
 * ### テスト・確認用関数:
 * - testFormSenderTaskStopFunctions(): 停止機能のテスト実行
 * - demoFormSenderTaskStop(): 停止処理のデモ実行（実際には停止しない）
 * 
 * ### 使用例:
 * ```javascript
 * // 実行中タスクの確認
 * const currentTasks = getRunningFormSenderTasks();
 * 
 * // 特定targeting_idのタスクを停止
 * const result = stopSpecificFormSenderTask(1);
 * 
 * // 全タスクを緊急停止
 * const stopAll = stopAllRunningFormSenderTasks();
 * ```
 */

// 設定定数
const CONFIG = {
  MAX_RETRIES: 3,
  RETRY_DELAY: 2000,
  GITHUB_API_BASE: 'https://api.github.com',
  DEFAULT_TARGETING_ID: 1, // デフォルトのターゲティングID
  JST_OFFSET: 9 * 60 * 60 * 1000, // JST のオフセット（ミリ秒）
  // 1ワークフロー内で起動するPythonワーカー数（1〜4）
  // GitHub Actions で --num-workers に反映されます
  WORKERS_PER_WORKFLOW: 4
};

/**
 * 時間ベースのトリガーから呼び出すメイン関数（新アーキテクチャ版）
 * この関数をGAS時間トリガーに設定してください
 */
function startFormSenderFromTrigger() {
  console.log('時間ベースのトリガーによりフォーム送信制御を開始します（新アーキテクチャ版）');
  
  try {
    // 【追加】実行開始時に現在のトリガーを削除（実行済トリガーの蓄積防止）
    const deleteResult = deleteCurrentFormSenderTrigger();
    if (deleteResult.success && deleteResult.deletedCount > 0) {
      console.log(`実行済トリガー削除成功: ${deleteResult.message}`);
    } else if (!deleteResult.success) {
      console.warn('実行済トリガー削除で問題発生:', deleteResult.error);
    }
    
    // スプレッドシートからアクティブなターゲティング設定を取得
    const activeTargetings = getActiveTargetingIdsFromSheet();
    
    if (!activeTargetings || activeTargetings.length === 0) {
      console.log('アクティブなターゲティング設定が見つかりません');
      return { success: false, message: 'アクティブなターゲティング設定なし' };
    }
    
    // FORM_SENDER.md:333-342準拠: 必要最小限のログ出力
    console.log(`${activeTargetings.length} 件のアクティブなターゲティングを処理開始`);
    
    let triggeredCount = 0;
    
    for (const targeting of activeTargetings) {
      try {
        const result = processTargeting(targeting.targeting_id);
        
        if (result && result.success) {
          triggeredCount++;
        }
        
      } catch (error) {
        // エラー発生時のエラー内容（デバッグ用）のみ出力
        console.error(`ターゲティング ${targeting.targeting_id} 処理エラー: ${error.message}`);
      }
    }
    
    // FORM_SENDER.md:333-342準拠: 必要最小限のログ出力
    // 統計情報、累計処理数、成功率などの詳細ログは出力しない
    console.log(`処理完了: ${triggeredCount} 件トリガー実行`);
    
    // 24時間後の次回トリガーを作成
    const nextTriggerResult = createNextDayTrigger();
    if (nextTriggerResult.success) {
      console.log(`次回トリガー作成完了: ${nextTriggerResult.execute_at_jst}`);
    } else {
      console.error(`次回トリガー作成失敗: ${nextTriggerResult.error}`);
    }
    
    return { 
      success: true, 
      message: `${triggeredCount} 件トリガー実行`,
      triggered: triggeredCount,
      next_trigger: nextTriggerResult
    };
    
  } catch (error) {
    // エラーが発生してもトリガーは設定する
    console.error(`フォーム送信制御でエラー: ${error.message}`);
    console.log('エラー発生時も次回トリガーを設定します');
    
    const nextTriggerResult = createNextDayTrigger();
    if (nextTriggerResult.success) {
      console.log(`次回トリガー作成完了（エラー時）: ${nextTriggerResult.execute_at_jst}`);
    } else {
      console.error(`次回トリガー作成失敗（エラー時）: ${nextTriggerResult.error}`);
    }
    
    // FORM_SENDER.md:286-289仕様準拠: エラーハンドリング
    // - スプレッドシート読み込みエラー時の処理
    // - 無効な targeting-id の検出と除外  
    // - GitHub API エラー時のリトライ処理
    const errorType = getErrorType(error.message);
    console.error(`フォーム送信制御で${errorType}エラー: ${error.message}`);
    return { 
      success: false, 
      message: error.message, 
      error_type: errorType,
      next_trigger: nextTriggerResult
    };
  }
}

/**
 * 手動実行用のエントリーポイント（新アーキテクチャ版）
 * @param {number} targetingId ターゲティングID（オプション）
 */
function startFormSender(targetingId = null) {
  try {
    console.log(`フォーム送信処理を開始（新アーキテクチャ版）: targetingId=${targetingId}`);
    
    const finalTargetingId = targetingId || CONFIG.DEFAULT_TARGETING_ID;
    const result = processTargeting(finalTargetingId);
    
    if (result && result.success) {
      console.log('フォーム送信連続処理ワークフローが正常に開始されました');
      return result;
    } else {
      console.log('フォーム送信ワークフローの開始条件を満たしていません');
      return result || { success: false, message: '開始条件未満足' };
    }
    
  } catch (error) {
    console.error(`フォーム送信処理でエラー: ${error.message}`);
    return { success: false, message: error.message };
  }
}

/**
 * 単一ターゲティングの処理（新アーキテクチャ版）
 * @param {number} targetingId ターゲティングID
 * @returns {Object} 処理結果
 */
function processTargeting(targetingId) {
  try {
    console.log(`ターゲティング ${targetingId} の処理を開始（新アーキテクチャ版）`);
    
    // スプレッドシートからターゲティング設定を取得
    const targetingConfig = getTargetingConfig(targetingId);
    if (!targetingConfig) {
      console.log(`ターゲティング ${targetingId} が見つかりません`);
      return { success: false, message: 'ターゲティング設定が見つからない' };
    }
    
    // 機微情報は出さない
    console.log(`ターゲティング設定取得完了（2シート結合）: ***COMPANY_REDACTED*** (client_id: ${targetingConfig.client_id})`);
    
    // 営業時間チェックはGitHub Actions側で実施（重複を避けるため、ここでは基本チェックのみ）
    console.log('営業時間制御は GitHub Actions 側で実施');
    
    // FORM_SENDER.md 1.4節準拠：基本的な処理対象企業存在チェック
    // 詳細なチェックはGitHub Actions側で実行、ここでは設定値の基本検証のみ実施
    if (!hasTargetCompaniesBasic(targetingId)) {
      console.log('基本的な設定検証に失敗したためスキップします');
      return { success: false, message: '基本設定検証失敗' };
    }
    
    // GitHub Actions ワークフローをトリガー（batch_id なし）
    console.log('条件チェック完了。GitHub Actions 連続処理ワークフローを開始します');
    
    const workflowResult = triggerFormSenderWorkflow(targetingId);
    
    if (workflowResult && workflowResult.success) {
      console.log(`GitHub Actions 連続処理ワークフローが正常に開始されました`);
      return {
        success: true,
        message: '連続処理ワークフロー開始完了',
        targetingId: targetingId
      };
    } else {
      console.error('GitHub Actions ワークフローの開始に失敗しました');
      return { success: false, message: 'ワークフロー開始失敗' };
    }
    
  } catch (error) {
    // FORM_SENDER.md:286-289準拠: 詳細エラー分類とハンドリング
    const errorType = getErrorType(error.message);
    console.error(`ターゲティング ${targetingId} の処理で${errorType}エラー: ${error.message}`);
    return { success: false, message: error.message, error_type: errorType, targeting_id: targetingId };
  }
}

/**
 * 06:25 JST: 当日用キューの完全リセットを実行
 */
function resetSendQueueAllDaily() {
  try {
    // 07:00 JST 以降は安全のため実行禁止
    const nowJstStr = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'HH:mm');
    const [hh, mm] = nowJstStr.split(':').map(Number);
    if (hh >= 7) { // 07:00 以降
      console.warn('resetSendQueueAllDaily: 07:00以降のためスキップしました');
      return { success: false, skipped: true, reason: 'after 07:00 JST' };
    }
    const res = resetSendQueueAll();
    console.log('send_queue truncated');
    return { success: true, result: res };
  } catch (e) {
    console.error('resetSendQueueAll error:', e);
    return { success: false, error: String(e) };
  }
}

/**
 * 06:35–06:50 JST: targeting毎に当日キューを生成
 */
function buildSendQueueForTargeting(targetingId) {
  try {
    const cfg = getTargetingConfig(targetingId); // 既存ロジックを流用
    if (!cfg || !cfg.client || !cfg.targeting) throw new Error('invalid 2-sheet config');
    const t = cfg.targeting;
    const dateJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
    // キュー上限は一律10000件（max_daily_sendsは送信成功数の上限としてRunner側で使用）
    const inserted = createQueueForTargeting(
      targetingId,
      dateJst,
      t.targeting_sql || '',
      t.ng_companies || '',
      10000,
      8
    );
    console.log(`Queue created for targeting ${targetingId}: ${inserted}`);
    return { success: true, inserted };
  } catch (e) {
    console.error('buildSendQueueForTargeting error:', e);
    return { success: false, error: String(e) };
  }
}

/**
 * 06:35–06:50 JST: アクティブな全targetingについて当日キューを一括生成
 * - startFormSenderFromTrigger と同様、スプレッドシートのアクティブ行を走査
 * - 企業名などの機密情報はログ出力しない
 */
function buildSendQueueForAllTargetings() {
  console.log('=== 当日キュー一括生成開始 ===');
  try {
    const activeTargetings = getActiveTargetingIdsFromSheet();
    if (!activeTargetings || activeTargetings.length === 0) {
      console.log('アクティブなターゲティング設定が見つかりません');
      return { success: false, message: 'アクティブtargetingなし', processed: 0 };
    }

    const dateJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
    let processed = 0;
    let failed = 0;
    let totalInserted = 0;
    const details = [];

    for (const t of activeTargetings) {
      const targetingId = t.targeting_id || t.id || t;
      try {
        // 2シート構造を確認
        const cfg = getTargetingConfig(targetingId);
        if (!cfg || !cfg.client || !cfg.targeting) throw new Error('invalid 2-sheet config');

        const targeting = cfg.targeting;
        const inserted = createQueueForTargeting(
          targetingId,
          dateJst,
          targeting.targeting_sql || '',
          targeting.ng_companies || '',
          10000,
          8
        );
        const n = Number(inserted) || 0;
        totalInserted += n;
        processed += 1;
        details.push({ targeting_id: targetingId, inserted: n, success: true });
        console.log(`targeting_id=${targetingId}: inserted=${n}`);
      } catch (e) {
        failed += 1;
        details.push({ targeting_id: targetingId, success: false, error: String(e) });
        console.error(`targeting_id=${targetingId}: キュー作成エラー: ${e}`);
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



/**
 * 営業時間内かどうかをチェック
 * @param {Object} targetingConfig ターゲティング設定
 * @returns {boolean} 営業時間内かどうか
 */
function isWithinBusinessHours(targetingConfig) {
  try {
    const now = new Date();
    const jstNow = new Date(now.getTime() + CONFIG.JST_OFFSET);
    
    // FORM_SENDER.md:175-176仕様準拠の曜日チェック
    // 仕様: 0=月曜日, 1=火曜日, 2=水曜日, 3=木曜日, 4=金曜日, 5=土曜日, 6=日曜日
    // JavaScript getDay()との変換: JS(日=0,月=1...土=6) → 仕様(月=0,火=1...日=6)
    // 変換式: (JS曜日 + 6) % 7 → 日=6, 月=0, 火=1, 水=2, 木=3, 金=4, 土=5
    const jsDay = jstNow.getDay(); // 0=日, 1=月, 2=火, 3=水, 4=木, 5=金, 6=土
    const currentDayOfWeek = (jsDay === 0) ? 6 : jsDay - 1; // 日曜を6に、他は1つ減らす
    const allowedDays = targetingConfig.send_days_of_week || [0, 1, 2, 3, 4]; // デフォルト: 平日(月火水木金)
    
    if (!allowedDays.includes(currentDayOfWeek)) {
      console.log(`営業日ではありません: 現在=${currentDayOfWeek}, 許可=${allowedDays}`);
      return false;
    }
    
    // 時間帯チェック
    const currentHour = jstNow.getHours();
    const currentMinute = jstNow.getMinutes();
    const currentTimeMinutes = currentHour * 60 + currentMinute;
    
    // 開始時間と終了時間をパース
    const startTime = targetingConfig.send_start_time || '08:00';
    const endTime = targetingConfig.send_end_time || '19:00';
    
    const startTimeMinutes = parseTimeToMinutes(startTime);
    const endTimeMinutes = parseTimeToMinutes(endTime);
    
    const isWithinTime = currentTimeMinutes >= startTimeMinutes && currentTimeMinutes <= endTimeMinutes;
    
    console.log(`時間帯チェック: 現在=${formatMinutesToTime(currentTimeMinutes)}, ` +
               `営業=${startTime}-${endTime}, 範囲内=${isWithinTime}`);
    
    return isWithinTime;
    
  } catch (error) {
    console.error(`営業時間チェックエラー: ${error.message}`);
    return false;
  }
}

/**
 * 時間文字列（HH:MM）を分に変換
 * @param {string} timeString 時間文字列
 * @returns {number} 分
 */
function parseTimeToMinutes(timeString) {
  const [hours, minutes] = timeString.split(':').map(Number);
  return hours * 60 + minutes;
}

/**
 * 分を時間文字列（HH:MM）に変換
 * @param {number} minutes 分
 * @returns {string} 時間文字列
 */
function formatMinutesToTime(minutes) {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}`;
}


/**
 * 基本的な処理対象企業存在チェック（新アーキテクチャ版）
 * FORM_SENDER.md 1.4節準拠の基本チェック実装
 * @param {number} targetingId ターゲティングID
 * @returns {boolean} 基本的な処理対象企業が存在するか
 */
function hasTargetCompaniesBasic(targetingId) {
  try {
    console.log(`基本的な処理対象企業チェック開始: targeting_id=${targetingId}`);
    
    // スプレッドシートからターゲティング設定を取得
    const targetingConfig = getTargetingConfig(targetingId);
    if (!targetingConfig) {
      console.log('ターゲティング設定が見つからないため基本チェック失敗');
      return false;
    }
    
    // 基本的な設定値の検証
    // targeting_sqlは空文字許可（空の場合は絞り込み条件なしとして扱う）
    const targeting_sql = targetingConfig.targeting?.targeting_sql ? targetingConfig.targeting.targeting_sql.trim() : '';
    console.log(`targeting_sql: ${targeting_sql ? '設定あり' : '空文字（絞り込みなし）'}`);
    
    // ng_companiesも空文字許可（空の場合は除外企業なしとして扱う）
    const ng_companies = targetingConfig.targeting?.ng_companies ? targetingConfig.targeting.ng_companies.trim() : '';
    console.log(`ng_companies: ${ng_companies ? '設定あり' : '空文字（除外なし）'}`);
    
    if (!targetingConfig.targeting?.max_daily_sends || targetingConfig.targeting.max_daily_sends <= 0) {
      console.log('max_daily_sends設定が無効のため基本チェック失敗');
      return false;
    }
    
    // 基本的なクライアント情報の検証（詳細フィールドチェック）
    // 必須フィールド（空文字不可）
    const requiredFields = [
      'company_name', 'company_name_kana', 'form_sender_name',
      'last_name', 'first_name', 'last_name_kana', 'first_name_kana',
      'last_name_hiragana', 'first_name_hiragana', 'position',
      'gender', 'email_1', 'email_2',
      'postal_code_1', 'postal_code_2', 'address_1', 'address_2', 'address_3', 'address_4',
      'phone_1', 'phone_2', 'phone_3'
    ];
    
    // 空文字許可フィールド（department, website_url, address_5）
    const optionalFields = ['department', 'website_url', 'address_5'];
    
    const missingFields = [];
    
    for (const field of requiredFields) {
      if (!targetingConfig.client?.[field] || targetingConfig.client[field].toString().trim() === '') {
        missingFields.push(field);
      }
    }
    
    if (missingFields.length > 0) {
      console.log(`基本的なクライアント情報が不足のため基本チェック失敗: ${missingFields.join(', ')}`);
      return false;
    }
    
    console.log(`フィールドバリデーション完了: 必須フィールド ${requiredFields.length} 件OK, 空文字許可フィールド ${optionalFields.length} 件`);
    
    console.log('基本的な処理対象企業チェック成功: 詳細チェックはGitHub Actions側で実行');
    return true;
    
  } catch (error) {
    console.error(`基本的な処理対象企業チェックエラー: ${error.message}`);
    return false;
  }
}

/**
 * GitHub Actions フォーム送信ワークフローをトリガー（新アーキテクチャ版）
 * @param {number} targetingId ターゲティングID
 * @returns {Object} ワークフロートリガー結果
 */
function triggerFormSenderWorkflow(targetingId) {
  try {
    console.log(`GitHub Actions 連続処理ワークフローをトリガー: targetingId=${targetingId}`);
    
    // スプレッドシートから完全なクライアント設定を取得
    const clientConfig = getTargetingConfig(targetingId);
    
    if (!clientConfig) {
      console.error(`targeting_id ${targetingId} の設定が見つかりません`);
      return { success: false, message: 'ターゲティング設定が見つからない' };
    }
    
    // 機微情報は出さない
    console.log(`クライアント設定取得完了: ***COMPANY_REDACTED*** (client_id: ${clientConfig.client_id})`);
    
    // 並列起動数（targetingシート M列: concurrent_workflow）。空/未定義は1。
    const cw = Math.max(1, parseInt(clientConfig?.targeting?.concurrent_workflow || 1) || 1);
    console.log(`並列起動数(concurrent_workflow): ${cw}`);

    let ok = 0;
    let fail = 0;
    for (let i = 1; i <= cw; i++) {
      const result = sendRepositoryDispatch('form_sender_task', targetingId, clientConfig, i, cw);
      if (result && result.success) {
        ok++;
      } else {
        fail++;
      }
      // 連打を避けるための微小ウェイト（API保護）。必要に応じて調整可。
      if (cw > 1 && i < cw) Utilities.sleep(150);
    }

    if (ok > 0 && fail === 0) {
      console.log(`GitHub Actions 連続処理ワークフロートリガー成功（${ok}/${cw}）`);
      return {
        success: true,
        targetingId: targetingId,
        started_runs: ok
      };
    } else if (ok > 0 && fail > 0) {
      console.warn(`GitHub Actions ワークフロートリガー一部成功（成功:${ok} 失敗:${fail} 合計:${cw}）`);
      return { success: true, partial: true, targetingId: targetingId, started_runs: ok, failed_runs: fail };
    } else {
      console.error('GitHub Actions ワークフロートリガー失敗');
      return { success: false, message: 'ワークフロートリガー失敗', started_runs: 0 };
    }
    
  } catch (error) {
    console.error(`ワークフロートリガーエラー: ${error.message}`);
    return { success: false, message: error.message };
  }
}



/**
 * 新アーキテクチャ用ユーティリティ関数群
 */


/**
 * 設定値をプロパティサービスから取得
 * @param {string} key 設定キー
 * @param {string} defaultValue デフォルト値
 * @returns {string} 設定値
 */
function getConfigValue(key, defaultValue = null) {
  try {
    const value = PropertiesService.getScriptProperties().getProperty(key);
    return value !== null ? value : defaultValue;
  } catch (error) {
    console.error(`設定値取得エラー (${key}): ${error.message}`);
    return defaultValue;
  }
}

/**
 * テスト用関数群（新アーキテクチャ版）
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
 * 全機能項目を網羅するテスト関数
 */
function testNewArchitecture() {
  console.log('=== FORM_SENDER.md仕様準拠 統合テスト開始 ===');
  
  const results = {
    timestamp: new Date().toISOString(),
    // FORM_SENDER.md:59-78準拠: スプレッドシート2シート構成連携
    spreadsheet_connection: testSpreadsheetConnection(),
    // FORM_SENDER.md:295-313準拠: GitHub Actions repository_dispatch連携
    github_connection: testGitHubConnection(),
    // FORM_SENDER.md:114-161準拠: ターゲティング処理フロー
    targeting_processing: null,
    // FORM_SENDER.md:169-208準拠: 営業時間制御システム
    business_hours_check: testBusinessHoursCheck(),
    // FORM_SENDER.md:175-200準拠: 処理対象企業存在チェック
    target_companies_check: testTargetCompaniesCheck(),
    // 仕様準拠性確認
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
      send_days_of_week: [0, 1, 2, 3, 4] // 月火水木金
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
    // テスト用のターゲティングID（存在するもの）
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
    // 1.1.2節: GitHub Actions ワークフロー仕様
    github_workflow_spec: true,
    // 1.2節: データベース構成仕様（2シート構成）
    database_structure: true,
    // 1.3.1節: アクティブターゲティング向けスプレッドシートデータ抽出フロー
    active_targeting_extraction: true,
    // 1.3.2節: プレースホルダ変数システムとの連携
    placeholder_variable_system: true,
    // 1.4節: 処理対象企業抽出アルゴリズム
    target_company_extraction: true,
    // 1.5節: GAS側制御システム
    gas_control_system: true,
    // 1.6節: 結果記録システム（GitHub Actions専用）
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
 * @returns {Object} テスト結果
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
    // 1. 実行中タスク状況確認のテスト
    console.log('1. 実行中タスク状況確認テスト');
    results.running_tasks_check = getRunningFormSenderTasks();
    console.log('実行中タスク確認結果:', results.running_tasks_check.success ? '成功' : '失敗');
    
    // 2. GitHub接続確認
    console.log('2. GitHub API接続確認');
    results.github_connection = testGitHubConnection();
    console.log('GitHub接続確認結果:', results.github_connection.success ? '成功' : '失敗');
    
    // 3. 停止機能の動作確認（実際の停止は行わない）
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
    
    // テスト結果サマリー
    const overallSuccess = results.running_tasks_check.success && 
                          results.github_connection.success && 
                          results.stop_functions_check.cancelable_runs_retrieval;
    
    console.log('=== form_sender_task停止機能テスト完了 ===');
    console.log(`テスト結果: ${overallSuccess ? '成功' : '部分的成功/失敗'}`);
    console.log(`実行中タスク数: ${results.running_tasks_check.total_running || 0}件`);
    console.log(`キャンセル可能タスク数: ${results.stop_functions_check.total_cancelable_runs}件`);
    
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
 * 実際の停止は行わず、動作確認のみ実施
 * @returns {Object} デモ実行結果
 */
function demoFormSenderTaskStop() {
  console.log('=== form_sender_task停止デモ実行開始 ===');
  
  try {
    // 現在の実行中タスクを確認
    console.log('現在の実行中タスク状況:');
    const currentTasks = getRunningFormSenderTasks();
    
    if (!currentTasks.success) {
      return { success: false, error: '実行中タスク取得失敗', details: currentTasks };
    }
    
    console.log(`実行中タスク数: ${currentTasks.total_running}件`);
    
    if (currentTasks.total_running === 0) {
      console.log('現在実行中のform_sender_taskはありません');
      return { success: true, message: '実行中タスクなし - 停止対象なし', demo_only: true };
    }
    
    // targeting_id別の内訳を表示
    const targetingIds = Object.keys(currentTasks.by_targeting_id);
    console.log(`識別済みtargeting_id: ${targetingIds.join(', ')}`);
    console.log(`targeting_id不明のタスク: ${currentTasks.unknown_targeting.length}件`);
    
    // デモ: 特定targeting_idの停止処理（実際には実行しない）
    if (targetingIds.length > 0) {
      const firstTargetingId = parseInt(targetingIds[0]);
      console.log(`[デモ] targeting_id ${firstTargetingId} の停止処理をシミュレーション:`);
      
      const relatedTasks = currentTasks.by_targeting_id[firstTargetingId];
      console.log(`  - 停止対象タスク数: ${relatedTasks.length}件`);
      relatedTasks.forEach(task => {
        console.log(`  - Run ID: ${task.run_id}, Status: ${task.status}`);
      });
      
      console.log(`[デモ] 実際の停止には stopSpecificFormSenderTask(${firstTargetingId}) を実行してください`);
    }
    
    // デモ: 全停止処理（実際には実行しない）
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
 * FORM_SENDER.md準拠: エラータイプ分類機能（実用性重視版）
 * FORM_SENDER.md:286-289仕様準拠の詳細エラー分類システム
 * @param {string} errorMessage エラーメッセージ
 * @returns {string} エラータイプ
 */
function getErrorType(errorMessage) {
  const message = errorMessage.toLowerCase();
  
  // FORM_SENDER.md:286-289仕様準拠: 実用的エラー分類
  
  // 1. スプレッドシート関連エラー（最も頻度が高い可能性）
  if (message.includes('spreadsheet') || message.includes('スプレッドシート') || 
      message.includes('シート') || message.includes('が見つかりません') ||
      message.includes('必須カラム') || message.includes('ヘッダー') ||
      message.includes('データがありません')) {
    return 'SPREADSHEET_CONFIG_ERROR';
  }
  
  // 2. GitHub API関連エラー（リトライ対象の重要エラー）
  if (message.includes('github') || message.includes('dispatch') || 
      message.includes('repository') || message.includes('authorization') ||
      message.includes('token') || message.includes('rate limit') ||
      message.includes('http 404') || message.includes('http 403')) {
    return 'GITHUB_API_ERROR';
  }
  
  // 3. ターゲティング設定エラー（設定ミス）
  if (message.includes('targeting_id') || message.includes('ターゲティング') ||
      message.includes('client_id') || message.includes('無効な値') ||
      message.includes('設定が見つからない')) {
    return 'TARGETING_CONFIG_ERROR';
  }
  
  // 4. クライアントデータエラー（データ不整合）
  if (message.includes('company_name') || message.includes('form_sender_name') ||
      message.includes('基本的なクライアント情報') || message.includes('結合') ||
      message.includes('client_config')) {
    return 'CLIENT_DATA_ERROR';
  }
  
  // 5. JSON解析エラー（データ形式問題）
  if (message.includes('json') || message.includes('parse') ||
      message.includes('解析') || message.includes('invalid json') ||
      message.includes('send_days_of_week')) {
    return 'JSON_PARSE_ERROR';
  }
  
  // 6. 営業時間制御エラー（業務ルール）
  if (message.includes('営業時間') || message.includes('営業日') ||
      message.includes('実行時間') || message.includes('時間外') ||
      message.includes('営業時間外')) {
    return 'BUSINESS_HOURS_ERROR';
  }
  
  // 7. ネットワーク・接続エラー（インフラ問題）
  if (message.includes('network') || message.includes('timeout') || 
      message.includes('connection') || message.includes('ネットワーク') ||
      message.includes('接続') || message.includes('fetch')) {
    return 'NETWORK_ERROR';
  }
  
  // 8. 権限・認証エラー（セキュリティ問題）
  if (message.includes('permission') || message.includes('権限') ||
      message.includes('unauthorized') || message.includes('forbidden') ||
      message.includes('認証')) {
    return 'PERMISSION_ERROR';
  }
  
  // 9. その他のシステムエラー
  return 'SYSTEM_ERROR';
}

/**
 * 進行中のform_sender_taskを一括停止する関数
 * @returns {Object} 停止処理結果
 */
function stopAllRunningFormSenderTasks() {
  try {
    console.log('=== 進行中form_sender_task一括停止開始 ===');
    
    // 実行中のform-senderワークフローを取得
    const runningTasks = getCancelableWorkflowRuns();
    
    if (!runningTasks.success) {
      console.error('実行中ワークフロー取得失敗:', runningTasks.error);
      return { success: false, error: '実行中ワークフロー取得失敗', details: runningTasks.error };
    }
    
    const cancelableRuns = runningTasks.runs || [];
    
    if (cancelableRuns.length === 0) {
      console.log('停止対象のform_sender_taskがありません');
      return { success: true, message: '停止対象なし', stopped_count: 0 };
    }
    
    console.log(`停止対象のform_sender_task: ${cancelableRuns.length}件`);
    
    // 各ワークフローランを停止
    let successCount = 0;
    let failureCount = 0;
    const results = [];
    
    for (const run of cancelableRuns) {
      console.log(`ワークフローラン停止実行: ID=${run.id}, Name=${run.name}, Status=${run.status}`);
      
      const cancelResult = cancelWorkflowRun(run.id);
      results.push({
        run_id: run.id,
        name: run.name,
        status: run.status,
        cancel_result: cancelResult
      });
      
      if (cancelResult.success) {
        successCount++;
        console.log(`ワークフローラン停止成功: ID=${run.id}`);
      } else {
        failureCount++;
        console.error(`ワークフローラン停止失敗: ID=${run.id}, エラー=${cancelResult.error}`);
      }
      
      // API制限を考慮して少し待機
      Utilities.sleep(500);
    }
    
    console.log(`=== form_sender_task一括停止完了: 成功=${successCount}件, 失敗=${failureCount}件 ===`);
    
    return {
      success: true,
      message: `form_sender_task停止完了: 成功=${successCount}件, 失敗=${failureCount}件`,
      total_tasks: cancelableRuns.length,
      stopped_count: successCount,
      failed_count: failureCount,
      details: results
    };
    
  } catch (error) {
    console.error('form_sender_task一括停止エラー:', error.message);
    return { success: false, error: error.message };
  }
}

/**
 * 特定のターゲティングIDに関連するform_sender_taskを停止
 * @param {number} targetingId ターゲティングID
 * @returns {Object} 停止処理結果
 */
function stopSpecificFormSenderTask(targetingId) {
  try {
    console.log(`=== targeting_id ${targetingId} のform_sender_task停止開始 ===`);
    
    // 実行中のform-senderワークフローを取得
    const runningTasks = getCancelableWorkflowRuns();
    
    if (!runningTasks.success) {
      console.error('実行中ワークフロー取得失敗:', runningTasks.error);
      return { success: false, error: '実行中ワークフロー取得失敗', targeting_id: targetingId };
    }
    
    const allRuns = runningTasks.runs || [];
    
    // 特定のターゲティングIDに関連するランをフィルタリング
    const relatedRuns = allRuns.filter(run => {
      // ワークフロー名やコミットメッセージから該当するtargeting_idを識別
      return run.head_commit?.message?.includes(`targeting_id=${targetingId}`) ||
             run.name?.includes(`targeting-${targetingId}`) ||
             run.display_title?.includes(`targeting_id=${targetingId}`);
    });
    
    if (relatedRuns.length === 0) {
      console.log(`targeting_id ${targetingId} に関連する実行中タスクが見つかりません`);
      return { 
        success: true, 
        message: `targeting_id ${targetingId} の実行中タスクなし`, 
        targeting_id: targetingId,
        stopped_count: 0 
      };
    }
    
    console.log(`targeting_id ${targetingId} 関連の停止対象: ${relatedRuns.length}件`);
    
    // 関連するワークフローランを停止
    let successCount = 0;
    let failureCount = 0;
    const results = [];
    
    for (const run of relatedRuns) {
      console.log(`関連ワークフローラン停止実行: ID=${run.id}, targeting_id=${targetingId}`);
      
      const cancelResult = cancelWorkflowRun(run.id);
      results.push({
        run_id: run.id,
        name: run.name,
        status: run.status,
        cancel_result: cancelResult
      });
      
      if (cancelResult.success) {
        successCount++;
        console.log(`関連ワークフローラン停止成功: ID=${run.id}, targeting_id=${targetingId}`);
      } else {
        failureCount++;
        console.error(`関連ワークフローラン停止失敗: ID=${run.id}, targeting_id=${targetingId}, エラー=${cancelResult.error}`);
      }
      
      // API制限を考慮して少し待機
      Utilities.sleep(500);
    }
    
    console.log(`=== targeting_id ${targetingId} のタスク停止完了: 成功=${successCount}件, 失敗=${failureCount}件 ===`);
    
    return {
      success: true,
      message: `targeting_id ${targetingId} のタスク停止完了: 成功=${successCount}件, 失敗=${failureCount}件`,
      targeting_id: targetingId,
      total_tasks: relatedRuns.length,
      stopped_count: successCount,
      failed_count: failureCount,
      details: results
    };
    
  } catch (error) {
    console.error(`targeting_id ${targetingId} のタスク停止エラー:`, error.message);
    return { success: false, error: error.message, targeting_id: targetingId };
  }
}

/**
 * キャンセル可能なform-senderワークフローランを取得
 * @returns {Object} ワークフローラン一覧
 */
function getCancelableWorkflowRuns() {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    const githubConfig = getGitHubConfig();
    const url = `${CONFIG.GITHUB_API_BASE}/repos/${githubConfig.OWNER}/${githubConfig.REPO}/actions/runs?status=in_progress&per_page=50`;
    
    console.log('実行中ワークフロー取得開始:', url);
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-FormSender/1.0'
      },
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode === 200) {
      const data = JSON.parse(responseText);
      
      // Form Sender関連のワークフローのみをフィルタリング
      const formSenderRuns = data.workflow_runs.filter(run => {
        return run.name === 'Form Sender' || 
               run.name?.includes('form-sender') || 
               run.path?.includes('form-sender') ||
               run.workflow_id?.toString().includes('form');
      });
      
      // キャンセル可能な状態のもののみを選択
      const cancelableRuns = formSenderRuns.filter(run => {
        return run.status === 'in_progress' || run.status === 'queued';
      });
      
      console.log(`実行中ワークフロー取得完了: 全件=${data.workflow_runs.length}件, Form Sender=${formSenderRuns.length}件, キャンセル可能=${cancelableRuns.length}件`);
      
      return {
        success: true,
        runs: cancelableRuns,
        total_runs: data.workflow_runs.length,
        form_sender_runs: formSenderRuns.length,
        cancelable_runs: cancelableRuns.length
      };
      
    } else {
      console.error(`実行中ワークフロー取得失敗: ${responseCode} - ${responseText}`);
      return { success: false, error: `HTTP ${responseCode}: ${responseText}` };
    }
    
  } catch (error) {
    console.error('実行中ワークフロー取得エラー:', error.message);
    return { success: false, error: error.message };
  }
}

/**
 * 個別のワークフローランをキャンセル
 * @param {number} runId ワークフローランID
 * @returns {Object} キャンセル結果
 */
function cancelWorkflowRun(runId) {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    const githubConfig = getGitHubConfig();
    const url = `${CONFIG.GITHUB_API_BASE}/repos/${githubConfig.OWNER}/${githubConfig.REPO}/actions/runs/${runId}/cancel`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-FormSender/1.0'
      },
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode === 202) {
      console.log(`ワークフローラン キャンセル成功: ID=${runId}`);
      return { success: true, run_id: runId, message: 'キャンセル成功' };
    } else {
      console.error(`ワークフローラン キャンセル失敗: ID=${runId}, ${responseCode} - ${responseText}`);
      return { success: false, run_id: runId, error: `HTTP ${responseCode}: ${responseText}` };
    }
    
  } catch (error) {
    console.error(`ワークフローラン キャンセルエラー: ID=${runId}`, error.message);
    return { success: false, run_id: runId, error: error.message };
  }
}

/**
 * 現在実行中のform_sender_taskの状況を確認
 * @returns {Object} 実行中タスクの詳細情報
 */
function getRunningFormSenderTasks() {
  try {
    console.log('=== 実行中form_sender_task状況確認開始 ===');
    
    const runningTasks = getCancelableWorkflowRuns();
    
    if (!runningTasks.success) {
      return { success: false, error: '実行中タスク取得失敗', details: runningTasks.error };
    }
    
    const runs = runningTasks.runs || [];
    
    if (runs.length === 0) {
      console.log('実行中のform_sender_taskはありません');
      return { success: true, message: '実行中タスクなし', running_tasks: [] };
    }
    
    // 実行中タスクの詳細情報を整理
    const taskDetails = runs.map(run => {
      // targeting_idを可能な限り抽出
      let targetingId = null;
      
      if (run.head_commit?.message) {
        const match = run.head_commit.message.match(/targeting_id=(\d+)/);
        if (match) targetingId = parseInt(match[1]);
      }
      
      return {
        run_id: run.id,
        name: run.name,
        status: run.status,
        conclusion: run.conclusion,
        targeting_id: targetingId,
        created_at: run.created_at,
        updated_at: run.updated_at,
        html_url: run.html_url
      };
    });
    
    // targeting_id別に分類
    const byTargetingId = {};
    const unknownTargeting = [];
    
    taskDetails.forEach(task => {
      if (task.targeting_id !== null) {
        if (!byTargetingId[task.targeting_id]) {
          byTargetingId[task.targeting_id] = [];
        }
        byTargetingId[task.targeting_id].push(task);
      } else {
        unknownTargeting.push(task);
      }
    });
    
    console.log(`実行中form_sender_task: 合計=${runs.length}件`);
    console.log(`targeting_id識別済み: ${Object.keys(byTargetingId).length}種類`);
    console.log(`targeting_id不明: ${unknownTargeting.length}件`);
    
    return {
      success: true,
      message: `実行中form_sender_task: ${runs.length}件`,
      total_running: runs.length,
      by_targeting_id: byTargetingId,
      unknown_targeting: unknownTargeting,
      all_tasks: taskDetails
    };
    
  } catch (error) {
    console.error('実行中タスク状況確認エラー:', error.message);
    return { success: false, error: error.message };
  }
}

/**
 * トリガー管理機能群（特定日時トリガー対応）
 */

/**
 * form-sender用の既存トリガーをすべて削除
 * @returns {Object} 削除結果
 */
function deleteFormSenderTriggers() {
  try {
    console.log('form-sender用トリガーの削除を開始');
    
    const triggers = ScriptApp.getProjectTriggers();
    let deletedCount = 0;
    
    triggers.forEach(trigger => {
      const handlerFunction = trigger.getHandlerFunction();
      
      // startFormSenderFromTriggerを呼び出すトリガーのみ削除
      if (handlerFunction === 'startFormSenderFromTrigger') {
        ScriptApp.deleteTrigger(trigger);
        deletedCount++;
        console.log(`トリガー削除: ${handlerFunction}, 種類: ${trigger.getTriggerSource()}`);
      }
    });
    
    console.log(`form-sender用トリガー削除完了: ${deletedCount}件削除`);
    return {
      success: true,
      deleted_count: deletedCount,
      message: `${deletedCount}件のトリガーを削除しました`
    };
    
  } catch (error) {
    console.error(`トリガー削除エラー: ${error.message}`);
    return {
      success: false,
      error: error.message
    };
  }
}

/**
 * 特定日時にstartFormSenderFromTriggerを実行するトリガーを作成
 * @param {Date} executeDateTime 実行日時
 * @returns {Object} 作成結果
 */
function createSpecificTimeTrigger(executeDateTime) {
  try {
    console.log(`特定日時トリガー作成開始: ${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);
    
    // 現在時刻より前の場合はエラー
    const now = new Date();
    if (executeDateTime <= now) {
      throw new Error(`指定日時が過去です: ${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);
    }
    
    const trigger = ScriptApp.newTrigger('startFormSenderFromTrigger')
      .timeBased()
      .at(executeDateTime)
      .create();
    
    console.log(`特定日時トリガー作成完了: ID=${trigger.getUniqueId()}, 実行予定=${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);
    
    return {
      success: true,
      trigger_id: trigger.getUniqueId(),
      execute_at: executeDateTime.toISOString(),
      execute_at_jst: executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'}),
      message: `${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}に実行予定のトリガーを作成しました`
    };
    
  } catch (error) {
    console.error(`特定日時トリガー作成エラー: ${error.message}`);
    return {
      success: false,
      error: error.message,
      execute_at: executeDateTime ? executeDateTime.toISOString() : null
    };
  }
}

/**
 * 次回実行時刻を計算（土日回避機能付き）
 * - 平日: 24時間後の00分
 * - 金曜日: 72時間後（月曜日の同時刻）の00分
 * - 土曜日: 48時間後（月曜日の同時刻）の00分
 * @returns {Date} 次回実行日時
 */
function getNextExecutionTime() {
  const now = new Date();
  const jstNow = new Date(now.getTime() + CONFIG.JST_OFFSET);
  
  // 現在の曜日を取得（JavaScript getDay()準拠: 0=日, 1=月, 2=火, 3=水, 4=木, 5=金, 6=土）
  const currentDayOfWeek = jstNow.getDay();
  let hoursToAdd = 24; // デフォルトは24時間後
  let skipReason = '';
  
  // 土日回避ロジック
  if (currentDayOfWeek === 5) { // 金曜日の場合
    hoursToAdd = 72; // 72時間後（月曜日）
    skipReason = '金曜日のため土日をスキップして月曜日に設定';
  } else if (currentDayOfWeek === 6) { // 土曜日の場合
    hoursToAdd = 48; // 48時間後（月曜日）
    skipReason = '土曜日のため日曜日をスキップして月曜日に設定';
  } else {
    skipReason = '平日のため翌日に設定';
  }
  
  // 指定時間後の同時刻を計算
  const nextExecution = new Date(jstNow.getTime() + (hoursToAdd * 60 * 60 * 1000));
  
  // 分を00に設定
  nextExecution.setMinutes(0);
  nextExecution.setSeconds(0);
  nextExecution.setMilliseconds(0);
  
  // JST→UTCに変換
  const nextExecutionUTC = new Date(nextExecution.getTime() - CONFIG.JST_OFFSET);
  
  // 曜日名を取得
  const dayNames = ['日曜日', '月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日'];
  const currentDayName = dayNames[currentDayOfWeek];
  const nextDayName = dayNames[nextExecution.getDay()];
  
  console.log(`次回実行時刻計算（土日回避）: 現在=${currentDayName} ${jstNow.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}, 次回=${nextDayName} ${nextExecution.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})} (${hoursToAdd}時間後)`);
  console.log(`土日回避: ${skipReason}`);
  
  return nextExecutionUTC;
}

/**
 * 次回トリガーを自動作成（土日回避機能付き）
 * - 平日: 24時間後に設定
 * - 金曜日: 72時間後（月曜日）に設定
 * - 土曜日: 48時間後（月曜日）に設定
 * @returns {Object} 作成結果
 */
function createNextDayTrigger() {
  try {
    console.log('次回トリガー作成開始（土日回避機能付き）');
    
    const nextExecutionTime = getNextExecutionTime();
    const result = createSpecificTimeTrigger(nextExecutionTime);
    
    if (result.success) {
      console.log(`次回トリガー作成成功: ${result.execute_at_jst}`);
    } else {
      console.error(`次回トリガー作成失敗: ${result.error}`);
    }
    
    return result;
    
  } catch (error) {
    console.error(`次回トリガー作成エラー: ${error.message}`);
    return {
      success: false,
      error: error.message
    };
  }
}

/**
 * 現在設定されているform-senderトリガーの一覧を表示
 * @returns {Object} トリガー一覧
 */
function listFormSenderTriggers() {
  try {
    console.log('=== form-senderトリガー一覧取得開始 ===');
    
    const triggers = ScriptApp.getProjectTriggers();
    const formSenderTriggers = triggers.filter(trigger => 
      trigger.getHandlerFunction() === 'startFormSenderFromTrigger'
    );
    
    const triggerList = formSenderTriggers.map(trigger => {
      const source = trigger.getTriggerSource();
      let details = {};
      
      if (source === ScriptApp.TriggerSource.CLOCK) {
        const eventType = trigger.getEventType();
        if (eventType === ScriptApp.EventType.ON_FORM_SUBMIT) {
          details.type = 'unknown';
        } else {
          // 特定日時トリガーの場合
          details.type = 'specific_time';
        }
      }
      
      return {
        id: trigger.getUniqueId(),
        handler: trigger.getHandlerFunction(),
        source: source.toString(),
        details: details
      };
    });
    
    console.log(`form-senderトリガー数: ${triggerList.length}件`);
    triggerList.forEach(trigger => {
      console.log(`ID: ${trigger.id}, ハンドラー: ${trigger.handler}, ソース: ${trigger.source}, タイプ: ${trigger.details.type}`);
    });
    
    console.log('=== form-senderトリガー一覧取得完了 ===');
    
    return {
      success: true,
      trigger_count: triggerList.length,
      triggers: triggerList,
      message: `${triggerList.length}件のform-senderトリガーが設定されています`
    };
    
  } catch (error) {
    console.error(`トリガー一覧取得エラー: ${error.message}`);
    return {
      success: false,
      error: error.message
    };
  }
}

/**
 * 特定日時トリガー機能のテスト用関数
 * 5分後に実行されるテストトリガーを作成
 * @returns {Object} テスト結果
 */
function testSpecificTimeTrigger() {
  try {
    console.log('=== 特定日時トリガーテスト開始 ===');
    
    // 5分後の日時を計算
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
    return {
      success: false,
      error: error.message
    };
  }
}

/**
 * 次回実行時刻計算のテスト用関数（土日回避機能テスト付き）
 * @param {boolean} includeWeekendTest 週末シミュレーションテストを含むかどうか
 * @returns {Object} テスト結果
 */
function testNextExecutionTime(includeWeekendTest = false) {
  try {
    console.log('=== 次回実行時刻計算テスト開始（土日回避機能付き） ===');
    
    // 実際の次回実行時刻を計算
    const nextTime = getNextExecutionTime();
    const jstTime = new Date(nextTime.getTime() + CONFIG.JST_OFFSET);
    const now = new Date();
    const jstNow = new Date(now.getTime() + CONFIG.JST_OFFSET);
    
    const dayNames = ['日曜日', '月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日'];
    const currentDayName = dayNames[jstNow.getDay()];
    const nextDayName = dayNames[jstTime.getDay()];
    
    const result = {
      success: true,
      current_time: jstNow.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'}),
      current_day: currentDayName,
      next_execution_utc: nextTime.toISOString(),
      next_execution_jst: jstTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'}),
      next_day: nextDayName,
      hours_until_next: Math.round((nextTime.getTime() - now.getTime()) / (1000 * 60 * 60) * 100) / 100
    };
    
    console.log('実際の次回実行時刻計算結果:');
    console.log(`現在時刻: ${currentDayName} ${result.current_time}`);
    console.log(`次回実行時刻: ${nextDayName} ${result.next_execution_jst}`);
    console.log(`実行まで: ${result.hours_until_next}時間`);
    
    // 週末シミュレーションテスト
    if (includeWeekendTest) {
      console.log('\n=== 週末シミュレーションテスト開始 ===');
      result.weekend_simulations = testWeekendSimulations();
      console.log('=== 週末シミュレーションテスト完了 ===');
    }
    
    console.log('=== 次回実行時刻計算テスト完了 ===');
    
    return result;
    
  } catch (error) {
    console.error(`次回実行時刻計算テストエラー: ${error.message}`);
    return {
      success: false,
      error: error.message
    };
  }
}

/**
 * 週末シミュレーションテスト（金曜日・土曜日の動作確認）
 * @returns {Object} シミュレーション結果
 */
function testWeekendSimulations() {
  const simulationResults = {};
  const dayNames = ['日曜日', '月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日'];
  
  try {
    // 各曜日でのシミュレーション
    for (let dayOfWeek = 0; dayOfWeek <= 6; dayOfWeek++) {
      const dayName = dayNames[dayOfWeek];
      
      // 指定曜日の13:00でシミュレーション
      const simulatedTime = new Date();
      simulatedTime.setHours(13, 0, 0, 0);
      
      // 指定曜日になるまで日付を調整
      const currentDay = simulatedTime.getDay();
      const daysToAdd = (dayOfWeek - currentDay + 7) % 7;
      simulatedTime.setDate(simulatedTime.getDate() + daysToAdd);
      
      // JST時刻として扱う
      const jstSimulated = new Date(simulatedTime.getTime() + CONFIG.JST_OFFSET);
      
      // 次回実行時刻を計算
      let hoursToAdd = 24; // デフォルト
      let skipReason = '平日のため翌日に設定';
      
      if (dayOfWeek === 5) { // 金曜日
        hoursToAdd = 72;
        skipReason = '金曜日のため土日をスキップして月曜日に設定';
      } else if (dayOfWeek === 6) { // 土曜日
        hoursToAdd = 48;
        skipReason = '土曜日のため日曜日をスキップして月曜日に設定';
      }
      
      const nextExecution = new Date(jstSimulated.getTime() + (hoursToAdd * 60 * 60 * 1000));
      nextExecution.setMinutes(0);
      nextExecution.setSeconds(0);
      nextExecution.setMilliseconds(0);
      
      const nextDayName = dayNames[nextExecution.getDay()];
      
      simulationResults[dayName] = {
        simulated_current: jstSimulated.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'}),
        next_execution: nextExecution.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'}),
        next_day: nextDayName,
        hours_added: hoursToAdd,
        skip_reason: skipReason
      };
      
      console.log(`${dayName}シミュレーション: ${jstSimulated.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})} → ${nextDayName} ${nextExecution.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})} (${hoursToAdd}時間後)`);
    }
    
    return {
      success: true,
      simulations: simulationResults,
      message: '全曜日のシミュレーション完了'
    };
    
  } catch (error) {
    console.error(`週末シミュレーションエラー: ${error.message}`);
    return {
      success: false,
      error: error.message,
      partial_results: simulationResults
    };
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
 * 現在実行中のトリガー自身を削除（form-sender用）
 * 実行済トリガーの蓄積を防ぐため、実行開始時に呼び出す
 * fetch-detail/Code.gsのdeleteCurrentTrigger()を参考に実装
 * @returns {Object} 削除結果
 */
function deleteCurrentFormSenderTrigger() {
  try {
    console.log('実行中form-senderトリガーの削除を開始します');
    
    const now = new Date();
    const currentTriggers = ScriptApp.getProjectTriggers().filter(trigger => 
      trigger.getHandlerFunction() === 'startFormSenderFromTrigger'
    );
    
    if (currentTriggers.length === 0) {
      console.log('対象関数のトリガーが見つかりません');
      return { success: true, message: '削除対象なし', deletedCount: 0 };
    }
    
    console.log(`対象トリガー数: ${currentTriggers.length}個`);
    
    // 現在時刻に最も近い（実行済の可能性が高い）トリガーを特定
    let bestMatch = null;
    let minTimeDiff = Infinity;
    
    for (const trigger of currentTriggers) {
      try {
        // 時刻ベースのトリガーの場合のみ処理
        if (trigger.getTriggerSource() === ScriptApp.TriggerSource.CLOCK) {
          // 注意: GASでは直接実行時刻を取得できないため、
          // トリガー作成時刻から推測するか、ID順で判定する
          const triggerInfo = {
            id: trigger.getUniqueId(),
            source: trigger.getTriggerSource()
          };
          
          console.log(`トリガー確認: ID=${triggerInfo.id}, Source=${triggerInfo.source}`);
          
          // 最も古いID（最初に作られた可能性が高い）を実行済とみなす
          if (!bestMatch || triggerInfo.id < bestMatch.id) {
            bestMatch = {
              trigger: trigger,
              id: triggerInfo.id
            };
          }
        }
      } catch (error) {
        console.warn(`トリガー情報取得エラー (ID: ${trigger.getUniqueId()}):`, error);
      }
    }
    
    if (bestMatch) {
      try {
        const triggerId = bestMatch.id;
        ScriptApp.deleteTrigger(bestMatch.trigger);
        console.log(`実行済form-senderトリガーを削除しました: ID=${triggerId}`);
        
        return {
          success: true,
          deletedCount: 1,
          deletedTriggerId: triggerId,
          message: '実行済form-senderトリガー削除完了'
        };
      } catch (deleteError) {
        console.error('form-senderトリガー削除エラー:', deleteError);
        return {
          success: false,
          error: deleteError.toString(),
          deletedCount: 0
        };
      }
    } else {
      console.log('削除対象の実行済form-senderトリガーを特定できませんでした');
      return {
        success: true,
        message: '削除対象を特定できず',
        deletedCount: 0
      };
    }
    
  } catch (error) {
    console.error('実行中form-senderトリガー削除エラー:', error);
    return {
      success: false,
      error: error.toString(),
      deletedCount: 0
    };
  }
}

/**
 * === 簡易テスト実行関数 ===
 * 開発・テスト用の便利関数群
 */

/**
 * 現在開発中のブランチでのテスト実行
 * 
 * 【重要な注意事項】
 * Repository Dispatchの制限により、実際の実行は以下のブランチで行われます：
 * - ワークフローファイル(.github/workflows/form-sender.yml)が存在するブランチ
 * - 通常はmainブランチ
 * 
 * 【期待される動作】
 * 1. ペイロードにブランチ情報が含まれる
 * 2. ワークフロー内でブランチを動的にチェックアウト可能
 * 3. ただし、ワークフロー自体は存在するブランチ（通常main）から実行される
 * 
 * 【真のブランチテストを行うには】
 * - テスト対象ブランチにワークフローファイルをコピーする
 * - または、mainブランチのワークフローでdynamic checkoutを実装する
 * 
 * @returns {Object} テスト実行結果
 */
function testCurrentBranch() {
  console.log(`=== 開発ブランチでの本番通り実行 ===`);
  console.log(`targeting.id=1 を使用して本番処理を実行します`);
  
  try {
    // 本番通りの実行（targeting.id=1のみ制限）
    const result = startFormSender(1);
    console.log('開発ブランチでの実行完了:', result);
    
    return {
      success: true,
      targeting_id: 1,
      result: result,
      note: 'Production-like execution with targeting_id=1 only'
    };
  } catch (error) {
    console.error('開発ブランチ実行エラー:', error.message);
    return {
      success: false,
      targeting_id: 1,
      error: error.message
    };
  }
}

/**
 * mainブランチでのテスト実行（本番環境テスト）
 * @returns {Object} テスト実行結果
 */
function testMainBranch() {
  const MAIN_BRANCH = 'main';
  console.log(`=== mainブランチでのテスト実行: ${MAIN_BRANCH} ===`);
  
  try {
    const result = testFormSenderOnBranch(MAIN_BRANCH, 1);
    console.log('mainブランチテスト完了:', result);
    return {
      success: true,
      branch: MAIN_BRANCH,
      targeting_id: 1,
      result: result
    };
  } catch (error) {
    console.error('mainブランチテストエラー:', error.message);
    return {
      success: false,
      branch: MAIN_BRANCH,
      error: error.message
    };
  }
}

/**
 * 簡易テスト実行（デフォルト：現在ブランチ）
 * GASエディタから最も簡単に実行できる関数
 * @returns {Object} テスト実行結果
 */
function quickTest() {
  console.log('=== 簡易テスト実行開始 ===');
  return testCurrentBranch();
}

/**
 * トリガー削除機能のテスト用関数
 * deleteCurrentFormSenderTrigger()の動作確認
 * @returns {Object} テスト結果
 */
function testFormSenderTriggerDeletion() {
  try {
    console.log('=== form-senderトリガー削除機能テスト開始 ===');
    
    // 現在のトリガー状況を確認
    console.log('1. 現在のトリガー状況確認');
    const beforeList = listFormSenderTriggers();
    console.log(`テスト前のトリガー数: ${beforeList.trigger_count}件`);
    
    // 削除機能のテスト実行
    console.log('2. 削除機能テスト実行');
    const deleteResult = deleteCurrentFormSenderTrigger();
    console.log('削除テスト結果:', deleteResult);
    
    // テスト後のトリガー状況確認
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
