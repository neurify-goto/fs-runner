/**
 * 実行モードおよびBatchリソース関連のユーティリティ関数群
 * Code.gsの主要エントリポイントから呼び出される補助ロジックを集約
 */

function parseBooleanProperty_(value) {
  if (value === null || typeof value === 'undefined') {
    return false;
  }
  if (typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    return value !== 0;
  }
  var normalized = String(value).trim().toLowerCase();
  return normalized === 'true' || normalized === '1' || normalized === 'yes' || normalized === 'on';
}

function resolveExecutionModePriority_() {
  var props = PropertiesService.getScriptProperties();
  var preferBatch = parseBooleanProperty_(props.getProperty('USE_GCP_BATCH'));
  var preferServerless = parseBooleanProperty_(props.getProperty('USE_SERVERLESS_FORM_SENDER'));

  var priority = [];

  if (preferBatch) {
    priority.push('batch');
  }
  if (preferServerless) {
    priority.push('serverless');
  }

  if (priority.indexOf('batch') === -1) {
    priority.unshift('batch');
  }
  if (priority.indexOf('serverless') === -1) {
    priority.push('serverless');
  }

  priority.push('github');
  return priority;
}

function getScriptPropertyInt_(key, fallback) {
  var props = PropertiesService.getScriptProperties();
  var raw = props.getProperty(key);
  if (raw === null || typeof raw === 'undefined') {
    return fallback;
  }
  var parsed = parseInt(raw, 10);
  if (!isFinite(parsed)) {
    return fallback;
  }
  return parsed;
}

function getScriptPropertyString_(key, fallback) {
  var props = PropertiesService.getScriptProperties();
  var raw = props.getProperty(key);
  if (raw === null || typeof raw === 'undefined' || String(raw).trim() === '') {
    return fallback;
  }
  return String(raw);
}

function resolveTargetingExecutionMode_(targetingConfig) {
  var config = targetingConfig || {};
  var isGlobalResolution = !targetingConfig || Object.keys(config).length === 0;
  var scriptProps = PropertiesService.getScriptProperties();
  var globalBatchDefault = parseBooleanProperty_(scriptProps.getProperty('USE_GCP_BATCH'));
  var globalServerlessDefault = parseBooleanProperty_(scriptProps.getProperty('USE_SERVERLESS_FORM_SENDER'));

  function hasAnyKey(obj, keys) {
    if (!obj || typeof obj !== 'object') {
      return false;
    }
    for (var idx = 0; idx < keys.length; idx++) {
      if (Object.prototype.hasOwnProperty.call(obj, keys[idx])) {
        return true;
      }
    }
    return false;
  }

  function collectFrom(obj, keys) {
    if (!obj || typeof obj !== 'object') {
      return [];
    }
    return keys
      .map(function(key) { return obj[key]; })
      .filter(function(value) { return typeof value !== 'undefined'; });
  }

  function hasExplicitValue(values) {
    return values.some(function(value) {
      if (value === null || typeof value === 'undefined') {
        return false;
      }
      if (typeof value === 'string') {
        return value.trim() !== '';
      }
      return true;
    });
  }

  var batchCandidates = [];
  batchCandidates = batchCandidates.concat(collectFrom(config, ['useGcpBatch', 'use_gcp_batch']));
  batchCandidates = batchCandidates.concat(collectFrom(config.targeting || {}, ['useGcpBatch', 'use_gcp_batch']));
  batchCandidates = batchCandidates.concat(collectFrom(config.batch || {}, ['enabled']));
  var batchFieldsPresent = hasAnyKey(config, ['useGcpBatch', 'use_gcp_batch']) ||
    hasAnyKey(config.targeting || {}, ['useGcpBatch', 'use_gcp_batch']) ||
    hasAnyKey(config.batch || {}, ['enabled']);

  var batchHasExplicit = hasExplicitValue(batchCandidates);
  var batchExplicitEnabled = batchCandidates.some(parseBooleanProperty_);
  var batchEnabled = batchExplicitEnabled || (!batchHasExplicit && globalBatchDefault);

  var serverlessCandidates = [];
  serverlessCandidates = serverlessCandidates.concat(collectFrom(config, ['useServerless', 'use_serverless']));
  serverlessCandidates = serverlessCandidates.concat(collectFrom(config.targeting || {}, ['useServerless', 'use_serverless']));

  var serverlessHasExplicit = hasExplicitValue(serverlessCandidates);
  var serverlessExplicitEnabled = serverlessCandidates.some(parseBooleanProperty_);
  var serverlessEnabled = serverlessExplicitEnabled || (!serverlessHasExplicit && globalServerlessDefault);

  return {
    batchEnabled: !!batchEnabled,
    serverlessEnabled: !!serverlessEnabled,
    batchHasExplicit: batchHasExplicit,
    batchExplicitEnabled: !!batchExplicitEnabled,
    serverlessHasExplicit: serverlessHasExplicit,
    serverlessExplicitEnabled: !!serverlessExplicitEnabled
  };
}

function resolveExecutionMode_(targetingConfig) {
  var priority = resolveExecutionModePriority_();
  var modes = resolveTargetingExecutionMode_(targetingConfig || {});

  if (modes.batchExplicitEnabled) {
    return 'batch';
  }
  if (!modes.batchExplicitEnabled && modes.serverlessExplicitEnabled) {
    return 'serverless';
  }

  for (var i = 0; i < priority.length; i++) {
    var modeCandidate = priority[i];
    if (modeCandidate === 'batch' && modes.batchEnabled) {
      return 'batch';
    }
    if (modeCandidate === 'serverless' && modes.serverlessEnabled) {
      return 'serverless';
    }
    if (modeCandidate === 'github') {
      return 'github';
    }
  }
  return 'github';
}

function isDispatcherConfigured_() {
  var props = PropertiesService.getScriptProperties();
  var queue = props.getProperty('FORM_SENDER_TASKS_QUEUE');
  var dispatcherUrl = props.getProperty('FORM_SENDER_DISPATCHER_URL');
  var dispatcherBase = props.getProperty('FORM_SENDER_DISPATCHER_BASE_URL');
  var dispatcherServiceAccount = props.getProperty('FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT');

  function hasValue(value) {
    return value && typeof value === 'string' && value.trim() !== '';
  }

  if (!hasValue(queue)) {
    return false;
  }

  if (!hasValue(dispatcherServiceAccount)) {
    console.warn('FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT が未設定のため dispatcher を使用できません');
    return false;
  }

  if (hasValue(dispatcherUrl)) {
    return true;
  }

  return hasValue(dispatcherBase);
}

function readTargetingField_(targetingConfig, keys) {
  if (!Array.isArray(keys)) {
    keys = [keys];
  }
  var sources = [targetingConfig, targetingConfig ? targetingConfig.targeting : null, targetingConfig ? targetingConfig.batch : null];
  for (var i = 0; i < sources.length; i++) {
    var source = sources[i];
    if (!source || typeof source !== 'object') {
      continue;
    }
    for (var j = 0; j < keys.length; j++) {
      var key = keys[j];
      if (typeof source[key] !== 'undefined' && source[key] !== null) {
        return source[key];
      }
    }
  }
  return undefined;
}

function shouldUseServerlessFormSender_() {
  var mode = resolveExecutionMode_(null);
  return mode === 'serverless';
}

function shouldUseDispatcherFormSender_() {
  var mode = resolveExecutionMode_(null);
  return mode === 'batch' || mode === 'serverless';
}

function isTargetingServerlessEnabled_(targetingConfig) {
  if (!targetingConfig) {
    return false;
  }

  var candidates = [];
  candidates.push(targetingConfig.useServerless);
  candidates.push(targetingConfig.use_serverless);
  if (targetingConfig.targeting) {
    candidates.push(targetingConfig.targeting.useServerless);
    candidates.push(targetingConfig.targeting.use_serverless);
  }
  return candidates.some(parseBooleanProperty_);
}

function ceilTo256_(value) {
  return Math.ceil(value / 256) * 256;
}

function calculateBatchResourceProfile_(workers, vcpuPerWorker, memoryPerWorkerMb, bufferMb) {
  var workerCount = Math.max(1, parseInt(workers, 10) || 1);
  var resolvedVcpuPerWorker = parseInt(vcpuPerWorker, 10);
  if (!isFinite(resolvedVcpuPerWorker) || resolvedVcpuPerWorker < 1) {
    resolvedVcpuPerWorker = 1;
  }

  var resolvedMemoryPerWorker = parseInt(memoryPerWorkerMb, 10);
  if (!isFinite(resolvedMemoryPerWorker) || resolvedMemoryPerWorker < 1024) {
    resolvedMemoryPerWorker = 2048;
  }

  var resolvedBufferMb = parseInt(bufferMb, 10);
  if (!isFinite(resolvedBufferMb) || resolvedBufferMb < 0) {
    resolvedBufferMb = 2048;
  }

  var totalVcpu = workerCount * resolvedVcpuPerWorker;
  var totalMemoryMb = ceilTo256_((workerCount * resolvedMemoryPerWorker) + resolvedBufferMb);

  return {
    vcpu: totalVcpu,
    memoryMb: totalMemoryMb,
    perWorkerVcpu: resolvedVcpuPerWorker,
    perWorkerMemoryMb: resolvedMemoryPerWorker
  };
}

function parseCustomMachineProfile_(machineType) {
  if (typeof machineType !== 'string') {
    return null;
  }
  var match = machineType.match(/custom-(\d+)-(\d+)/i);
  if (!match) {
    return null;
  }
  var vcpu = parseInt(match[1], 10);
  var memoryMb = parseInt(match[2], 10);
  if (!isFinite(vcpu) || !isFinite(memoryMb)) {
    return null;
  }
  return {
    vcpu: vcpu,
    memoryMb: memoryMb
  };
}

function parseStandardMachineProfile_(machineType) {
  if (typeof machineType !== 'string') {
    return null;
  }
  var normalized = machineType.trim().toLowerCase();
  var match = normalized.match(/^(n2d|n2|e2)-(standard|highmem|highcpu)-(\d+)$/);
  if (!match) {
    return null;
  }
  var tier = match[2];
  var vcpu = parseInt(match[3], 10);
  if (!isFinite(vcpu) || vcpu < 1) {
    return null;
  }
  var memoryPerVcpuMb;
  if (tier === 'standard') {
    memoryPerVcpuMb = 4096;
  } else if (tier === 'highmem') {
    memoryPerVcpuMb = 8192;
  } else if (tier === 'highcpu') {
    memoryPerVcpuMb = 1024;
  } else {
    return null;
  }
  return {
    vcpu: vcpu,
    memoryMb: vcpu * memoryPerVcpuMb
  };
}

function buildBatchPayload_(targetingConfig, workers, parallelism, instanceCount) {
  var scriptProps = PropertiesService.getScriptProperties();
  var defaultMaxParallelism = getScriptPropertyInt_('FORM_SENDER_BATCH_MAX_PARALLELISM_DEFAULT', 100);
  var defaultPreferSpot = parseBooleanProperty_(scriptProps.getProperty('FORM_SENDER_BATCH_PREFER_SPOT_DEFAULT') || 'true');
  var defaultAllowOnDemand = parseBooleanProperty_(scriptProps.getProperty('FORM_SENDER_BATCH_ALLOW_ON_DEMAND_DEFAULT') || 'false');
  var machineTypeDefault = getScriptPropertyString_('FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT', null);
  var machineTypeOverride = getScriptPropertyString_('FORM_SENDER_BATCH_MACHINE_TYPE_OVERRIDE', null);
  var defaultTtlHours = getScriptPropertyInt_('FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH', 48);
  var defaultRefreshSeconds = getScriptPropertyInt_('FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH', 21600);
  var defaultMaxAttempts = getScriptPropertyInt_('FORM_SENDER_BATCH_MAX_ATTEMPTS_DEFAULT', 1);
  var defaultVcpuPerWorker = getScriptPropertyInt_('FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT', 1);
  if (!isFinite(defaultVcpuPerWorker) || defaultVcpuPerWorker < 1) {
    defaultVcpuPerWorker = 1;
  }
  var defaultMemoryPerWorkerMb = getScriptPropertyInt_('FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT', 2048);
  if (!isFinite(defaultMemoryPerWorkerMb) || defaultMemoryPerWorkerMb < 1024) {
    defaultMemoryPerWorkerMb = 2048;
  }
  var defaultBufferMb = getScriptPropertyInt_('FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT', 2048);
  if (!isFinite(defaultBufferMb) || defaultBufferMb < 0) {
    defaultBufferMb = 2048;
  }

  var maxParallelOverride = readTargetingField_(targetingConfig, ['batch_max_parallelism', 'batchMaxParallelism', 'max_parallelism']);
  var preferSpotOverride = readTargetingField_(targetingConfig, ['batch_prefer_spot', 'batchPreferSpot', 'prefer_spot']);
  var fallbackOverride = readTargetingField_(targetingConfig, ['batch_allow_on_demand_fallback', 'batchAllowOnDemandFallback', 'allow_on_demand_fallback']);
  var machineTypeField = readTargetingField_(targetingConfig, ['batch_machine_type', 'batchMachineType', 'machine_type']);
  var ttlOverride = readTargetingField_(targetingConfig, ['batch_signed_url_ttl_hours', 'batchSignedUrlTtlHours', 'signed_url_ttl_hours']);
  var batchSignedUrlRefreshSeconds = readTargetingField_(targetingConfig, ['batch_signed_url_refresh_threshold_seconds', 'batchSignedUrlRefreshThresholdSeconds', 'signed_url_refresh_threshold_seconds']);
  var vcpuPerWorkerOverride = readTargetingField_(targetingConfig, ['batch_vcpu_per_worker', 'batchVcpuPerWorker', 'vcpu_per_worker']);
  var memoryPerWorkerOverride = readTargetingField_(targetingConfig, ['batch_memory_per_worker_mb', 'batchMemoryPerWorkerMb', 'memory_per_worker_mb']);
  var batchMaxAttemptsField = readTargetingField_(targetingConfig, ['batch_max_attempts', 'batchMaxAttempts', 'max_attempts']);

  var maxParallelism = null;
  if (typeof maxParallelOverride !== 'undefined') {
    var parsedMax = parseInt(maxParallelOverride, 10);
    if (isFinite(parsedMax) && parsedMax > 0) {
      maxParallelism = parsedMax;
    }
  }
  if (!isFinite(maxParallelism) || maxParallelism === null) {
    maxParallelism = defaultMaxParallelism;
  }

  var preferSpot = defaultPreferSpot;
  if (typeof preferSpotOverride !== 'undefined') {
    preferSpot = parseBooleanProperty_(preferSpotOverride);
  }

  var allowOnDemand = defaultAllowOnDemand;
  if (typeof fallbackOverride !== 'undefined') {
    allowOnDemand = parseBooleanProperty_(fallbackOverride);
  }

  var resolvedVcpuPerWorker = defaultVcpuPerWorker;
  if (typeof vcpuPerWorkerOverride !== 'undefined' && vcpuPerWorkerOverride !== null) {
    var parsedVcpu = parseInt(vcpuPerWorkerOverride, 10);
    if (isFinite(parsedVcpu) && parsedVcpu >= 1) {
      resolvedVcpuPerWorker = parsedVcpu;
    }
  }

  var resolvedMemoryPerWorkerMb = defaultMemoryPerWorkerMb;
  if (typeof memoryPerWorkerOverride !== 'undefined' && memoryPerWorkerOverride !== null) {
    var parsedMemory = parseInt(memoryPerWorkerOverride, 10);
    if (isFinite(parsedMemory) && parsedMemory >= 1024) {
      resolvedMemoryPerWorkerMb = Math.max(parsedMemory, 2048);
    }
  }

  var resolvedMaxAttempts = defaultMaxAttempts;
  if (typeof batchMaxAttemptsField !== 'undefined' && batchMaxAttemptsField !== null) {
    var parsedAttempts = parseInt(batchMaxAttemptsField, 10);
    if (isFinite(parsedAttempts) && parsedAttempts >= 1) {
      resolvedMaxAttempts = parsedAttempts;
    }
  }
  if (!isFinite(resolvedMaxAttempts) || resolvedMaxAttempts < 1) {
    resolvedMaxAttempts = 1;
  }

  var resourceProfile = calculateBatchResourceProfile_(workers, resolvedVcpuPerWorker, resolvedMemoryPerWorkerMb, defaultBufferMb);
  var machineType = machineTypeOverride || machineTypeField;
  var normalizedMachineType = machineType ? String(machineType).trim().toLowerCase() : '';
  var usingImplicitStandard = false;
  if (!machineType || normalizedMachineType === '' || normalizedMachineType === 'e2-standard-2') {
    if (!machineType || normalizedMachineType === '') {
      if (typeof machineTypeDefault === 'string' && machineTypeDefault.trim() !== '') {
        machineType = machineTypeDefault;
      } else {
        machineType = 'e2-standard-2';
      }
    }
    usingImplicitStandard = true;
    normalizedMachineType = String(machineType).trim().toLowerCase();
  }

  var requestedMachineType = machineType;

  if (usingImplicitStandard) {
    var standardProfile = parseStandardMachineProfile_(normalizedMachineType);
    var standardVcpuLimit = standardProfile ? standardProfile.vcpu : 2;
    var standardMemoryLimitMb = standardProfile ? standardProfile.memoryMb : 8192;
    var requiresCustomShape = resourceProfile.vcpu > standardVcpuLimit || resourceProfile.memoryMb > standardMemoryLimitMb;
    if (requiresCustomShape) {
      machineType = 'n2d-custom-' + resourceProfile.vcpu + '-' + resourceProfile.memoryMb;
      requestedMachineType = machineType;
    }
  }

  var resolvedInstanceCount = null;
  if (typeof instanceCount !== 'undefined' && instanceCount !== null && String(instanceCount).trim() !== '') {
    var parsedInstanceCount = parseInt(instanceCount, 10);
    if (isFinite(parsedInstanceCount) && parsedInstanceCount >= 1) {
      resolvedInstanceCount = Math.min(parsedInstanceCount, 16);
    }
  }

  var parsedMachine = parseCustomMachineProfile_(machineType);
  var memoryWarning = false;
  var machineTypeFallbackApplied = false;
  var fallbackMemoryMb = resourceProfile.memoryMb;
  var fallbackVcpu = resourceProfile.vcpu;
  if (parsedMachine && parsedMachine.memoryMb < resourceProfile.memoryMb) {
    memoryWarning = true;
    fallbackMemoryMb = Math.max(resourceProfile.memoryMb, 10240);
    fallbackMemoryMb = ceilTo256_(fallbackMemoryMb);
    fallbackVcpu = Math.max(resourceProfile.vcpu, 4);
    console.warn(JSON.stringify({
      level: 'warning',
      event: 'batch_machine_type_insufficient',
      requested_machine_type: machineType,
      required_memory_mb: resourceProfile.memoryMb,
      fallback_machine_type: 'n2d-custom-' + fallbackVcpu + '-' + fallbackMemoryMb,
      workers: resourceProfile.vcpu / Math.max(1, resolvedVcpuPerWorker)
    }));
    machineType = 'n2d-custom-' + fallbackVcpu + '-' + fallbackMemoryMb;
    machineTypeFallbackApplied = true;
  }

  var ttlHours = defaultTtlHours;
  if (typeof ttlOverride !== 'undefined') {
    var parsedTtl = parseInt(ttlOverride, 10);
    if (isFinite(parsedTtl) && parsedTtl >= 1 && parsedTtl <= 168) {
      ttlHours = parsedTtl;
    }
  }

  var refreshSeconds = defaultRefreshSeconds;
  if (typeof batchSignedUrlRefreshSeconds !== 'undefined' && batchSignedUrlRefreshSeconds !== null) {
    var parsedRefresh = parseInt(batchSignedUrlRefreshSeconds, 10);
    if (isFinite(parsedRefresh) && parsedRefresh >= 60 && parsedRefresh <= 604800) {
      refreshSeconds = parsedRefresh;
    }
  }

  var resolvedMaxParallelism = Math.max(1, Math.min(maxParallelism, parallelism || maxParallelism));
  if (resolvedInstanceCount !== null) {
    var instanceTarget = Math.max(1, Math.min(maxParallelism, resolvedInstanceCount));
    if (resolvedMaxParallelism < instanceTarget) {
      resolvedMaxParallelism = instanceTarget;
    }
  }

  var payload = {
    enabled: true,
    max_parallelism: resolvedMaxParallelism,
    prefer_spot: !!preferSpot,
    allow_on_demand_fallback: !!allowOnDemand,
    machine_type: machineType,
    signed_url_ttl_hours: ttlHours,
    signed_url_refresh_threshold_seconds: refreshSeconds,
    vcpu_per_worker: resolvedVcpuPerWorker,
    memory_buffer_mb: defaultBufferMb,
    max_attempts: resolvedMaxAttempts,
    memory_per_worker_mb: resolvedMemoryPerWorkerMb
  };

  if (resolvedInstanceCount !== null) {
    payload.instance_count = resolvedInstanceCount;
  }

  if (memoryWarning) {
    payload.memory_warning = true;
    payload.computed_memory_mb = resourceProfile.memoryMb;
    if (machineTypeFallbackApplied) {
      if (requestedMachineType) {
        payload.requested_machine_type = requestedMachineType;
      }
      payload.machine_type_overridden = true;
      payload.fallback_machine_type = machineType;
      payload.fallback_memory_mb = fallbackMemoryMb;
      payload.fallback_vcpu = fallbackVcpu;
    }
  }

  return payload;
}

function resolveShardCount_() {
  var prop = PropertiesService.getScriptProperties().getProperty('FORM_SENDER_SHARD_COUNT');
  var value = parseInt(prop, 10);
  if (!isFinite(value) || value <= 0) {
    return 8;
  }
  return value;
}

function resolveParallelism_(concurrentWorkflow) {
  var prop = PropertiesService.getScriptProperties().getProperty('FORM_SENDER_PARALLELISM_OVERRIDE');
  var value = parseInt(prop, 10);
  if (!isFinite(value) || value <= 0) {
    return concurrentWorkflow;
  }
  return Math.min(concurrentWorkflow, value);
}

function resolveWorkersPerWorkflow_() {
  var prop = PropertiesService.getScriptProperties().getProperty('FORM_SENDER_WORKERS_OVERRIDE');
  var value = parseInt(prop, 10);
  if (!isFinite(value) || value <= 0) {
    return CONFIG.WORKERS_PER_WORKFLOW;
  }
  return Math.max(1, Math.min(4, value));
}

function resolveBatchInstanceCount_(targetingConfig) {
  var defaultCount = getScriptPropertyInt_('FORM_SENDER_BATCH_INSTANCE_COUNT_DEFAULT', 2);
  if (!isFinite(defaultCount) || defaultCount < 1) {
    defaultCount = 2;
  }

  var overrideCount = getScriptPropertyInt_('FORM_SENDER_BATCH_INSTANCE_COUNT_OVERRIDE', 0);
  if (isFinite(overrideCount) && overrideCount >= 1) {
    defaultCount = overrideCount;
  }

  var fieldValue = readTargetingField_(targetingConfig, ['batch_instance_count', 'batchInstanceCount', 'instance_count']);
  if (typeof fieldValue !== 'undefined' && fieldValue !== null && String(fieldValue).trim() !== '') {
    var parsed = parseInt(fieldValue, 10);
    if (isFinite(parsed) && parsed >= 1) {
      defaultCount = parsed;
    }
  }

  if (!isFinite(defaultCount) || defaultCount < 1) {
    defaultCount = 1;
  }
  if (defaultCount > 16) {
    defaultCount = 16;
  }
  return defaultCount;
}

function resolveBatchWorkersPerWorkflow_(targetingConfig, fallbackWorkers) {
  var base = Math.max(1, parseInt(fallbackWorkers, 10) || 1);
  var defaultFallback = base > 2 ? 2 : base;
  var defaultWorkers = getScriptPropertyInt_('FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_DEFAULT', defaultFallback);
  if (!isFinite(defaultWorkers) || defaultWorkers < 1) {
    defaultWorkers = defaultFallback;
  }

  var overrideWorkers = getScriptPropertyInt_('FORM_SENDER_BATCH_WORKERS_PER_WORKFLOW_OVERRIDE', 0);
  if (isFinite(overrideWorkers) && overrideWorkers >= 1) {
    defaultWorkers = overrideWorkers;
  }

  var fieldValue = readTargetingField_(targetingConfig, ['batch_workers_per_workflow', 'batchWorkersPerWorkflow']);
  if (typeof fieldValue !== 'undefined' && fieldValue !== null && String(fieldValue).trim() !== '') {
    var parsed = parseInt(fieldValue, 10);
    if (isFinite(parsed) && parsed >= 1) {
      defaultWorkers = parsed;
    }
  }

  if (!isFinite(defaultWorkers) || defaultWorkers < 1) {
    defaultWorkers = base;
  }
  if (defaultWorkers > 16) {
    defaultWorkers = 16;
  }
  return defaultWorkers;
}

function resolveSignedUrlTtlSeconds_(mode) {
  var defaultHours = mode === 'batch' ? 48 : 15;
  var propertyKey = mode === 'batch' ? 'FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH' : 'FORM_SENDER_SIGNED_URL_TTL_HOURS';
  var ttlHours = getScriptPropertyInt_(propertyKey, defaultHours);
  if (!isFinite(ttlHours) || ttlHours <= 0) {
    ttlHours = defaultHours;
  }
  return Math.max(1, ttlHours) * 3600;
}

function listDispatcherExecutionsSafe_(targetingId) {
  try {
    return CloudRunDispatcherClient.listRunningExecutions(targetingId);
  } catch (error) {
    console.warn('CloudRunDispatcherClient.listRunningExecutions failed: ' + error);
    return null;
  }
}

function allocateRunIndexBase_(targetingId, runTotal) {
  var lock = LockService.getScriptLock();
  lock.waitLock(5000);
  try {
    var props = PropertiesService.getScriptProperties();
    var baseKey = 'FORM_SENDER_RUN_INDEX_BASE__' + targetingId;
    var stateKey = baseKey + '__STATE';
    var todayJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');

    var delta = parseInt(runTotal, 10);
    if (!isFinite(delta) || delta <= 0) {
      delta = 1;
    }

    var current = 0;
    var rawState = props.getProperty(stateKey);
    if (rawState) {
      try {
        var state = JSON.parse(rawState);
        if (state && state.date === todayJst) {
          var stored = Number(state.counter);
          if (isFinite(stored) && stored >= 0) {
            current = stored;
          }
        }
      } catch (error) {
        console.warn('run_index_base state parse error. resetting counter:', error);
        current = 0;
      }
    } else {
      var legacyRaw = props.getProperty(baseKey);
      if (legacyRaw !== null && typeof legacyRaw !== 'undefined') {
        console.log('run_index_base legacy property detected for targeting ' + targetingId + '. resetting for daily counter migration.');
      }
      current = 0;
    }

    if (!isFinite(current) || current < 0) {
      current = 0;
    }

    var next = current + delta;
    props.setProperty(baseKey, String(next));
    props.setProperty(stateKey, JSON.stringify({
      date: todayJst,
      counter: next,
      updated_at: new Date().toISOString()
    }));

    return current;
  } finally {
    lock.releaseLock();
  }
}
