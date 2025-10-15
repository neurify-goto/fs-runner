/**
 * form_sender_task の停止・状況確認など運用向け機能
 */

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
