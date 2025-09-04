/**
 * GitHub API統合モジュール
 * Repository Dispatch イベント送信機能
 */

// GitHub リポジトリ設定
const GITHUB_CONFIG = {
  OWNER: 'neurify-goto',  // GitHubオーナー名
  REPO: 'fs-runner'  // リポジトリ名
};

/**
 * Repository Dispatch イベント送信
 * @param {string} taskType タスクタイプ
 * @param {string} batchId バッチID
 * @param {Array} batchData バッチデータ
 * @returns {Object} 送信結果
 */
function sendRepositoryDispatch(taskType, batchId, batchData) {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    // イベントタイプを決定
    const eventType = getEventTypeFromTaskType(taskType);
    
    // ペイロード構築
    const payload = {
      event_type: eventType,
      client_payload: {
        batch_id: batchId,
        task_type: taskType,
        batch_data: batchData,
        triggered_at: new Date().toISOString(),
        gas_version: '1.0.0'
      }
    };
    
    // Repository Dispatch API呼び出し
    const url = `${CONFIG.GITHUB_API_BASE}/repos/${GITHUB_CONFIG.OWNER}/${GITHUB_CONFIG.REPO}/dispatches`;
    
    console.log(`Repository Dispatch送信: ${url}`);
    console.log(`Event Type: ${eventType}, Batch ID: ${batchId}, データ件数: ${batchData.length}`);
    
    const response = UrlFetchApp.fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'GAS-Batch-Processor/1.0'
      },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode === 204) {
      console.log('Repository Dispatch送信成功');
      return { success: true };
    } else {
      console.error(`Repository Dispatch送信失敗: ${responseCode} - ${responseText}`);
      return { 
        success: false, 
        error: `HTTP ${responseCode}: ${responseText}`,
        batch_id: batchId
      };
    }
    
  } catch (error) {
    console.error('Repository Dispatch送信エラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      batch_id: batchId
    };
  }
}

/**
 * タスクタイプからイベントタイプを取得
 * @param {string} taskType タスクタイプ
 * @returns {string} GitHub Actions イベントタイプ
 */
function getEventTypeFromTaskType(taskType) {
  const eventTypeMapping = {
    'fuma_detail': 'fetch_detail_task'
  };
  
  return eventTypeMapping[taskType] || 'fetch_detail_task';
}

/**
 * GitHub APIレート制限確認
 * @returns {Object} レート制限情報
 */
function checkGitHubRateLimit() {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    const url = `${CONFIG.GITHUB_API_BASE}/rate_limit`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-Batch-Processor/1.0'
      },
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200) {
      const data = JSON.parse(response.getContentText());
      
      const coreLimit = data.resources.core;
      console.log('GitHub API レート制限情報:');
      console.log(`コアAPI: ${coreLimit.remaining}/${coreLimit.limit} (リセット: ${new Date(coreLimit.reset * 1000)})`);
      
      return {
        success: true,
        core: {
          limit: coreLimit.limit,
          remaining: coreLimit.remaining,
          reset_at: new Date(coreLimit.reset * 1000),
          usage_rate: Math.round(((coreLimit.limit - coreLimit.remaining) / coreLimit.limit) * 100)
        }
      };
    } else {
      console.error(`レート制限確認失敗: ${responseCode}`);
      return { success: false, error: `HTTP ${responseCode}` };
    }
    
  } catch (error) {
    console.error('レート制限確認エラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * 特定ワークフローの実行状況確認
 * @param {string} batchId バッチID（オプション）
 * @returns {Object} ワークフロー実行情報
 */
function checkWorkflowRuns(batchId = null) {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    let url = `${CONFIG.GITHUB_API_BASE}/repos/${GITHUB_CONFIG.OWNER}/${GITHUB_CONFIG.REPO}/actions/runs?per_page=10`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-Batch-Processor/1.0'
      },
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200) {
      const data = JSON.parse(response.getContentText());
      
      // 最近の実行状況をログ出力
      console.log(`=== 最新ワークフロー実行状況 (${data.workflow_runs.length}件) ===`);
      
      data.workflow_runs.slice(0, 5).forEach(run => {
        console.log(`ID: ${run.id}, Status: ${run.status}, Conclusion: ${run.conclusion}, Created: ${run.created_at}`);
      });
      
      // 特定バッチIDでフィルタ（完全一致は困難だが参考情報として）
      if (batchId) {
        const relatedRuns = data.workflow_runs.filter(run => 
          run.head_commit?.message?.includes(batchId) || 
          run.name?.includes(batchId)
        );
        
        console.log(`Batch ID「${batchId}」関連の実行: ${relatedRuns.length}件`);
      }
      
      return {
        success: true,
        total_count: data.total_count,
        recent_runs: data.workflow_runs.slice(0, 5).map(run => ({
          id: run.id,
          status: run.status,
          conclusion: run.conclusion,
          created_at: run.created_at,
          updated_at: run.updated_at
        }))
      };
    } else {
      console.error(`ワークフロー実行確認失敗: ${responseCode}`);
      return { success: false, error: `HTTP ${responseCode}` };
    }
    
  } catch (error) {
    console.error('ワークフロー実行確認エラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * GitHub API接続テスト
 * @returns {Object} テスト結果
 */
function testGitHubConnection() {
  try {
    console.log('GitHub API接続テスト開始');
    
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      return { success: false, error: 'GITHUB_TOKEN が設定されていません' };
    }
    
    // 認証ユーザー情報取得でテスト
    const url = `${CONFIG.GITHUB_API_BASE}/user`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-Batch-Processor/1.0'
      },
      muteHttpExceptions: true,
      timeout: CONFIG.TIMEOUT_MS
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200) {
      const userData = JSON.parse(response.getContentText());
      console.log(`GitHub API接続テスト成功: ${userData.login}`);
      
      return { 
        success: true, 
        message: `GitHub接続成功 (ユーザー: ${userData.login})`,
        user: userData.login
      };
    } else {
      const errorText = response.getContentText();
      console.error(`GitHub API接続テスト失敗: ${responseCode} - ${errorText}`);
      return { success: false, error: `HTTP ${responseCode}: ${errorText}` };
    }
    
  } catch (error) {
    console.error('GitHub API接続テストエラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * エラー分類・解析機能
 */

/**
 * APIエラーを原因別に分類
 * @param {number} responseCode HTTPレスポンスコード
 * @param {string} responseText レスポンステキスト
 * @param {Error} error エラーオブジェクト（ある場合）
 * @returns {Object} エラー分類結果
 */
function classifyAPIError(responseCode, responseText, error = null) {
  const result = {
    category: 'unknown',
    severity: 'medium',
    retryable: false,
    suggestedDelay: 0,
    description: 'Unknown error'
  };
  
  try {
    // ネットワーク関連エラー
    if (error && (error.toString().includes('timeout') || error.toString().includes('network'))) {
      result.category = 'network';
      result.severity = 'low';
      result.retryable = true;
      result.suggestedDelay = 5000;
      result.description = 'ネットワーク接続エラー';
      return result;
    }
    
    // HTTPステータスコード別分類
    switch (responseCode) {
      case 401:
        result.category = 'authentication';
        result.severity = 'high';
        result.retryable = false;
        result.description = '認証エラー（トークン無効）';
        break;
        
      case 403:
        // レート制限かどうかチェック
        if (responseText && responseText.includes('rate limit')) {
          result.category = 'rate_limit';
          result.severity = 'medium';
          result.retryable = true;
          result.suggestedDelay = 60000; // 1分間待機
          result.description = 'APIレート制限';
        } else {
          result.category = 'permission';
          result.severity = 'high';
          result.retryable = false;
          result.description = '権限不足';
        }
        break;
        
      case 404:
        result.category = 'not_found';
        result.severity = 'medium';
        result.retryable = false;
        result.description = 'リソースが見つからない';
        break;
        
      case 422:
        result.category = 'validation';
        result.severity = 'medium';
        result.retryable = false;
        result.description = 'バリデーションエラー';
        break;
        
      case 500:
      case 502:
      case 503:
      case 504:
        result.category = 'server_error';
        result.severity = 'medium';
        result.retryable = true;
        result.suggestedDelay = 10000;
        result.description = 'サーバーエラー（一時的）';
        break;
        
      default:
        if (responseCode >= 400 && responseCode < 500) {
          result.category = 'client_error';
          result.severity = 'medium';
          result.retryable = false;
          result.description = 'クライアントエラー';
        } else if (responseCode >= 500) {
          result.category = 'server_error';
          result.severity = 'medium';
          result.retryable = true;
          result.suggestedDelay = 5000;
          result.description = 'サーバーエラー';
        }
        break;
    }
    
  } catch (classifyError) {
    console.warn('エラー分類処理でエラー:', classifyError);
  }
  
  return result;
}

/**
 * 指数バックオフ計算
 * @param {number} attempt 試行回数（1から開始）
 * @param {number} baseDelay 基本遅延時間（ミリ秒）
 * @param {number} maxDelay 最大遅延時間（ミリ秒）
 * @param {number} multiplier 倍率
 * @returns {number} 計算された遅延時間
 */
function calculateExponentialBackoff(attempt, baseDelay = 1000, maxDelay = 30000, multiplier = 2) {
  const delay = baseDelay * Math.pow(multiplier, attempt - 1);
  return Math.min(delay, maxDelay);
}

/**
 * 連続失敗回数を管理するヘルパー
 */
const FailureTracker = {
  failures: {},
  
  /**
   * 失敗回数を記録
   * @param {string} key 識別キー
   */
  recordFailure(key) {
    this.failures[key] = (this.failures[key] || 0) + 1;
  },
  
  /**
   * 成功時にリセット
   * @param {string} key 識別キー
   */
  recordSuccess(key) {
    delete this.failures[key];
  },
  
  /**
   * 失敗回数を取得
   * @param {string} key 識別キー
   * @returns {number} 失敗回数
   */
  getFailureCount(key) {
    return this.failures[key] || 0;
  },
  
  /**
   * 失敗回数が上限を超えているかチェック
   * @param {string} key 識別キー
   * @param {number} maxFailures 上限回数
   * @returns {boolean} 上限超過フラグ
   */
  isExceedingLimit(key, maxFailures) {
    return this.getFailureCount(key) >= maxFailures;
  }
};

/**
 * API最適化・キャッシュ機能
 */

/**
 * インメモリキャッシュシステム
 */
const APICache = {
  cache: {},
  
  /**
   * キャッシュキーを生成
   * @param {string} url URL
   * @param {Object} headers ヘッダー（認証情報を除く）
   * @returns {string} キャッシュキー
   */
  generateKey(url, headers = {}) {
    // 認証情報を除いたヘッダーでキーを生成
    const safeHeaders = Object.fromEntries(
      Object.entries(headers).filter(([key]) => 
        !key.toLowerCase().includes('authorization')
      )
    );
    return `${url}_${JSON.stringify(safeHeaders)}`;
  },
  
  /**
   * キャッシュから取得
   * @param {string} key キャッシュキー
   * @param {number} maxAge 最大保持期間（ミリ秒）
   * @returns {*} キャッシュ値またはnull
   */
  get(key, maxAge) {
    const cached = this.cache[key];
    if (!cached) return null;
    
    const now = Date.now();
    if (now - cached.timestamp > maxAge) {
      delete this.cache[key];
      return null;
    }
    
    console.log(`キャッシュヒット: ${key}`);
    return cached.value;
  },
  
  /**
   * キャッシュに設定
   * @param {string} key キャッシュキー
   * @param {*} value 値
   */
  set(key, value) {
    this.cache[key] = {
      value: value,
      timestamp: Date.now()
    };
    console.log(`キャッシュ設定: ${key}`);
  },
  
  /**
   * キャッシュをクリア
   * @param {string} keyPattern 削除対象のキーパターン（部分一致）
   */
  clear(keyPattern = null) {
    if (keyPattern) {
      const keysToDelete = Object.keys(this.cache).filter(key => 
        key.includes(keyPattern)
      );
      keysToDelete.forEach(key => delete this.cache[key]);
      console.log(`パターンマッチキャッシュクリア: ${keyPattern} (${keysToDelete.length}個)`);
    } else {
      this.cache = {};
      console.log('全キャッシュクリア');
    }
  },
  
  /**
   * キャッシュサイズとステータス取得
   * @returns {Object} キャッシュ情報
   */
  getStatus() {
    const keys = Object.keys(this.cache);
    const now = Date.now();
    
    const status = {
      totalEntries: keys.length,
      validEntries: 0,
      expiredEntries: 0,
      oldestEntry: null,
      newestEntry: null
    };
    
    keys.forEach(key => {
      const entry = this.cache[key];
      const age = now - entry.timestamp;
      
      // 仮に2分を基準として判定
      if (age > 120000) {
        status.expiredEntries++;
      } else {
        status.validEntries++;
      }
      
      if (!status.oldestEntry || entry.timestamp < status.oldestEntry) {
        status.oldestEntry = entry.timestamp;
      }
      if (!status.newestEntry || entry.timestamp > status.newestEntry) {
        status.newestEntry = entry.timestamp;
      }
    });
    
    return status;
  }
};

/**
 * キャッシュ機能付きAPI呼び出し
 * @param {string} url URL
 * @param {Object} options fetch options
 * @param {number} cacheMaxAge キャッシュ最大保持期間（ミリ秒）
 * @returns {Object} API応答またはキャッシュ結果
 */
function fetchWithCache(url, options = {}, cacheMaxAge = 120000) {
  const config = CONFIG;
  const actualCacheMaxAge = cacheMaxAge || config.API_CACHE_DURATION_MS || 120000;
  
  // キャッシュキー生成
  const cacheKey = APICache.generateKey(url, options.headers || {});
  
  // キャッシュチェック
  const cached = APICache.get(cacheKey, actualCacheMaxAge);
  if (cached) {
    return {
      fromCache: true,
      ...cached
    };
  }
  
  try {
    // 実際のAPI呼び出し
    console.log(`API呼び出し実行: ${url}`);
    const response = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true,
      timeout: config.TIMEOUT_MS || 60000,
      ...options
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    const result = {
      responseCode: responseCode,
      responseText: responseText,
      success: responseCode === 200,
      fromCache: false
    };
    
    // 成功した場合のみキャッシュに保存
    if (responseCode === 200) {
      APICache.set(cacheKey, result);
    }
    
    return result;
    
  } catch (error) {
    console.error('API呼び出しエラー:', error);
    return {
      responseCode: 0,
      responseText: '',
      success: false,
      error: error.toString(),
      fromCache: false
    };
  }
}

/**
 * ワークフロー判定の堅牢化機能
 */

/**
 * ワークフローがfetch_detail_taskかどうかを複数条件で判定
 * @param {Object} workflowRun GitHub Actions workflow run オブジェクト
 * @returns {boolean} fetch_detail_taskワークフローかどうか
 */
function isFetchDetailWorkflow(workflowRun) {
  const criteria = {
    nameMatch: false,
    pathMatch: false,
    eventMatch: false,
    headBranchMatch: false
  };
  
  try {
    // 1. ワークフロー名による判定
    if (workflowRun.name) {
      criteria.nameMatch = workflowRun.name === 'Fetch Detail' ||
                          workflowRun.name.toLowerCase().includes('fetch-detail') ||
                          workflowRun.name.toLowerCase().includes('fetch_detail');
    }
    
    // 2. ワークフローファイルパスによる判定
    if (workflowRun.path) {
      criteria.pathMatch = workflowRun.path.includes('fetch-detail.yml') ||
                          workflowRun.path.includes('fetch-detail.yaml') ||
                          workflowRun.path.includes('fetch_detail.yml') ||
                          workflowRun.path.includes('fetch_detail.yaml');
    }
    
    // 3. トリガーイベントによる判定
    if (workflowRun.event === 'repository_dispatch') {
      criteria.eventMatch = true;
    }
    
    // 4. ヘッドブランチ情報（追加の確認）
    if (workflowRun.head_branch) {
      // 通常はmainまたは特定のブランチから実行される
      criteria.headBranchMatch = ['main', 'master', 'develop'].includes(workflowRun.head_branch) ||
                                workflowRun.head_branch.includes('feature/fetch-detail');
    }
    
    // 判定ロジック: 複数条件の組み合わせで堅牢性を確保
    const primaryMatch = criteria.nameMatch || criteria.pathMatch;
    const secondaryMatch = criteria.eventMatch;
    
    // プライマリ条件（名前またはパス）とセカンダリ条件（イベント）の組み合わせ
    const isMatch = primaryMatch && (secondaryMatch || criteria.headBranchMatch);
    
    // デバッグ情報出力
    if (isMatch) {
      console.log(`ワークフロー判定一致: ID=${workflowRun.id}, 名前=${workflowRun.name}, イベント=${workflowRun.event}`);
      console.log(`判定詳細: name=${criteria.nameMatch}, path=${criteria.pathMatch}, event=${criteria.eventMatch}, branch=${criteria.headBranchMatch}`);
    }
    
    return isMatch;
    
  } catch (error) {
    console.warn(`ワークフロー判定エラー (ID: ${workflowRun.id}):`, error);
    
    // エラー時はフォールバック: 基本的な名前判定のみ
    return workflowRun.name === 'Fetch Detail';
  }
}

/**
 * ワークフロー判定のテスト用関数
 * @param {Object} testWorkflowRun テスト用ワークフローオブジェクト
 * @returns {Object} テスト結果
 */
function testWorkflowIdentification(testWorkflowRun) {
  console.log('=== ワークフロー判定テスト ===');
  
  const sampleWorkflows = testWorkflowRun ? [testWorkflowRun] : [
    {
      id: 1,
      name: 'Fetch Detail',
      path: '.github/workflows/fetch-detail.yml',
      event: 'repository_dispatch',
      head_branch: 'main'
    },
    {
      id: 2,
      name: 'Form Finder',
      path: '.github/workflows/form-finder.yml',
      event: 'repository_dispatch',
      head_branch: 'main'
    },
    {
      id: 3,
      name: 'Fetch Detail Legacy',
      path: '.github/workflows/fetch_detail_old.yml',
      event: 'repository_dispatch',
      head_branch: 'feature/fetch-detail-update'
    },
    {
      id: 4,
      name: 'Other Workflow',
      path: '.github/workflows/other.yml',
      event: 'push',
      head_branch: 'main'
    }
  ];
  
  const results = sampleWorkflows.map(workflow => {
    const isMatch = isFetchDetailWorkflow(workflow);
    console.log(`ワークフロー ${workflow.id} (${workflow.name}): ${isMatch ? '一致' : '不一致'}`);
    return { workflow, isMatch };
  });
  
  console.log('=== ワークフロー判定テスト完了 ===');
  return results;
}

/**
 * 改善された並列実行数チェック（キャッシュ・エラーハンドリング強化版）
 * @returns {Object} チェック結果 - {success: boolean, runningCount: number, error?: string}
 */
function checkRunningFetchDetailWorkflows() {
  const functionKey = 'checkRunningWorkflows';
  const config = CONFIG;
  
  try {
    // 連続失敗回数チェック
    const maxFailures = config.MAX_CONSECUTIVE_API_FAILURES || 3;
    if (FailureTracker.isExceedingLimit(functionKey, maxFailures)) {
      console.warn(`並列実行数チェック: 連続失敗回数が上限(${maxFailures})に達したため、グレースフルデグラデーション`);
      
      if (config.GRACEFUL_DEGRADATION_ENABLED) {
        // グレースフルデグラデーション: 仮の値を返して処理継続
        return {
          success: true,
          runningCount: 0, // 安全のため0と仮定
          degraded: true,
          reason: '連続API失敗によるデグラデーション'
        };
      } else {
        return {
          success: false,
          error: `連続${maxFailures}回失敗のため一時停止`,
          runningCount: 0
        };
      }
    }
    
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      FailureTracker.recordFailure(functionKey);
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    // 実行中のワークフローを取得（キャッシュ機能付き）
    const url = `${config.GITHUB_API_BASE}/repos/${GITHUB_CONFIG.OWNER}/${GITHUB_CONFIG.REPO}/actions/runs?status=in_progress&per_page=100`;
    
    console.log(`並列実行数チェック開始: ${url}`);
    
    const apiResult = fetchWithCache(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-Batch-Processor/1.0'
      }
    }, config.API_CACHE_DURATION_MS);
    
    const responseCode = apiResult.responseCode;
    const responseText = apiResult.responseText;
    
    if (apiResult.fromCache) {
      console.log('並列実行数チェック: キャッシュから取得');
    }
    
    if (responseCode === 200) {
      const data = JSON.parse(responseText);
      
      // fetch_detail_taskワークフローのみフィルタ（堅牢化版）
      const fetchDetailRuns = data.workflow_runs.filter(run => {
        return isFetchDetailWorkflow(run);
      });
      
      const runningCount = fetchDetailRuns.length;
      
      console.log(`fetch_detail_taskワークフロー並列実行数: ${runningCount}個`);
      
      if (runningCount > 0) {
        console.log('実行中のfetch_detail_taskワークフロー:');
        fetchDetailRuns.forEach((run, index) => {
          console.log(`  ${index + 1}. ID: ${run.id}, 開始: ${run.created_at}, Status: ${run.status}`);
        });
      }
      
      // 成功時は失敗カウンターをリセット
      FailureTracker.recordSuccess(functionKey);
      
      return {
        success: true,
        runningCount: runningCount,
        runningWorkflows: fetchDetailRuns.map(run => ({
          id: run.id,
          created_at: run.created_at,
          status: run.status
        }))
      };
    } else {
      // エラー分類とリトライ可能性判定
      const errorClassification = classifyAPIError(responseCode, responseText);
      
      console.error(`並列実行数チェック失敗: ${responseCode} - ${responseText}`);
      console.error(`エラー分類: ${errorClassification.category} (${errorClassification.severity}) - ${errorClassification.description}`);
      
      // 失敗を記録
      FailureTracker.recordFailure(functionKey);
      
      return { 
        success: false, 
        error: `HTTP ${responseCode}: ${responseText}`,
        errorClassification: errorClassification,
        runningCount: 0,
        retryable: errorClassification.retryable,
        suggestedDelay: errorClassification.suggestedDelay
      };
    }
    
  } catch (error) {
    console.error('並列実行数チェックエラー:', error);
    
    // ネットワークエラーの分類
    const errorClassification = classifyAPIError(0, '', error);
    FailureTracker.recordFailure(functionKey);
    
    return { 
      success: false, 
      error: error.toString(),
      errorClassification: errorClassification,
      runningCount: 0,
      retryable: errorClassification.retryable,
      suggestedDelay: errorClassification.suggestedDelay
    };
  }
}

/**
 * リトライ機能付きRepository Dispatch送信
 * @param {string} taskType タスクタイプ
 * @param {string} batchId バッチID
 * @param {Array} batchData バッチデータ
 * @param {number} maxRetries 最大リトライ回数
 * @returns {Object} 送信結果
 */
function sendRepositoryDispatchWithRetry(taskType, batchId, batchData, maxRetries = CONFIG.MAX_RETRIES) {
  let lastError = null;
  
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    console.log(`Repository Dispatch送信試行 ${attempt}/${maxRetries}: batch_id=${batchId}`);
    
    const result = sendRepositoryDispatch(taskType, batchId, batchData);
    
    if (result.success) {
      if (attempt > 1) {
        console.log(`Repository Dispatch送信成功 (${attempt}回目で成功): batch_id=${batchId}`);
      }
      return result;
    }
    
    lastError = result.error;
    
    if (attempt < maxRetries) {
      const delay = CONFIG.RETRY_DELAY * attempt; // 指数バックオフ
      console.log(`Repository Dispatch送信失敗、${delay}ms後にリトライ: ${result.error}`);
      Utilities.sleep(delay);
    }
  }
  
  console.error(`Repository Dispatch送信最終失敗: batch_id=${batchId}, エラー: ${lastError}`);
  return { 
    success: false, 
    error: `${maxRetries}回リトライ後も失敗: ${lastError}`,
    batch_id: batchId
  };
}
