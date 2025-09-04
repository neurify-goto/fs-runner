/**
 * GitHub API統合モジュール（Form Analyzer用）
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
    const url = `${FORM_ANALYZER_CONFIG.GITHUB_API_BASE}/repos/${GITHUB_CONFIG.OWNER}/${GITHUB_CONFIG.REPO}/dispatches`;
    
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
      muteHttpExceptions: true
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
    'form_analyzer': 'form_analyzer_task'
  };
  
  return eventTypeMapping[taskType] || 'form_analyzer_task';
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
    
    const url = `${FORM_ANALYZER_CONFIG.GITHUB_API_BASE}/rate_limit`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-Batch-Processor/1.0'
      },
      muteHttpExceptions: true
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
    
    let url = `${FORM_ANALYZER_CONFIG.GITHUB_API_BASE}/repos/${GITHUB_CONFIG.OWNER}/${GITHUB_CONFIG.REPO}/actions/runs?per_page=10`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-Batch-Processor/1.0'
      },
      muteHttpExceptions: true
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
    const url = `${FORM_ANALYZER_CONFIG.GITHUB_API_BASE}/user`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'GAS-Batch-Processor/1.0'
      },
      muteHttpExceptions: true
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
 * リトライ機能付きRepository Dispatch送信
 * @param {string} taskType タスクタイプ
 * @param {string} batchId バッチID
 * @param {Array} batchData バッチデータ
 * @param {number} maxRetries 最大リトライ回数
 * @returns {Object} 送信結果
 */
function sendRepositoryDispatchWithRetry(taskType, batchId, batchData, maxRetries = FORM_ANALYZER_CONFIG.MAX_RETRIES) {
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
      const delay = FORM_ANALYZER_CONFIG.RETRY_DELAY * attempt; // 指数バックオフ
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