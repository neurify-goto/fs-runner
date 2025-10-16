/**
 * form_sender_task の停止・状況確認など運用向け機能
 */

const AUTO_STOP_STATE_KEY = 'FORM_SENDER_AUTO_STOP_SCHEDULE_V1';
const AUTO_STOP_TRIGGER_ID_KEY = 'FORM_SENDER_AUTO_STOP_TRIGGER_ID';
const AUTO_STOP_SESSION_INFO_KEY = 'FORM_SENDER_ACTIVE_SESSION_INFO';
const AUTO_STOP_TRIGGER_HANDLER = 'autoStopFormSenderFromSchedule';
const SCRIPT_PROP_DEFAULT_SESSION_HOURS = 'FORM_SENDER_MAX_SESSION_HOURS_DEFAULT';
const SCRIPT_PROP_DEFAULT_SEND_END_TIME = 'FORM_SENDER_DEFAULT_SEND_END_TIME';
const SESSION_HOURS_FALLBACK = 8;
const BUSINESS_END_TIME_FALLBACK = '18:00';

function getScriptPropertyNumberSafe_(key, fallback) {
  try {
    const props = PropertiesService.getScriptProperties();
    const raw = props.getProperty(key);
    if (raw === null || typeof raw === 'undefined') {
      return fallback;
    }
    const parsed = parseFloat(String(raw).trim());
    if (!isFinite(parsed)) {
      return fallback;
    }
    return parsed;
  } catch (e) {
    console.warn(`getScriptPropertyNumberSafe_(${key}) error: ${e && e.message ? e.message : e}`);
    return fallback;
  }
}

function getScriptPropertyStringSafe_(key, fallback) {
  try {
    const props = PropertiesService.getScriptProperties();
    const raw = props.getProperty(key);
    if (!raw || String(raw).trim() === '') {
      return fallback;
    }
    return String(raw).trim();
  } catch (e) {
    console.warn(`getScriptPropertyStringSafe_(${key}) error: ${e && e.message ? e.message : e}`);
    return fallback;
  }
}

function normalizeTimeStringSafe_(value) {
  if (!value || typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  const match = trimmed.match(/^(\d{1,2}):(\d{2})$/);
  if (!match) {
    return null;
  }
  const h = parseInt(match[1], 10);
  const m = parseInt(match[2], 10);
  if (!isFinite(h) || !isFinite(m) || h < 0 || h > 23 || m < 0 || m > 59) {
    return null;
  }
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function resolveSessionHours_(targetingConfig) {
  const configHours = (function() {
    try {
      const target = targetingConfig && targetingConfig.targeting ? targetingConfig.targeting : null;
      const raw = target ? target.session_max_hours : null;
      if (typeof raw === 'number' && isFinite(raw) && raw > 0) {
        return raw;
      }
      if (typeof raw === 'string') {
        const parsed = parseFloat(raw.trim());
        if (isFinite(parsed) && parsed > 0) {
          return parsed;
        }
      }
    } catch (e) {
      console.warn('resolveSessionHours_ config parsing error:', e.message);
    }
    return null;
  })();

  if (configHours && configHours > 0) {
    return configHours;
  }

  const propHours = getScriptPropertyNumberSafe_(SCRIPT_PROP_DEFAULT_SESSION_HOURS, null);
  if (propHours && propHours > 0) {
    return propHours;
  }

  if (typeof CONFIG !== 'undefined' && CONFIG && typeof CONFIG.MAX_SESSION_DURATION_HOURS === 'number' && CONFIG.MAX_SESSION_DURATION_HOURS > 0) {
    return CONFIG.MAX_SESSION_DURATION_HOURS;
  }

  return SESSION_HOURS_FALLBACK;
}

function resolveSendEndTimeForTargeting_(targetingConfig) {
  const fromConfig = (function() {
    try {
      const target = targetingConfig && targetingConfig.targeting ? targetingConfig.targeting : null;
      const raw = target ? target.send_end_time : null;
      if (typeof raw === 'string') {
        const normalized = normalizeTimeStringSafe_(raw);
        if (normalized) {
          return normalized;
        }
      }
    } catch (e) {
      console.warn('resolveSendEndTimeForTargeting_ config parsing error:', e.message);
    }
    return null;
  })();

  if (fromConfig) {
    return fromConfig;
  }

  const fromProps = normalizeTimeStringSafe_(getScriptPropertyStringSafe_(SCRIPT_PROP_DEFAULT_SEND_END_TIME, ''));
  if (fromProps) {
    return fromProps;
  }

  const fallbackNormalized = normalizeTimeStringSafe_(BUSINESS_END_TIME_FALLBACK);
  return fallbackNormalized || '18:00';
}

function stopAllRunningFormSenderTasks() {
  if (shouldUseDispatcherFormSender_()) {
    return stopAllRunningFormSenderTasksServerless_();
  }
  var dispatcherResponse = listDispatcherExecutionsSafe_(null);
  var hasDispatcherExecutions = dispatcherResponse && dispatcherResponse.executions && dispatcherResponse.executions.length > 0;
  if (hasDispatcherExecutions) {
    return stopAllRunningFormSenderTasksServerless_(dispatcherResponse);
  }
  return stopAllRunningFormSenderTasksLegacy_();
}

function stopAllRunningFormSenderTasksServerless_(prefetchedResponse) {
  try {
    console.log('=== 進行中form_sender_task一括停止開始 ===');
    const response = prefetchedResponse || CloudRunDispatcherClient.listRunningExecutions();
    const executions = (response && response.executions) || [];

    if (executions.length === 0) {
      console.log('停止対象のform_sender_taskがありません');
      return { success: true, message: '停止対象なし', stopped_count: 0 };
    }
    console.log(`停止対象のform_sender_task: ${executions.length}件`);

    let successCount = 0;
    let failureCount = 0;
    const results = [];

    for (const execution of executions) {
      var executionMode = execution.execution_mode || (execution.metadata && execution.metadata.execution_mode) || 'cloud_run';
      var cancelResult = null;
      try {
        cancelResult = CloudRunDispatcherClient.cancelExecution(execution.execution_id);
        successCount++;
        console.log(`Cloud dispatcher execution 停止成功: execution_id=${execution.execution_id}, mode=${executionMode}`);
      } catch (err) {
        failureCount++;
        cancelResult = { success: false, error: String(err) };
        console.error(`Cloud dispatcher execution 停止失敗: execution_id=${execution.execution_id}, mode=${executionMode}, エラー=${err}`);
      }

      results.push({
        execution_id: execution.execution_id,
        status: execution.status,
        execution_mode: executionMode,
        cancel_result: cancelResult
      });

      Utilities.sleep(200);
    }

    console.log(`=== form_sender_task一括停止完了: 成功=${successCount}件, 失敗=${failureCount}件 ===`);

    return {
      success: failureCount === 0,
      message: `form_sender_task停止完了: 成功=${successCount}件, 失敗=${failureCount}件`,
      total_tasks: executions.length,
      stopped_count: successCount,
      failed_count: failureCount,
      details: results
    };

  } catch (error) {
    console.error('form_sender_task一括停止エラー:', error.message);
    return { success: false, error: error.message };
  }
}

function stopAllRunningFormSenderTasksLegacy_() {
  try {
    console.log('=== 進行中form_sender_task停止開始（GitHub Actions API） ===');

    const cancelableRunsResult = getCancelableWorkflowRuns();
    if (!cancelableRunsResult.success) {
      console.error('実行中ワークフロー取得失敗:', cancelableRunsResult.error);
      return { success: false, error: cancelableRunsResult.error };
    }

    const cancelableRuns = cancelableRunsResult.runs || [];
    if (cancelableRuns.length === 0) {
      console.log('停止対象のform_sender_taskがありません');
      return { success: true, message: '停止対象なし', stopped_count: 0, details: [] };
    }

    console.log(`停止対象のform_sender_task: ${cancelableRuns.length}件`);

    let successCount = 0;
    let failureCount = 0;
    const results = [];

    for (const run of cancelableRuns) {
      console.log(`ワークフローラン停止実行: ID=${run.id}, name=${run.name}, status=${run.status}`);
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

      Utilities.sleep(500);
    }

    console.log(`=== form_sender_task一括停止完了: 成功=${successCount}件, 失敗=${failureCount}件 ===`);

    return {
      success: failureCount === 0,
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

function stopSpecificFormSenderTask(targetingId) {
  if (shouldUseDispatcherFormSender_()) {
    return stopSpecificFormSenderTaskServerless_(targetingId);
  }
  var dispatcherResponse = listDispatcherExecutionsSafe_(targetingId);
  var hasDispatcherExecutions = dispatcherResponse && dispatcherResponse.executions && dispatcherResponse.executions.length > 0;
  if (hasDispatcherExecutions) {
    return stopSpecificFormSenderTaskServerless_(targetingId, dispatcherResponse);
  }
  return stopSpecificFormSenderTaskLegacy_(targetingId);
}

function stopSpecificFormSenderTaskServerless_(targetingId, prefetchedResponse) {
  try {
    console.log(`=== targeting_id ${targetingId} のform_sender_task停止開始 ===`);

    const response = prefetchedResponse || CloudRunDispatcherClient.listRunningExecutions(targetingId);
    const executions = (response && response.executions) || [];

    if (executions.length === 0) {
      console.log(`targeting_id ${targetingId} に関連する実行中タスクが見つかりません`);
      return {
        success: true,
        message: `targeting_id ${targetingId} の実行中タスクなし`,
        targeting_id: targetingId,
        stopped_count: 0
      };
    }

    console.log(`targeting_id ${targetingId} 関連の停止対象: ${executions.length}件`);

    let successCount = 0;
    let failureCount = 0;
    const results = [];

    for (const execution of executions) {
      var executionMode = execution.execution_mode || (execution.metadata && execution.metadata.execution_mode) || 'cloud_run';
      var cancelResult = null;
      try {
        cancelResult = CloudRunDispatcherClient.cancelExecution(execution.execution_id);
        successCount++;
        console.log(`Cloud dispatcher execution 停止成功: execution_id=${execution.execution_id}, mode=${executionMode}`);
      } catch (err) {
        failureCount++;
        cancelResult = { success: false, error: String(err) };
        console.error(`Cloud dispatcher execution 停止失敗: execution_id=${execution.execution_id}, mode=${executionMode}, エラー=${err}`);
      }

      results.push({
        execution_id: execution.execution_id,
        status: execution.status,
        execution_mode: executionMode,
        cancel_result: cancelResult
      });

      Utilities.sleep(200);
    }

    console.log(`=== targeting_id ${targetingId} のタスク停止完了: 成功=${successCount}件, 失敗=${failureCount}件 ===`);

    return {
      success: failureCount === 0,
      message: `targeting_id ${targetingId} のタスク停止完了: 成功=${successCount}件, 失敗=${failureCount}件`,
      targeting_id: targetingId,
      total_tasks: executions.length,
      stopped_count: successCount,
      failed_count: failureCount,
      details: results
    };

  } catch (error) {
    console.error(`targeting_id ${targetingId} のタスク停止エラー:`, error.message);
    return { success: false, error: error.message, targeting_id: targetingId };
  }
}

function stopSpecificFormSenderTaskLegacy_(targetingId) {
  try {
    console.log(`=== targeting_id ${targetingId} のform_sender_task停止開始 ===`);

    const runningTasks = getCancelableWorkflowRuns();

    if (!runningTasks.success) {
      console.error('実行中ワークフロー取得失敗:', runningTasks.error);
      return { success: false, error: '実行中ワークフロー取得失敗', targeting_id: targetingId };
    }

    const allRuns = runningTasks.runs || [];

    const relatedRuns = allRuns.filter(run => {
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

      const formSenderRuns = data.workflow_runs.filter(run => {
        return run.name === 'Form Sender' ||
               run.name?.includes('form-sender') ||
               run.path?.includes('form-sender') ||
               run.workflow_id?.toString().includes('form');
      });

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

function cancelWorkflowRun(runId) {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }

    const githubConfig = getGitHubConfig();
    const url = `${CONFIG.GITHUB_API_BASE}/repos/${githubConfig.OWNER}/${githubConfig.REPO}/actions/runs/${runId}/cancel`;

    console.log(`ワークフローランキャンセル開始: ${url}`);

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
      console.log('ワークフローランキャンセル成功');
      return { success: true, run_id: runId };
    } else {
      console.error(`ワークフローランキャンセル失敗: ${responseCode} - ${responseText}`);
      return { success: false, error: `HTTP ${responseCode}: ${responseText}`, run_id: runId };
    }

  } catch (error) {
    console.error('ワークフローランキャンセルエラー:', error.message);
    return { success: false, error: error.message, run_id: runId };
  }
}

function getRunningFormSenderTasks() {
  if (shouldUseDispatcherFormSender_()) {
    return getRunningFormSenderTasksServerless_();
  }
  var dispatcherResponse = listDispatcherExecutionsSafe_(null);
  var hasDispatcherExecutions = dispatcherResponse && dispatcherResponse.executions && dispatcherResponse.executions.length > 0;
  if (hasDispatcherExecutions) {
    return getRunningFormSenderTasksServerless_(dispatcherResponse);
  }
  return getRunningFormSenderTasksLegacy_();
}

function getRunningFormSenderTasksServerless_(prefetchedResponse) {
  try {
    console.log('=== 実行中form_sender_task状況確認開始 ===');

    const response = prefetchedResponse || CloudRunDispatcherClient.listRunningExecutions();
    const executions = (response && response.executions) || [];

    if (executions.length === 0) {
      console.log('実行中のform_sender_taskはありません');
      return { success: true, message: '実行中タスクなし', running_tasks: [] };
    }

    const taskDetails = executions.map(function(exec) {
      var metadata = exec.metadata || {};
      var batchMeta = metadata.batch || {};
      var cloudRunMeta = metadata.cloud_run || {};
      var parallelism = typeof batchMeta.parallelism !== 'undefined' ? batchMeta.parallelism : (metadata.batch_parallelism || null);
      var arraySize = typeof batchMeta.array_size !== 'undefined' ? batchMeta.array_size : (metadata.batch_array_size || null);
      var attempts = typeof batchMeta.attempts !== 'undefined' ? batchMeta.attempts : (metadata.batch_attempts || null);
      var maxRetryCount = typeof batchMeta.max_retry_count !== 'undefined' ? batchMeta.max_retry_count : (metadata.batch_max_retry_count || null);
      var memoryMb = typeof batchMeta.memory_mb !== 'undefined' ? batchMeta.memory_mb : (metadata.batch_memory_mb || null);
      var memoryBufferMb = typeof batchMeta.memory_buffer_mb !== 'undefined' ? batchMeta.memory_buffer_mb : (metadata.batch_memory_buffer_mb || null);
      var preferSpot = typeof batchMeta.prefer_spot !== 'undefined' ? batchMeta.prefer_spot : (metadata.batch_prefer_spot || null);
      var allowOnDemand = typeof batchMeta.allow_on_demand !== 'undefined' ? batchMeta.allow_on_demand : (metadata.batch_allow_on_demand || null);
      var memoryWarning = batchMeta.memory_warning === true || metadata.batch_memory_warning === true;
      var computedMemoryMb = typeof batchMeta.computed_memory_mb !== 'undefined' ? batchMeta.computed_memory_mb : (metadata.batch_computed_memory_mb || null);
      var machineType = batchMeta.machine_type || metadata.batch_machine_type || null;
      var requestedMachineType = batchMeta.requested_machine_type || metadata.batch_requested_machine_type || null;
      var cpuMilli = typeof batchMeta.cpu_milli !== 'undefined' ? batchMeta.cpu_milli : (metadata.batch_cpu_milli || null);

      return {
        execution_id: exec.execution_id,
        run_id: exec.execution_id,
        targeting_id: exec.targeting_id,
        status: exec.status,
        run_index_base: exec.run_index_base,
        started_at: exec.started_at,
        ended_at: exec.ended_at || null,
        cloud_run_execution: cloudRunMeta.execution || metadata.cloud_run_execution || null,
        cloud_run_operation: cloudRunMeta.operation || metadata.cloud_run_operation || null,
        batch_job_name: batchMeta.job_name || metadata.batch_job_name || null,
        batch_task_group: batchMeta.task_group || metadata.batch_task_group || null,
        batch_parallelism: parallelism,
        batch_array_size: arraySize,
        batch_attempts: attempts,
        batch_max_retry_count: maxRetryCount,
        batch_memory_mb: memoryMb,
        batch_memory_buffer_mb: memoryBufferMb,
        batch_memory_warning: memoryWarning,
        batch_computed_memory_mb: memoryWarning ? computedMemoryMb : null,
        batch_prefer_spot: preferSpot,
        batch_allow_on_demand: allowOnDemand,
        batch_machine_type: machineType,
        batch_requested_machine_type: requestedMachineType,
        batch_cpu_milli: cpuMilli
      };
    });

    const byTargetingId = {};
    const unknownTargeting = [];

    taskDetails.forEach(function(task) {
      if (typeof task.targeting_id === 'number' && !isNaN(task.targeting_id)) {
        if (!byTargetingId[task.targeting_id]) {
          byTargetingId[task.targeting_id] = [];
        }
        byTargetingId[task.targeting_id].push(task);
      } else {
        unknownTargeting.push(task);
      }
    });

    console.log(`実行中form_sender_task: 合計=${executions.length}件`);
    console.log(`targeting_id識別済み: ${Object.keys(byTargetingId).length}種類`);
    console.log(`targeting_id不明: ${unknownTargeting.length}件`);

    return {
      success: true,
      message: `実行中form_sender_task: ${executions.length}件`,
      total_running: executions.length,
      by_targeting_id: byTargetingId,
      unknown_targeting: unknownTargeting,
      all_tasks: taskDetails
    };

  } catch (error) {
    console.error('実行中タスク状況確認エラー:', error.message);
    return { success: false, error: error.message };
  }
}

function registerFormSenderSessionStart(triggerName, options) {
  const opts = options || {};
  const now = new Date();

  if (opts.reset === true) {
    resetAutoStopSchedule_(now);
  }

  const props = PropertiesService.getScriptProperties();
  const sessionInfo = {
    trigger: triggerName || 'unknown',
    started_at: Utilities.formatDate(now, 'Asia/Tokyo', "yyyy-MM-dd'T'HH:mm:ssXXX")
  };
  try {
    props.setProperty(AUTO_STOP_SESSION_INFO_KEY, JSON.stringify(sessionInfo));
  } catch (e) {
    console.warn('セッション情報保存エラー:', e.message);
  }

  const durationHours = resolveSessionHours_(null);
  const stopDate = new Date(now.getTime() + durationHours * 60 * 60 * 1000);

  scheduleAutoStopEntries_([
    {
      targetingId: null,
      stopDate: stopDate,
      reason: 'max_runtime',
      metadata: {
        trigger: triggerName || 'unknown'
      }
    }
  ], now);
}

function registerAutoStopForTargeting(targetingConfig, options) {
  if (!targetingConfig || !targetingConfig.targeting) {
    return;
  }

  const targetingId = typeof targetingConfig.targeting_id === 'number'
    ? targetingConfig.targeting_id
    : targetingConfig.targeting.id;
  if (!targetingId || !isFinite(targetingId)) {
    return;
  }

  const now = new Date();
  const sessionHours = resolveSessionHours_(targetingConfig);
  const sessionStopDate = new Date(now.getTime() + sessionHours * 60 * 60 * 1000);

  const sendEndTime = resolveSendEndTimeForTargeting_(targetingConfig);
  const businessEnd = createJstDateForTime_(sendEndTime, now);
  const normalizedBusinessEnd = ensureMinDelay_(businessEnd, now);

  scheduleAutoStopEntries_([
    {
      targetingId: targetingId,
      stopDate: ensureMinDelay_(sessionStopDate, now),
      reason: 'max_runtime',
      metadata: {
        session_hours: sessionHours,
        trigger: options && options.triggerName ? String(options.triggerName) : null,
        source: options && options.source ? String(options.source) : null
      }
    },
    {
      targetingId: targetingId,
      stopDate: normalizedBusinessEnd,
      reason: 'business_end',
      metadata: {
        send_end_time: sendEndTime,
        trigger: options && options.triggerName ? String(options.triggerName) : null,
        source: options && options.source ? String(options.source) : null
      }
    }
  ], now);

  try {
    console.log(`ターゲティング${targetingId}向け自動停止（max_runtime=${sessionHours}h, business_end=${sendEndTime}）を登録しました`);
  } catch (logError) {
    // ログ出力失敗は無視
  }
}

function autoStopFormSenderFromSchedule() {
  const now = new Date();
  const schedule = loadAutoStopSchedule_();

  if (!schedule.entries || schedule.entries.length === 0) {
    refreshAutoStopTrigger_(schedule, now);
    return;
  }

  const minDelay = (typeof CONFIG.AUTO_STOP_MIN_DELAY_MS === 'number' && CONFIG.AUTO_STOP_MIN_DELAY_MS > 0)
    ? CONFIG.AUTO_STOP_MIN_DELAY_MS
    : 60 * 1000;
  const dueThreshold = now.getTime() + Math.floor(minDelay / 4);

  const remaining = [];
  let sessionCompleted = false;

  for (let i = 0; i < schedule.entries.length; i++) {
    const entry = schedule.entries[i];
    if (!entry || !entry.stop_at_epoch_ms) {
      continue;
    }

    if (entry.stop_at_epoch_ms <= dueThreshold) {
      try {
        executeAutoStopEntry_(entry, now);
        if (entry.targeting_id === null) {
          sessionCompleted = true;
        }
      } catch (e) {
        console.error('自動停止処理エラー:', e.message);
      }
    } else if (!sessionCompleted) {
      remaining.push(entry);
    }
  }

  schedule.entries = sessionCompleted ? [] : remaining.sort(function(a, b) {
    return a.stop_at_epoch_ms - b.stop_at_epoch_ms;
  });

  saveAutoStopSchedule_(schedule);
  refreshAutoStopTrigger_(schedule, now);
}

function loadAutoStopSchedule_() {
  const props = PropertiesService.getScriptProperties();
  try {
    const raw = props.getProperty(AUTO_STOP_STATE_KEY);
    if (!raw) {
      return { version: 1, entries: [] };
    }
    const parsed = JSON.parse(raw);
    const entries = Array.isArray(parsed && parsed.entries) ? parsed.entries : [];
    return {
      version: parsed && parsed.version ? parsed.version : 1,
      entries: entries
        .map(function(entry) {
          return {
            targeting_id: typeof entry.targeting_id === 'number' ? entry.targeting_id : null,
            reason: entry && entry.reason ? String(entry.reason) : 'unknown',
            stop_at_epoch_ms: Number(entry && entry.stop_at_epoch_ms) || 0,
            stop_at_iso: entry && entry.stop_at_iso ? String(entry.stop_at_iso) : null,
            metadata: entry && entry.metadata ? entry.metadata : {}
          };
        })
        .filter(function(entry) { return entry.stop_at_epoch_ms > 0; })
        .sort(function(a, b) { return a.stop_at_epoch_ms - b.stop_at_epoch_ms; })
    };
  } catch (e) {
    console.warn('自動停止スケジュールの読み込みに失敗しました:', e.message);
    return { version: 1, entries: [] };
  }
}

function saveAutoStopSchedule_(schedule) {
  const props = PropertiesService.getScriptProperties();
  try {
    props.setProperty(AUTO_STOP_STATE_KEY, JSON.stringify(schedule || { version: 1, entries: [] }));
  } catch (e) {
    console.error('自動停止スケジュール保存エラー:', e.message);
  }
}

function resetAutoStopSchedule_(now) {
  const props = PropertiesService.getScriptProperties();
  try {
    props.deleteProperty(AUTO_STOP_STATE_KEY);
    props.deleteProperty(AUTO_STOP_TRIGGER_ID_KEY);
  } catch (e) {
    console.warn('自動停止スケジュールのリセットでエラー:', e.message);
  }

  try {
    if (typeof deleteTriggersByHandler === 'function') {
      deleteTriggersByHandler(AUTO_STOP_TRIGGER_HANDLER);
    } else {
      const triggers = ScriptApp.getProjectTriggers();
      triggers.forEach(function(tr) {
        if (tr.getHandlerFunction() === AUTO_STOP_TRIGGER_HANDLER) {
          ScriptApp.deleteTrigger(tr);
        }
      });
    }
  } catch (triggerError) {
    console.warn('自動停止用トリガー削除に失敗:', triggerError.message);
  }

  saveAutoStopSchedule_({ version: 1, entries: [] });
}

function scheduleAutoStopEntries_(entries, now) {
  if (!entries || entries.length === 0) {
    return;
  }

  const baseline = now instanceof Date ? now : new Date();
  const minDelay = (typeof CONFIG.AUTO_STOP_MIN_DELAY_MS === 'number' && CONFIG.AUTO_STOP_MIN_DELAY_MS > 0)
    ? CONFIG.AUTO_STOP_MIN_DELAY_MS
    : 60 * 1000;
  const schedule = loadAutoStopSchedule_();
  const retained = [];

  for (let i = 0; i < schedule.entries.length; i++) {
    const entry = schedule.entries[i];
    if (entry.stop_at_epoch_ms && entry.stop_at_epoch_ms >= baseline.getTime() - minDelay) {
      retained.push(entry);
    }
  }

  entries.forEach(function(entry) {
    const normalized = normalizeAutoStopEntry_(entry, baseline);
    if (!normalized) {
      return;
    }

    for (let i = retained.length - 1; i >= 0; i--) {
      const existing = retained[i];
      if (existing.targeting_id === normalized.targeting_id && existing.reason === normalized.reason) {
        retained.splice(i, 1);
      }
    }

    retained.push(normalized);
  });

  retained.sort(function(a, b) { return a.stop_at_epoch_ms - b.stop_at_epoch_ms; });

  const nextSchedule = { version: 1, entries: retained };
  saveAutoStopSchedule_(nextSchedule);
  refreshAutoStopTrigger_(nextSchedule, baseline);
}

function refreshAutoStopTrigger_(schedule, now) {
  const baseline = now instanceof Date ? now : new Date();
  try {
    if (typeof deleteTriggersByHandler === 'function') {
      deleteTriggersByHandler(AUTO_STOP_TRIGGER_HANDLER);
    } else {
      const triggers = ScriptApp.getProjectTriggers();
      triggers.forEach(function(tr) {
        if (tr.getHandlerFunction() === AUTO_STOP_TRIGGER_HANDLER) {
          ScriptApp.deleteTrigger(tr);
        }
      });
    }
  } catch (e) {
    console.warn('自動停止トリガー再設定時の削除に失敗:', e.message);
  }

  const props = PropertiesService.getScriptProperties();

  if (!schedule.entries || schedule.entries.length === 0) {
    try {
      props.deleteProperty(AUTO_STOP_TRIGGER_ID_KEY);
    } catch (e) {
      // ignore
    }
    return;
  }

  const nextEntry = schedule.entries[0];
  let nextDate = new Date(nextEntry.stop_at_epoch_ms);
  nextDate = ensureMinDelay_(nextDate, baseline);

  try {
    const trigger = ScriptApp.newTrigger(AUTO_STOP_TRIGGER_HANDLER)
      .timeBased()
      .at(nextDate)
      .create();
    props.setProperty(AUTO_STOP_TRIGGER_ID_KEY, trigger.getUniqueId());
    console.log(`auto-stopトリガーを設定しました: targeting=${nextEntry.targeting_id === null ? 'ALL' : nextEntry.targeting_id}, 実行予定=${nextDate.toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })}`);
  } catch (e) {
    console.error('auto-stopトリガー設定エラー:', e.message);
  }
}

function normalizeAutoStopEntry_(entry, now) {
  if (!entry) {
    return null;
  }

  const baseline = now instanceof Date ? now : new Date();

  const stopDate = ensureMinDelay_(entry.stopDate, baseline);
  if (!(stopDate instanceof Date) || isNaN(stopDate.getTime())) {
    return null;
  }

  const targetingId = (typeof entry.targetingId === 'number' && isFinite(entry.targetingId)) ? entry.targetingId : null;
  const reason = entry.reason ? String(entry.reason) : (targetingId === null ? 'max_runtime' : 'business_end');

  return {
    targeting_id: targetingId,
    reason: reason,
    stop_at_epoch_ms: stopDate.getTime(),
    stop_at_iso: Utilities.formatDate(stopDate, 'Asia/Tokyo', "yyyy-MM-dd'T'HH:mm:ssXXX"),
    metadata: entry.metadata || {}
  };
}

function createJstDateForTime_(timeString, baseDate) {
  if (!timeString || typeof timeString !== 'string') {
    return null;
  }

  const trimmed = timeString.trim();
  if (!/^\d{1,2}:\d{2}$/.test(trimmed)) {
    return null;
  }

  const parts = trimmed.split(':');
  const hours = parseInt(parts[0], 10);
  const minutes = parseInt(parts[1], 10);
  if (!isFinite(hours) || !isFinite(minutes)) {
    return null;
  }

  const base = baseDate instanceof Date ? baseDate : new Date();
  const dateStr = Utilities.formatDate(base, 'Asia/Tokyo', 'yyyy-MM-dd');
  const iso = Utilities.formatString('%sT%02d:%02d:00+09:00', dateStr, hours, minutes);
  const result = new Date(iso);
  if (isNaN(result.getTime())) {
    return null;
  }
  return result;
}

function ensureMinDelay_(dateObj, now) {
  const baseline = now instanceof Date ? now : new Date();
  const minDelay = (typeof CONFIG.AUTO_STOP_MIN_DELAY_MS === 'number' && CONFIG.AUTO_STOP_MIN_DELAY_MS > 0)
    ? CONFIG.AUTO_STOP_MIN_DELAY_MS
    : 60 * 1000;

  let targetDate = dateObj instanceof Date ? new Date(dateObj) : null;
  if (!targetDate || isNaN(targetDate.getTime())) {
    targetDate = new Date(baseline.getTime() + minDelay);
  }

  if (targetDate.getTime() <= baseline.getTime() + minDelay) {
    return new Date(baseline.getTime() + minDelay);
  }
  return targetDate;
}

function executeAutoStopEntry_(entry, now) {
  const timestamp = Utilities.formatDate(now instanceof Date ? now : new Date(), 'Asia/Tokyo', "yyyy-MM-dd'T'HH:mm:ssXXX");
  if (!entry) {
    return;
  }

  if (entry.targeting_id === null) {
    console.log(`[auto-stop] セッション最大稼働時間に達したため全タスク停止 (${timestamp})`);
    return stopAllRunningFormSenderTasks();
  }

  console.log(`[auto-stop] targeting_id ${entry.targeting_id} の営業終了により停止 (${timestamp})`);
  return stopSpecificFormSenderTask(entry.targeting_id);
}

function getRunningFormSenderTasksLegacy_() {
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

    const taskDetails = runs.map(run => {
      let targetingId = null;

      if (run.head_commit?.message) {
        const match = run.head_commit.message.match(/targeting_id=(\d+)/);
        if (match) targetingId = parseInt(match[1], 10);
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
      running_tasks: taskDetails
    };

  } catch (error) {
    console.error('実行中タスク状況確認エラー:', error.message);
    return { success: false, error: error.message };
  }
}
