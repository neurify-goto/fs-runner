/**
 * Cloud Tasks を利用して Cloud Run dispatcher を呼び出すクライアント
 */
var CloudRunDispatcherClient = (function() {
  function resolveServiceAccount_() {
    if (typeof ServiceAccountClient !== 'undefined' && ServiceAccountClient) {
      return ServiceAccountClient;
    }
    throw new Error('ServiceAccountClient が未定義です');
  }

  function getQueuePath_() {
    var queue = PropertiesService.getScriptProperties().getProperty('FORM_SENDER_TASKS_QUEUE');
    if (!queue) {
      throw new Error('FORM_SENDER_TASKS_QUEUE が設定されていません');
    }
    return queue.replace(/^\/+/, '');
  }

  function buildTaskId_(payload) {
    var targetingId = (payload && payload.targeting_id != null) ? String(payload.targeting_id) : 'unknown';
    var runIndexBase = (payload && payload.execution && payload.execution.run_index_base != null)
      ? String(payload.execution.run_index_base)
      : '0';
    var triggeredAt = payload && payload.metadata && payload.metadata.triggered_at_jst;
    var datePart = null;
    if (typeof triggeredAt === 'string') {
      var match = triggeredAt.match(/^(\d{4})-(\d{2})-(\d{2})/);
      if (match && match.length === 4) {
        datePart = match[1] + match[2] + match[3];
      }
    }
    if (!datePart) {
      datePart = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd');
    }
    var rawId = ['fs', datePart, targetingId, runIndexBase].join('-');
    return rawId.replace(/[^A-Za-z0-9\-]/g, '-');
  }

  function getDispatcherUrl_() {
    var props = PropertiesService.getScriptProperties();
    var explicitUrl = props.getProperty('FORM_SENDER_DISPATCHER_URL');
    if (explicitUrl) {
      return explicitUrl;
    }

    var baseUrl = props.getProperty('FORM_SENDER_DISPATCHER_BASE_URL');
    if (!baseUrl) {
      throw new Error('FORM_SENDER_DISPATCHER_URL/BASE_URL が設定されていません');
    }

    var normalizedBase = String(baseUrl).trim().replace(/\/$/, '');
    return normalizedBase + '/v1/form-sender/tasks';
  }

  function getDispatcherServiceAccount_() {
    return PropertiesService.getScriptProperties().getProperty('FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT');
  }

  function getDispatcherApiBase_() {
    var url = getDispatcherUrl_();
    try {
      var parsed = new URL(url);
      return parsed.origin;
    } catch (e) {
      // Fallback: strip known suffixes manually
      var base = url.replace(/\/tasks$/, '');
      base = base.replace(/\/v1\/form-sender\/?$/, '');
      base = base.replace(/\/$/, '');
      return base;
    }
  }

  function fetchDispatcher_(path, options) {
    var base = getDispatcherApiBase_();
    var url = base + (path.charAt(0) === '/' ? path : '/' + path);
    var token = resolveServiceAccount_().getIdToken(base);
    var requestOptions = options || {};
    requestOptions.muteHttpExceptions = true;
    requestOptions.headers = requestOptions.headers || {};
    requestOptions.headers.Authorization = 'Bearer ' + token;
    if (requestOptions.payload && requestOptions.contentType === 'application/json' && typeof requestOptions.payload !== 'string') {
      requestOptions.payload = JSON.stringify(requestOptions.payload);
    }
    return UrlFetchApp.fetch(url, requestOptions);
  }

  function validateConfig(clientConfig) {
    if (!clientConfig) {
      throw new Error('clientConfig is required for validation');
    }
    var response = fetchDispatcher_('/v1/form-sender/validate-config', {
      method: 'post',
      contentType: 'application/json',
      payload: { client_config: clientConfig }
    });
    var status = response.getResponseCode();
    if (status >= 300) {
      var text = response.getContentText();
      var detail = text || '';
      if (text) {
        try {
          var parsed = JSON.parse(text);
          if (parsed && parsed.detail) {
            detail = parsed.detail;
          }
        } catch (e) {
          // ignore parse error, keep raw text
        }
      }
      throw new Error('dispatcher validate-config 失敗: ' + (detail || status));
    }
    var body = response.getContentText();
    return body ? JSON.parse(body) : { status: 'ok' };
  }

  function enqueue(payload) {
    var queuePath = getQueuePath_();
    var dispatcherUrl = getDispatcherUrl_();
    var serviceAccountClient = resolveServiceAccount_();
    var token = serviceAccountClient.getAccessToken(['https://www.googleapis.com/auth/cloud-platform']);
    var createTaskUrl = 'https://cloudtasks.googleapis.com/v2/' + encodeURI(queuePath) + '/tasks';

    var now = new Date();
    var scheduleTimeIso = new Date(now.getTime() + 1000).toISOString();
    var body = {
      httpRequest: {
        httpMethod: 'POST',
        url: dispatcherUrl,
        headers: {
          'Content-Type': 'application/json'
        },
        body: Utilities.base64Encode(JSON.stringify(payload))
      },
      scheduleTime: scheduleTimeIso
    };
    var taskId = buildTaskId_(payload);
    body.name = queuePath + '/tasks/' + taskId;
    var dispatcherServiceAccount = getDispatcherServiceAccount_();
    var audienceUrl = getDispatcherApiBase_();
    var idToken = serviceAccountClient.getIdToken(audienceUrl);
    body.httpRequest.headers.Authorization = 'Bearer ' + idToken;

    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'cloud_tasks_enqueue_request',
        queue: queuePath,
        dispatcher_url: dispatcherUrl,
        request_sa: serviceAccountClient.getServiceAccountEmail(),
        uses_id_token: true,
        oidc_service_account: dispatcherServiceAccount || null
      }));
    } catch (loggingError) {
      // ログ出力に失敗しても処理は継続
    }

    var response = UrlFetchApp.fetch(createTaskUrl, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ task: body }),
      muteHttpExceptions: true,
      headers: {
        Authorization: 'Bearer ' + token
      }
    });
    var statusCode = response.getResponseCode();
    var responseText = response.getContentText();

    try {
      console.log(JSON.stringify({
        level: 'info',
        event: 'cloud_tasks_enqueue_response',
        statusCode: statusCode,
        task_id: taskId,
        success: statusCode < 300
      }));
    } catch (logErr) {
      // ignore logging failure
    }

    if (statusCode >= 300) {
      var errorPayload = null;
      if (responseText) {
        try {
          errorPayload = JSON.parse(responseText);
        } catch (parseErr) {
          errorPayload = responseText;
        }
      }
      try {
        console.error(JSON.stringify({
          level: 'error',
          event: 'cloud_tasks_enqueue_failure',
          statusCode: statusCode,
          queue: queuePath,
          dispatcher_url: dispatcherUrl,
          oidc_service_account: dispatcherServiceAccount || null,
          uses_id_token: true,
          response: errorPayload
        }));
      } catch (logErr) {
        // ignore logging failure
      }
      if (statusCode === 409) {
        var duplicateDetected = false;
        var parsedError = null;
        if (responseText) {
          try {
            parsedError = JSON.parse(responseText);
            if (parsedError && parsedError.error && parsedError.error.status === 'ALREADY_EXISTS') {
              duplicateDetected = true;
            }
          } catch (parseError) {
            // noop
          }
        }
        if (!duplicateDetected && responseText && responseText.indexOf('ALREADY_EXISTS') !== -1) {
          duplicateDetected = true;
        }
        if (duplicateDetected) {
          console.log('Cloud Tasks duplicate detected. taskId=' + taskId + ' を成功扱いにします。');
          return {
            name: body.name,
            duplicate: true,
            status: 'ALREADY_EXISTS',
            detail: parsedError && parsedError.error ? parsedError.error.message : null
          };
        }
      }
      throw new Error('Cloud Tasks enqueue 失敗: ' + responseText);
    }
    return responseText ? JSON.parse(responseText) : { name: body.name };
  }

  function listExecutions(status, targetingId) {
    var params = [];
    if (status) {
      params.push('status=' + encodeURIComponent(status));
    }
    if (typeof targetingId !== 'undefined' && targetingId !== null) {
      params.push('targeting_id=' + encodeURIComponent(targetingId));
    }
    var path = '/v1/form-sender/executions';
    if (params.length > 0) {
      path += '?' + params.join('&');
    }
    var response = fetchDispatcher_(path, { method: 'get' });
    if (response.getResponseCode() >= 300) {
      throw new Error('dispatcher list executions 失敗: ' + response.getContentText());
    }
    var text = response.getContentText();
    return text ? JSON.parse(text) : { executions: [] };
  }

  function listRunningExecutions(targetingId) {
    return listExecutions('running', targetingId);
  }

  function cancelExecution(executionId) {
    if (!executionId) {
      throw new Error('executionId is required');
    }
    var response = fetchDispatcher_('/v1/form-sender/executions/' + encodeURIComponent(executionId) + '/cancel', {
      method: 'post',
      contentType: 'application/json',
      payload: '{}'
    });
    if (response.getResponseCode() >= 300) {
      throw new Error('dispatcher cancel execution 失敗: ' + response.getContentText());
    }
    var text = response.getContentText();
    return text ? JSON.parse(text) : { status: 'cancelled', execution_id: executionId };
  }

  return {
    enqueue: enqueue,
    validateConfig: validateConfig,
    listExecutions: listExecutions,
    listRunningExecutions: listRunningExecutions,
    cancelExecution: cancelExecution
  };
})();
