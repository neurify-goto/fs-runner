/**
 * フォーム送信制御システムのエントリーポイント集約ファイル。
 * ここではGASトリガーや手動実行から呼び出される中核関数のみを提供し、
 * 補助的なユーティリティは分割済みモジュールに配置している。
 */

const CONFIG = {
  MAX_RETRIES: 3,
  RETRY_DELAY: 2000,
  GITHUB_API_BASE: 'https://api.github.com',
  DEFAULT_TARGETING_ID: 1,
  QUEUE_TARGETING_IDS: [1],
  CHUNK_LIMIT_INITIAL: 2000,
  CHUNK_LIMIT_MIN: 250,
  CHUNK_ID_WINDOW_INITIAL: 50000,
  CHUNK_ID_WINDOW_MIN: 10000,
  CHUNK_TIME_BUDGET_MS: 240000,
  JST_OFFSET: 9 * 60 * 60 * 1000,
  WORKERS_PER_WORKFLOW: 4,
  HOLIDAY_CALENDAR_ID: 'ja.japanese#holiday@group.v.calendar.google.com',
  MILLISECONDS_PER_DAY: 24 * 60 * 60 * 1000,
  MAX_SKIP_DAYS: 10,
  DAILY_TRIGGER_HOUR: 9,
  DAILY_TRIGGER_MINUTE: 0,
  MAX_SESSION_DURATION_HOURS: 8,
  AUTO_STOP_MIN_DELAY_MS: 60 * 1000
};

var __HOLIDAY_CACHE = {};

/**
 * Fisher-Yates方式で配列をシャッフルして新しい順序を返す
 * @param {Array} items 元の配列
 * @returns {Array} シャッフル済みの新しい配列
 */
function shuffleArray_(items) {
  const cloned = Array.isArray(items) ? items.slice() : [];
  for (let i = cloned.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    const temp = cloned[i];
    cloned[i] = cloned[j];
    cloned[j] = temp;
  }
  return cloned;
}

function startFormSenderFromTrigger() {
  console.log('時間ベースのトリガーによりフォーム送信制御を開始します（新アーキテクチャ版）');

  try {
    const deleteResult = deleteCurrentFormSenderTrigger();
    if (deleteResult.success && deleteResult.deletedCount > 0) {
      console.log(`実行済トリガー削除成功: ${deleteResult.message}`);
    } else if (!deleteResult.success) {
      console.warn('実行済トリガー削除で問題発生:', deleteResult.error);
    }

    const jstToday = new Date();
    if (!isBusinessDayJst_(jstToday)) {
      console.log('本日は週末または祝日のため、処理をスキップします（通常トリガー）');
      const nextTriggerResult = createNextDayTrigger();
      if (nextTriggerResult.success) {
        console.log(`次回トリガー作成完了: ${nextTriggerResult.execute_at_jst}`);
      } else {
        console.error(`次回トリガー作成失敗: ${nextTriggerResult.error}`);
      }
      return { success: true, skipped: true, reason: 'non-business-day', next_trigger: nextTriggerResult };
    }

    const activeTargetings = getActiveTargetingIdsFromSheet();
    if (!activeTargetings || activeTargetings.length === 0) {
      console.log('アクティブなターゲティング設定が見つかりません');
      const nextTriggerResult = createNextDayTrigger();
      if (nextTriggerResult.success) {
        console.log(`次回トリガー作成完了（アクティブなし）: ${nextTriggerResult.execute_at_jst}`);
      } else {
        console.error(`次回トリガー作成失敗（アクティブなし）: ${nextTriggerResult.error}`);
      }
      return {
        success: false,
        message: 'アクティブなターゲティング設定なし',
        next_trigger: nextTriggerResult
      };
    }

    const randomizedTargetings = shuffleArray_(activeTargetings);
    console.log(`${randomizedTargetings.length} 件のアクティブなターゲティングをランダム順で処理開始`);
    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'form_sender_targeting_order_randomized',
        targeting_order: randomizedTargetings.map(t => t.targeting_id)
      }));
    } catch (_) {}
    registerFormSenderSessionStart('startFormSenderFromTrigger', { reset: true });
    let triggeredCount = 0;

    for (const targeting of randomizedTargetings) {
      try {
        const result = processTargeting(targeting.targeting_id, { useExtra: targeting.use_extra_table === true });
        if (result && result.success) {
          triggeredCount++;
        }
      } catch (error) {
        console.error(`ターゲティング ${targeting.targeting_id} 処理エラー: ${error.message}`);
      }
    }

    console.log(`処理完了: ${triggeredCount} 件トリガー実行`);

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
    console.error(`フォーム送信制御でエラー: ${error.message}`);
    console.log('エラー発生時も次回トリガーを設定します');

    const nextTriggerResult = createNextDayTrigger();
    if (nextTriggerResult.success) {
      console.log(`次回トリガー作成完了（エラー時）: ${nextTriggerResult.execute_at_jst}`);
    } else {
      console.error(`次回トリガー作成失敗（エラー時）: ${nextTriggerResult.error}`);
    }

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

function startFormSenderFromTriggerAt7() {
  console.log('時間トリガー(7:00)によりフォーム送信制御を開始します');

  try {
    const del7 = deleteTriggersByHandler('startFormSenderFromTriggerAt7');
    if (!del7.success) console.warn('7時トリガー削除で問題発生:', del7.error);

    const jstToday = new Date();
    if (!isBusinessDayJst_(jstToday)) {
      console.log('本日は週末または祝日のため、処理をスキップします（7:00）');
      const nextJst = getNextWeekdayExecutionTimeAt(7);
      const next = createSpecificTimeTriggerFor('startFormSenderFromTriggerAt7', nextJst);
      return { success: true, skipped: true, reason: 'non-business-day', next_trigger: next };
    }

    try {
      const stopRes = stopAllRunningFormSenderTasks();
      if (stopRes && stopRes.success) {
        const stopped = Number(stopRes.stopped_count || 0);
        console.log(`既存form-senderワークフローを停止: ${stopped}件`);
      } else {
        console.warn('既存form-senderワークフロー停止に失敗:', stopRes && stopRes.error);
      }
    } catch (e) {
      console.warn('既存form-senderワークフロー停止中に例外:', e);
    }

    console.log('ワークフロー停止後、1分間待機します...');
    Utilities.sleep(60000);
    console.log('待機完了。新規ワークフローを開始します。');

    const activeTargetings = getActiveTargetingIdsFromSheet();
    if (!activeTargetings || activeTargetings.length === 0) {
      console.log('アクティブなターゲティング設定が見つかりません');
    } else {
      registerFormSenderSessionStart('startFormSenderFromTriggerAt7', { reset: true });
      let triggered = 0;
      for (const t of activeTargetings) {
        try {
          const r = processTargeting(t.targeting_id, { useExtra: t.use_extra_table === true });
          if (r && r.success) triggered++;
        } catch (e) {
          console.error(`ターゲティング ${t.targeting_id} 処理エラー: ${e.message}`);
        }
      }
      console.log(`処理完了(7時): ${triggered} 件トリガー実行`);
    }

    const nextJst = getNextWeekdayExecutionTimeAt(7);
    const next = createSpecificTimeTriggerFor('startFormSenderFromTriggerAt7', nextJst);
    if (next.success) {
      console.log(`次回(7時)トリガー作成完了: ${next.execute_at_jst}`);
    } else {
      console.error(`次回(7時)トリガー作成失敗: ${next.error}`);
    }

    return {
      success: true,
      next_trigger: next
    };
  } catch (error) {
    console.error(`7時実行でエラー: ${error.message}`);
    const nextJst = getNextWeekdayExecutionTimeAt(7);
    const next = createSpecificTimeTriggerFor('startFormSenderFromTriggerAt7', nextJst);
    return { success: false, error: error.message, next_trigger: next };
  }
}

function startFormSenderFromTriggerAt13() {
  console.log('時間トリガー(13:00)によりフォーム送信制御を開始します');

  try {
    const del13 = deleteTriggersByHandler('startFormSenderFromTriggerAt13');
    if (!del13.success) console.warn('13時トリガー削除で問題発生:', del13.error);

    const jstToday = new Date();
    if (!isBusinessDayJst_(jstToday)) {
      console.log('本日は週末または祝日のため、処理をスキップします（13:00）');
      const nextJst = getNextWeekdayExecutionTimeAt(13);
      const next = createSpecificTimeTriggerFor('startFormSenderFromTriggerAt13', nextJst);
      return { success: true, skipped: true, reason: 'non-business-day', next_trigger: next };
    }

    try {
      const stopRes = stopAllRunningFormSenderTasks();
      if (stopRes && stopRes.success) {
        const stopped = Number(stopRes.stopped_count || 0);
        console.log(`既存form-senderワークフローを停止: ${stopped}件`);
      } else {
        console.warn('既存form-senderワークフロー停止に失敗:', stopRes && stopRes.error);
      }
    } catch (e) {
      console.warn('既存form-senderワークフロー停止中に例外:', e);
    }

    console.log('ワークフロー停止後、1分間待機します...');
    Utilities.sleep(60000);
    console.log('待機完了。新規ワークフローを開始します。');

    const activeTargetings = getActiveTargetingIdsFromSheet();
    if (!activeTargetings || activeTargetings.length === 0) {
      console.log('アクティブなターゲティング設定が見つかりません');
    } else {
      registerFormSenderSessionStart('startFormSenderFromTriggerAt13', { reset: true });
      let triggered = 0;
      for (const t of activeTargetings) {
        try {
          const r = processTargeting(t.targeting_id, { useExtra: t.use_extra_table === true });
          if (r && r.success) triggered++;
        } catch (e) {
          console.error(`ターゲティング ${t.targeting_id} 処理エラー: ${e.message}`);
        }
      }
      console.log(`処理完了(13時): ${triggered} 件トリガー実行`);
    }

    const nextJst = getNextWeekdayExecutionTimeAt(13);
    const next = createSpecificTimeTriggerFor('startFormSenderFromTriggerAt13', nextJst);
    if (next.success) {
      console.log(`次回(13時)トリガー作成完了: ${next.execute_at_jst}`);
    } else {
      console.error(`次回(13時)トリガー作成失敗: ${next.error}`);
    }

    return {
      success: true,
      next_trigger: next
    };
  } catch (error) {
    console.error(`13時実行でエラー: ${error.message}`);
    const nextJst = getNextWeekdayExecutionTimeAt(13);
    const next = createSpecificTimeTriggerFor('startFormSenderFromTriggerAt13', nextJst);
    return { success: false, error: error.message, next_trigger: next };
  }
}

function startFormSenderAll() {
  console.log('手動一括実行: 全アクティブtargetingの処理を開始');
  try {
    const activeTargetings = getActiveTargetingIdsFromSheet();
    if (!activeTargetings || activeTargetings.length === 0) {
      console.log('アクティブなターゲティング設定が見つかりません');
      return { success: false, message: 'アクティブtargetingなし', triggered: 0 };
    }
    registerFormSenderSessionStart('startFormSenderAll', { reset: true });
    let triggered = 0;
    const randomizedTargetings = shuffleArray_(activeTargetings);
    console.log(`${randomizedTargetings.length} 件のアクティブターゲティングをランダム順で処理`);
    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'form_sender_manual_targeting_order_randomized',
        targeting_order: randomizedTargetings.map(t => t.targeting_id)
      }));
    } catch (_) {}
    for (const t of randomizedTargetings) {
      try {
        const r = processTargeting(t.targeting_id, { useExtra: t.use_extra_table === true });
        if (r && r.success) triggered++;
      } catch (e) {
        console.error(`ターゲティング ${t.targeting_id} 処理エラー: ${e.message}`);
      }
    }
    console.log(`手動一括実行完了: ${triggered} 件トリガー実行`);
    return { success: true, triggered };
  } catch (error) {
    console.error('手動一括実行エラー:', error.message);
    return { success: false, error: error.message };
  }
}

function startFormSender(targetingId = null) {
  try {
    console.log(`フォーム送信処理を開始（新アーキテクチャ版）: targetingId=${targetingId}`);

    const finalTargetingId = targetingId || CONFIG.DEFAULT_TARGETING_ID;
    registerFormSenderSessionStart('startFormSender', { reset: true });
    const result = processTargeting(finalTargetingId);

    if (result && result.success) {
      console.log('フォーム送信連続処理ワークフローが正常に開始されました');
      return result;
    }
    console.log('フォーム送信ワークフローの開始条件を満たしていません');
    return result || { success: false, message: '開始条件未満足' };
  } catch (error) {
    console.error(`フォーム送信処理でエラー: ${error.message}`);
    return { success: false, message: error.message };
  }
}

function processTargeting(targetingId, options) {
  try {
    console.log(`ターゲティング ${targetingId} の処理を開始（新アーキテクチャ版）`);

    const targetingConfig = getTargetingConfig(targetingId);
    if (!targetingConfig) {
      console.log(`ターゲティング ${targetingId} が見つかりません`);
      return { success: false, message: 'ターゲティング設定が見つからない' };
    }

    console.log(`ターゲティング設定取得完了（2シート結合）: ***COMPANY_REDACTED*** (client_id: ${targetingConfig.client_id})`);
    console.log('営業時間制御は GitHub Actions 側で実施（GAS側で自動停止スケジュールを登録）');

    if (!hasTargetCompaniesBasic(targetingId)) {
      console.log('基本的な設定検証に失敗したためスキップします');
      return { success: false, message: '基本設定検証失敗' };
    }

    const resolvedOptions = options ? Object.assign({}, options) : {};
    if (typeof resolvedOptions.useExtra === 'undefined') {
      resolvedOptions.useExtra = !!targetingConfig.use_extra_table;
    } else {
      resolvedOptions.useExtra = !!resolvedOptions.useExtra;
    }
    if (!resolvedOptions.workflowTrigger) {
      resolvedOptions.workflowTrigger = 'automated';
    }

    try {
      registerAutoStopForTargeting(targetingConfig, {
        triggerName: resolvedOptions.workflowTrigger,
        source: 'processTargeting'
      });
    } catch (scheduleError) {
      console.warn(`ターゲティング ${targetingId} の自動停止スケジュール登録に失敗: ${scheduleError && scheduleError.message ? scheduleError.message : scheduleError}`);
    }

    var modeDetails = resolveTargetingExecutionMode_(targetingConfig || {});
    var modePriority = resolveExecutionModePriority_();
    var selectedMode = resolveExecutionMode_(targetingConfig);
    var dispatcherReady = isDispatcherConfigured_();

    console.log(JSON.stringify({
      level: 'debug',
      event: 'execution_mode_decision',
      targeting_id: targetingId,
      requested_mode: selectedMode,
      dispatcher_ready: dispatcherReady,
      mode_priority: modePriority,
      mode_flags: modeDetails
    }));

    if ((selectedMode === 'batch' || selectedMode === 'serverless') && !dispatcherReady) {
      console.warn('dispatcher 未設定のため GitHub Actions 経路へフォールバックします');
      selectedMode = 'github';
    }

    if (selectedMode === 'batch') {
      console.log('条件チェック完了。Cloud Tasks 経由で Cloud Batch ジョブを起動します');
      resolvedOptions.executionMode = 'batch';
      return triggerServerlessFormSenderWorkflow_(targetingId, targetingConfig, resolvedOptions);
    }

    if (selectedMode === 'serverless') {
      console.log('条件チェック完了。Cloud Tasks 経由で Cloud Run Job を起動します');
      resolvedOptions.executionMode = 'serverless';
      return triggerServerlessFormSenderWorkflow_(targetingId, targetingConfig, resolvedOptions);
    }

    console.log('条件チェック完了。GitHub Actions 連続処理ワークフローを開始します');
    const workflowResult = triggerFormSenderWorkflow(targetingId, resolvedOptions);
    if (workflowResult && workflowResult.success) {
      console.log('GitHub Actions 連続処理ワークフローが正常に開始されました');
      return {
        success: true,
        message: '連続処理ワークフロー開始完了',
        targetingId: targetingId
      };
    }
    console.error('GitHub Actions ワークフローの開始に失敗しました');
    return { success: false, message: 'ワークフロー開始失敗' };

  } catch (error) {
    const errorType = getErrorType(error.message);
    console.error(`ターゲティング ${targetingId} の処理で${errorType}エラー: ${error.message}`);
    return { success: false, message: error.message, error_type: errorType, targeting_id: targetingId };
  }
}
