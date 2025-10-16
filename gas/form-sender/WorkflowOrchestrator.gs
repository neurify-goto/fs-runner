/**
 * ワークフロー起動やターゲティング検証に関するロジックを集約
 */

function hasTargetCompaniesBasic(targetingId) {
  try {
    console.log(`基本的な処理対象企業チェック開始: targeting_id=${targetingId}`);

    const targetingConfig = getTargetingConfig(targetingId);
    if (!targetingConfig) {
      console.log('ターゲティング設定が見つからないため基本チェック失敗');
      return false;
    }

    const targeting_sql = targetingConfig.targeting?.targeting_sql ? targetingConfig.targeting.targeting_sql.trim() : '';
    console.log(`targeting_sql: ${targeting_sql ? '設定あり' : '空文字（絞り込みなし）'}`);

    const ng_companies = targetingConfig.targeting?.ng_companies ? targetingConfig.targeting.ng_companies.trim() : '';
    console.log(`ng_companies: ${ng_companies ? '設定あり' : '空文字（除外なし）'}`);

    if (!targetingConfig.targeting?.max_daily_sends || targetingConfig.targeting.max_daily_sends <= 0) {
      console.log('max_daily_sends設定が無効のため基本チェック失敗');
      return false;
    }

    const requiredFields = [
      'company_name', 'company_name_kana', 'form_sender_name',
      'last_name', 'first_name', 'last_name_kana', 'first_name_kana',
      'last_name_hiragana', 'first_name_hiragana', 'position',
      'gender', 'email_1', 'email_2',
      'postal_code_1', 'postal_code_2', 'address_1', 'address_2', 'address_3', 'address_4',
      'phone_1', 'phone_2', 'phone_3'
    ];

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

function triggerServerlessFormSenderWorkflow_(targetingId, targetingConfig, options) {
  var storage = StorageClient;
  var uploadInfo = null;
  try {
    var clientConfig = targetingConfig;
    var executionMode = (options && options.executionMode) || 'cloud_run';
    var dispatcherMode = executionMode === 'batch' ? 'batch' : 'cloud_run';
    var targeting = clientConfig.targeting || {};
    var testMode = !!(options && options.testMode === true);
    var useExtra = testMode ? false : !!((options && options.useExtra === true) || clientConfig.use_extra_table || (clientConfig.targeting && clientConfig.targeting.use_extra_table));
    var baseRunTotal = Math.max(1, parseInt(targeting.concurrent_workflow || 1, 10) || 1);
    var batchInstanceCount = null;
    if (dispatcherMode === 'batch') {
      batchInstanceCount = resolveBatchInstanceCount_(targetingConfig);
    }
    var runTotal = baseRunTotal;
    if (dispatcherMode === 'batch' && typeof batchInstanceCount === 'number' && isFinite(batchInstanceCount)) {
      runTotal = Math.max(baseRunTotal, batchInstanceCount);
    }
    var parallelism = resolveParallelism_(runTotal);
    var workers = resolveWorkersPerWorkflow_();
    if (dispatcherMode === 'batch') {
      workers = resolveBatchWorkersPerWorkflow_(targetingConfig, workers);
    }
    var shards = resolveShardCount_();
    var runIndexBase = allocateRunIndexBase_(targetingId, runTotal);

    var dispatcher = CloudRunDispatcherClient;
    try {
      dispatcher.validateConfig(clientConfig);
    } catch (validationError) {
      console.error('dispatcher validate-config でエラー:', validationError);
      var validationMessage = String(validationError && validationError.message ? validationError.message : validationError);
      return { success: false, message: validationMessage, error_type: 'validation_failed' };
    }

    var queueLogLabel = testMode ? 'send_queue_test' : (useExtra ? 'send_queue_extra' : 'send_queue');
    console.log(`${queueLogLabel} を再構築します`);
    var queueResult = buildSendQueueForTargeting(targetingId, { testMode: testMode, useExtra: useExtra });
    if (!queueResult || queueResult.success !== true) {
      console.error(`${queueLogLabel} 作成に失敗しました`);
      return { success: false, message: `${queueLogLabel} 作成失敗` };
    }

    uploadInfo = storage.uploadClientConfig(targetingId, clientConfig, { runId: Utilities.getUuid() });
    var signedUrlTtlSeconds = resolveSignedUrlTtlSeconds_(dispatcherMode);
    var signedUrl = storage.generateSignedUrl(uploadInfo.bucket, uploadInfo.objectName, signedUrlTtlSeconds);

    var jobExecutionId = Utilities.getUuid();
    var payload = {
      execution_id: jobExecutionId,
      targeting_id: targetingId,
      client_config_ref: signedUrl,
      client_config_object: uploadInfo.objectUri,
      tables: (function() {
        if (testMode) {
          return {
            use_extra_table: false,
            company_table: 'companies',
            send_queue_table: 'send_queue_test',
            submissions_table: 'submissions_test'
          };
        }
        return {
          use_extra_table: useExtra,
          company_table: useExtra ? 'companies_extra' : 'companies',
          send_queue_table: useExtra ? 'send_queue_extra' : 'send_queue'
        };
      })(),
      execution: {
        run_total: runTotal,
        parallelism: parallelism,
        run_index_base: runIndexBase,
        shards: shards,
        workers_per_workflow: workers
      },
      test_mode: testMode,
      branch: options && options.branch ? String(options.branch) : null,
      workflow_trigger: (options && options.workflowTrigger) || 'automated',
      metadata: {
        triggered_at_jst: Utilities.formatDate(new Date(), 'Asia/Tokyo', "yyyy-MM-dd'T'HH:mm:ssXXX"),
        gas_trigger: options && options.triggerName ? String(options.triggerName) : 'startFormSenderFromTrigger'
      }
    };

    payload.mode = dispatcherMode;

    if (dispatcherMode === 'batch') {
      payload.dispatcher_mode = 'batch';
      payload.batch = buildBatchPayload_(targetingConfig, workers, parallelism, batchInstanceCount);
      payload.execution.parallelism = Math.min(payload.execution.parallelism, payload.batch.max_parallelism);
      payload.cpu_class = 'gcp_spot';
    } else {
      payload.dispatcher_mode = 'cloud_run';
    }

    if (options && options.additionalMetadata && typeof options.additionalMetadata === 'object') {
      payload.metadata = Object.assign({}, payload.metadata, options.additionalMetadata);
    }

    var enqueueResult = dispatcher.enqueue(payload);

    return {
      success: true,
      message: 'dispatcher workflow 起動完了',
      execution_id: jobExecutionId,
      run_total: runTotal,
      payload: payload,
      dispatcher_response: enqueueResult
    };
  } catch (error) {
    console.error('dispatcher workflow 起動中にエラー', error);
    return { success: false, message: String(error) };
  } finally {
    if (uploadInfo && typeof enqueueResult === 'undefined') {
      try {
        storage.deleteObject(uploadInfo.bucket, uploadInfo.objectName);
      } catch (cleanupError) {
        console.warn('temporary object cleanup failed:', cleanupError);
      }
    }
  }
}

function triggerFormSenderWorkflow(targetingId, options) {
  try {
    const effectiveTargetingId = typeof targetingId === 'number' ? targetingId : CONFIG.DEFAULT_TARGETING_ID;
    if (typeof targetingId === 'undefined' || targetingId === null) {
      console.log(`targetingId が指定されていないため、既定値(${CONFIG.DEFAULT_TARGETING_ID})を使用します`);
    }
    const clientConfig = getTargetingConfig(effectiveTargetingId);
    if (!clientConfig) {
      return { success: false, message: 'targeting configuration not found', targetingId: effectiveTargetingId };
    }

    const modeDetails = resolveTargetingExecutionMode_(clientConfig || {});
    const modePriority = resolveExecutionModePriority_();
    const selectedMode = resolveExecutionMode_(clientConfig || {});
    const dispatcherReady = isDispatcherConfigured_();

    console.log(JSON.stringify({
      level: 'debug',
      event: 'workflow_entry_mode_check',
      targeting_id: effectiveTargetingId,
      selected_mode: selectedMode,
      dispatcher_ready: dispatcherReady,
      priority: modePriority,
      flags: modeDetails
    }));

    if ((selectedMode === 'batch' || selectedMode === 'serverless') && dispatcherReady) {
      const dispatcherOptions = Object.assign({}, options || {}, {
        executionMode: selectedMode,
        triggerName: options && options.triggerName ? options.triggerName : 'manual',
        additionalMetadata: options && options.additionalMetadata ? options.additionalMetadata : null
      });
      var dispatcherResult = triggerServerlessFormSenderWorkflow_(effectiveTargetingId, clientConfig, dispatcherOptions);
      return dispatcherResult;
    }

    if ((selectedMode === 'batch' || selectedMode === 'serverless') && !dispatcherReady) {
      console.warn('dispatcher が未設定のため GitHub Actions 経路にフォールバックします');
    }

    const useExtra = (function() {
      if (options && typeof options.useExtra !== 'undefined') {
        return !!options.useExtra;
      }
      return !!clientConfig.use_extra_table;
    })();

    const cw = Math.max(1, parseInt(clientConfig?.targeting?.concurrent_workflow || 1) || 1);
    console.log(`並列起動数(concurrent_workflow): ${cw}`);

    const testMode = !!(options && options.testMode === true);
    const queueLogLabel = testMode ? 'send_queue_test' : (useExtra ? 'send_queue_extra' : 'send_queue');
    console.log(`${queueLogLabel} を再構築します`);
    const queueResult = buildSendQueueForTargeting(effectiveTargetingId, { testMode, useExtra });
    if (!queueResult || queueResult.success !== true) {
      console.error(`${queueLogLabel} 作成に失敗しました`);
      return { success: false, message: `${queueLogLabel} 作成失敗` };
    }
    console.log(`GitHub Actions 連続処理ワークフローをトリガー: targetingId=${effectiveTargetingId}`);

    let ok = 0;
    let fail = 0;
    const dispatchOptions = Object.assign({}, options || {}, { useExtra });
    for (let i = 1; i <= cw; i++) {
      const result = sendRepositoryDispatch('form_sender_task', effectiveTargetingId, clientConfig, i, cw, dispatchOptions);
      if (result && result.success) {
        ok++;
      } else {
        fail++;
      }
      if (cw > 1 && i < cw) Utilities.sleep(150);
    }

    if (ok > 0 && fail === 0) {
      console.log(`GitHub Actions 連続処理ワークフロートリガー成功（${ok}/${cw}）`);
      return {
        success: true,
        targetingId: effectiveTargetingId,
        started_runs: ok
      };
    } else if (ok > 0 && fail > 0) {
      console.warn(`GitHub Actions ワークフロートリガー一部成功（成功:${ok} 失敗:${fail} 合計:${cw}）`);
      return { success: true, partial: true, targetingId: effectiveTargetingId, started_runs: ok, failed_runs: fail };
    } else {
      console.error('GitHub Actions ワークフロートリガー失敗');
      return { success: false, message: 'ワークフロートリガー失敗', started_runs: 0 };
    }

  } catch (error) {
    console.error(`ワークフロートリガーエラー: ${error.message}`);
    return { success: false, message: error.message };
  }
}

function triggerFormSenderWorkflowExtra(targetingId) {
  return triggerFormSenderWorkflow(targetingId, { useExtra: true });
}

function processTargetingExtra(targetingId) {
  try {
    console.log(`ターゲティング ${targetingId} の処理を開始（extraテーブル）`);
    const targetingConfig = getTargetingConfig(targetingId);
    if (!targetingConfig) {
      console.log(`ターゲティング ${targetingId} が見つかりません`);
      return { success: false, message: 'ターゲティング設定が見つからない' };
    }
    console.log('条件チェック完了。GitHub Actions 連続処理ワークフローを開始します（extra）');
    const workflowResult = triggerFormSenderWorkflow(targetingId, { useExtra: true });
    if (workflowResult && workflowResult.success) {
      console.log('GitHub Actions 連続処理ワークフロー（extra）が正常に開始されました');
      return { success: true, message: '連続処理ワークフロー開始完了(extra)', targetingId };
    } else {
      console.error('GitHub Actions ワークフローの開始に失敗しました（extra）');
      return { success: false, message: 'ワークフロー開始失敗(extra)' };
    }
  } catch (error) {
    const errorType = getErrorType(error.message);
    console.error(`ターゲティング ${targetingId} の処理で${errorType}エラー: ${error.message}`);
    return { success: false, message: error.message, error_type: errorType, targeting_id: targetingId };
  }
}

function getConfigValue(key, defaultValue = null) {
  try {
    const value = PropertiesService.getScriptProperties().getProperty(key);
    return value !== null ? value : defaultValue;
  } catch (error) {
    console.error(`設定値取得エラー (${key}): ${error.message}`);
    return defaultValue;
  }
}
