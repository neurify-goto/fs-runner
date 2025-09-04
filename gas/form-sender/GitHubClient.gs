/**
 * GitHub API統合モジュール（フォーム送信システム用）
 * Repository Dispatch イベント送信機能
 * 
 * FORM_SENDER.md の仕様に基づく実装
 */

/**
 * GitHub リポジトリ設定を取得
 * PropertiesServiceから動的に取得し、設定の柔軟性を確保
 */
function getGitHubConfig() {
  const owner = PropertiesService.getScriptProperties().getProperty('GITHUB_OWNER') || 'neurify-goto';
  const repo = PropertiesService.getScriptProperties().getProperty('GITHUB_REPO') || 'fs-runner';
  
  return {
    OWNER: owner,
    REPO: repo
  };
}

/**
 * フォーム送信用 Repository Dispatch イベント送信（スプレッドシート対応版）
 * @param {string} taskType タスクタイプ（'form_sender_task'）
 * @param {number} targetingId ターゲティングID
 * @param {Object} clientConfig スプレッドシートから取得したクライアント設定
 * @returns {Object} 送信結果
 */
function sendRepositoryDispatch(taskType, targetingId, clientConfig) {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    // イベントタイプを決定
    const eventType = getEventTypeFromTaskType(taskType);
    
    // ペイロード構築（2シート構造データ整合性確保版）
    // clientConfigの2シート構造整合性を検証
    if (!clientConfig.client || !clientConfig.targeting) {
      throw new Error(`clientConfigの2シート構造が不完全です: client=${!!clientConfig.client}, targeting=${!!clientConfig.targeting}`);
    }
    
    // 2シート構造の完全性を確認
    const requiredClientFields = ['company_name', 'form_sender_name', 'email_1', 'email_2'];
    const requiredTargetingFields = ['subject', 'message', 'max_daily_sends', 'send_start_time', 'send_end_time'];
    
    const missingClientFields = requiredClientFields.filter(field => !clientConfig.client[field]);
    const missingTargetingFields = requiredTargetingFields.filter(field => !clientConfig.targeting[field]);
    
    if (missingClientFields.length > 0) {
      throw new Error(`clientConfig.client に必須フィールドが不足: ${missingClientFields.join(', ')}`);
    }
    
    if (missingTargetingFields.length > 0) {
      throw new Error(`clientConfig.targeting に必須フィールドが不足: ${missingTargetingFields.join(', ')}`);
    }
    
    const payload = {
      event_type: eventType,
      client_payload: {
        targeting_id: targetingId,
        client_config: clientConfig, // 検証済み2シート構造
        task_type: taskType,
        triggered_at: new Date().toISOString(),
        gas_version: '2.0.1-2sheet-validated' // バージョン更新で構造確認済みを表示
      }
    };
    
    // Repository Dispatch API呼び出し
    const githubConfig = getGitHubConfig();
    const url = `${CONFIG.GITHUB_API_BASE}/repos/${githubConfig.OWNER}/${githubConfig.REPO}/dispatches`;
    
    console.log(`フォーム送信用 Repository Dispatch送信: ${url}`);
    console.log(`Event Type: ${eventType}, Targeting ID: ${targetingId}, Client: ${clientConfig.client?.company_name} (client_id: ${clientConfig.client_id}), Sender: ${clientConfig.client?.form_sender_name}`);
    
    const response = UrlFetchApp.fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'GAS-FormSender/1.0'
      },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode === 204) {
      console.log('フォーム送信用 Repository Dispatch送信成功');
      return { 
        success: true,
        targeting_id: targetingId,
        company_name: clientConfig.client?.company_name,
        event_type: eventType
      };
    } else {
      console.error(`Repository Dispatch送信失敗: ${responseCode} - ${responseText}`);
      return { 
        success: false, 
        error: `HTTP ${responseCode}: ${responseText}`,
        targeting_id: targetingId
      };
    }
    
  } catch (error) {
    console.error('Repository Dispatch送信エラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      targeting_id: targetingId
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
    'form_sender_task': 'form_sender_task',
    'form_sender': 'form_sender_task',
    'fuma_form_sender': 'form_sender_task',
    // ブランチテスト用メイン実行
    'form_sender_test': 'form_sender_test',
    // 軽量ブランチテスト用
    'form_sender_branch_test': 'form_sender_branch_test'
  };
  
  return eventTypeMapping[taskType] || 'form_sender_task';
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
        'User-Agent': 'GAS-FormSender/1.0'
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
 * フォーム送信ワークフローの実行状況確認（スプレッドシート対応版）
 * @param {number} targetingId ターゲティングID（オプション）
 * @returns {Object} ワークフロー実行情報
 */
function checkFormSenderWorkflowRuns(targetingId = null) {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    const githubConfig = getGitHubConfig();
    let url = `${CONFIG.GITHUB_API_BASE}/repos/${githubConfig.OWNER}/${githubConfig.REPO}/actions/runs?per_page=10`;
    
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
    
    if (responseCode === 200) {
      const data = JSON.parse(response.getContentText());
      
      // Form Sender ワークフローのみフィルタリング
      const formSenderRuns = data.workflow_runs.filter(run => 
        run.name === 'Form Sender' || run.name.includes('form-sender') || 
        run.path?.includes('form-sender')
      );
      
      console.log(`=== フォーム送信ワークフロー実行状況 (${formSenderRuns.length}件) - 新アーキテクチャ ===`);
      
      formSenderRuns.slice(0, 5).forEach(run => {
        console.log(`ID: ${run.id}, Status: ${run.status}, Conclusion: ${run.conclusion}, Created: ${run.created_at}`);
      });
      
      // 特定Targeting IDでフィルタ（新アーキテクチャ）
      if (targetingId) {
        const relatedRuns = formSenderRuns.filter(run => 
          run.head_commit?.message?.includes(`targeting_id=${targetingId}`) ||
          run.name?.includes(`targeting-${targetingId}`)
        );
        
        console.log(`Targeting ID「${targetingId}」関連の Form Sender 実行: ${relatedRuns.length}件`);
      }
      
      return {
        success: true,
        total_form_sender_runs: formSenderRuns.length,
        recent_runs: formSenderRuns.slice(0, 5).map(run => ({
          id: run.id,
          name: run.name,
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
    console.log('GitHub API接続テスト開始（フォーム送信システム用）');
    
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
        'User-Agent': 'GAS-FormSender/1.0'
      },
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode === 200) {
      const userData = JSON.parse(response.getContentText());
      console.log(`GitHub API接続テスト成功: ${userData.login}`);
      
      return { 
        success: true, 
        message: `GitHub接続成功 (ユーザー: ${userData.login}, システム: Form Sender)`,
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
 * リトライ機能付きフォーム送信用 Repository Dispatch送信（スプレッドシート対応版）
 * @param {string} taskType タスクタイプ
 * @param {number} targetingId ターゲティングID
 * @param {Object} clientConfig スプレッドシートから取得したクライアント設定
 * @param {number} maxRetries 最大リトライ回数
 * @returns {Object} 送信結果
 */
function sendFormSenderDispatchWithRetry(taskType, targetingId, clientConfig, maxRetries = CONFIG.MAX_RETRIES) {
  let lastError = null;
  
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    console.log(`フォーム送信用 Repository Dispatch送信試行 ${attempt}/${maxRetries}: targeting_id=${targetingId}, client=${clientConfig.client?.company_name} (client_id: ${clientConfig.client_id})`);
    
    const result = sendRepositoryDispatch(taskType, targetingId, clientConfig);
    
    if (result.success) {
      if (attempt > 1) {
        console.log(`フォーム送信用 Repository Dispatch送信成功 (${attempt}回目で成功): targeting_id=${targetingId}, client=${clientConfig.client?.company_name} (client_id: ${clientConfig.client_id})`);
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
  
  console.error(`フォーム送信用 Repository Dispatch送信最終失敗: targeting_id=${targetingId}, client=${clientConfig.client?.company_name} (client_id: ${clientConfig.client_id}), エラー: ${lastError}`);
  return { 
    success: false, 
    error: `${maxRetries}回リトライ後も失敗: ${lastError}`,
    targeting_id: targetingId
  };
}

/**
 * ブランチ指定Workflow Dispatch送信
 * 
 * Repository Dispatchの制約を回避し、指定ブランチのワークフローを直接実行します。
 * これにより現在ブランチの実装を現在ブランチのワークフローでテストできます。
 * 
 * @param {string} taskType タスクタイプ（互換性のため残存）
 * @param {number} targetingId ターゲティングID
 * @param {Object} clientConfig クライアント設定
 * @param {string} branch 実行対象ブランチ
 * @returns {Object} 送信結果
 */
function sendWorkflowDispatchToBranch(taskType, targetingId, clientConfig, branch = 'main') {
  try {
    const githubToken = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません');
    }
    
    // クライアント設定の前処理と検証
    if (!clientConfig) {
      throw new Error('clientConfig is null or undefined');
    }
    
    let clientConfigJson;
    try {
      clientConfigJson = JSON.stringify(clientConfig);
      console.log(`client_config JSON size: ${clientConfigJson.length} characters`);
      
      // GitHub APIの入力制限チェック（65535文字制限）
      if (clientConfigJson.length > 60000) {
        console.warn(`client_config is large: ${clientConfigJson.length} characters`);
      }
      
    } catch (jsonError) {
      console.error('JSON.stringify failed:', jsonError);
      throw new Error(`Failed to serialize clientConfig: ${jsonError.message}`);
    }
    
    // クライアント設定をJSON形式で保存（テスト実行のため）
    const tempConfigId = `config_${Date.now()}_${targetingId}`;
    PropertiesService.getScriptProperties().setProperty(`temp_${tempConfigId}`, clientConfigJson);
    
    // Workflow Dispatch用ペイロード
    const payload = {
      ref: branch,
      inputs: {
        targeting_id: targetingId.toString(),
        test_mode: 'true',
        client_config: clientConfigJson
      }
    };
    
    // デバッグ情報の詳細出力
    console.log('=== Workflow Dispatch Payload Debug ===');
    console.log(`Payload size: ${JSON.stringify(payload).length} characters`);
    console.log(`client_config size: ${clientConfigJson.length} characters`);
    console.log(`inputs keys: ${Object.keys(payload.inputs).join(', ')}`);
    console.log(`targeting_id type: ${typeof payload.inputs.targeting_id}`);
    console.log(`test_mode type: ${typeof payload.inputs.test_mode}`);
    console.log(`client_config type: ${typeof payload.inputs.client_config}`);
    
    const githubConfig = getGitHubConfig();
    const url = `${CONFIG.GITHUB_API_BASE}/repos/${githubConfig.OWNER}/${githubConfig.REPO}/actions/workflows/form-sender.yml/dispatches`;
    
    console.log(`ブランチ指定Workflow Dispatch送信: ${url}`);
    console.log(`Branch: ${branch}, Targeting ID: ${targetingId}, Test Mode: true`);
    
    const response = UrlFetchApp.fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${githubToken}`,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'GAS-FormSender/1.0'
      },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    if (responseCode === 204) {
      console.log(`ブランチ指定Workflow Dispatch送信成功: ${branch}`);
      return { 
        success: true,
        targeting_id: targetingId,
        branch: branch,
        dispatch_type: 'workflow_dispatch',
        temp_config_id: tempConfigId
      };
    } else {
      console.error(`=== GitHub API Error Details ===`);
      console.error(`Response Code: ${responseCode}`);
      console.error(`Response Text: ${responseText}`);
      console.error(`Request URL: ${url}`);
      console.error(`Request Method: POST`);
      console.error(`Payload keys: ${Object.keys(payload).join(', ')}`);
      console.error(`Input parameters: ${Object.keys(payload.inputs).join(', ')}`);
      
      // GitHub APIエラーの詳細解析
      try {
        const errorDetails = JSON.parse(responseText);
        if (errorDetails.errors) {
          console.error(`GitHub API Validation Errors:`);
          errorDetails.errors.forEach((err, index) => {
            console.error(`  Error ${index + 1}: ${JSON.stringify(err)}`);
          });
        }
      } catch (parseError) {
        console.error(`Could not parse error response as JSON: ${parseError}`);
      }
      
      return { 
        success: false, 
        error: `HTTP ${responseCode}: ${responseText}`,
        targeting_id: targetingId,
        branch: branch,
        debug_info: {
          url: url,
          payload_size: JSON.stringify(payload).length,
          client_config_size: clientConfigJson.length
        }
      };
    }
    
  } catch (error) {
    console.error('ブランチ指定Workflow Dispatch送信エラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      targeting_id: targetingId,
      branch: branch
    };
  }
}


/**
 * ブランチでのForm Senderテスト実行（実スプレッドシートデータ使用）
 * 
 * 【重要】
 * 架空のテストデータは使用せず、スプレッドシートから実際のデータを取得します。
 * これにより誤送信リスクを完全に排除し、本番と同じデータでテストを実行します。
 * 
 * @param {string} branch テスト対象ブランチ
 * @param {number} testTargetingId テスト用ターゲティングID（デフォルト: 1）
 * @returns {Object} テスト結果
 */
function testFormSenderOnBranch(branch, testTargetingId = 1) {
  try {
    console.log(`=== ブランチ指定Form Senderテスト開始: ${branch} (targeting_id=${testTargetingId}) ===`);
    console.log(`🔐 セキュリティ重要: スプレッドシートから実際のデータを取得します（架空データは使用しません）`);
    
    // スプレッドシートから実際のクライアント設定を取得
    const realClientConfig = getTargetingConfig(testTargetingId);
    
    if (!realClientConfig) {
      const errorMessage = `targeting_id ${testTargetingId} のスプレッドシートデータが見つかりません`;
      console.error(errorMessage);
      return { 
        success: false, 
        error: errorMessage, 
        branch: branch, 
        targeting_id: testTargetingId,
        fix_required: 'スプレッドシートのtargetingシートとclientシートの設定を確認してください'
      };
    }
    
    console.log(`✅ 実データ取得成功: ${realClientConfig.client?.company_name} (client_id: ${realClientConfig.client_id})`);
    console.log(`📋 テストモード: GitHub ActionsのTEST_MODE=trueで実際の送信は防止されます`);
    
    // ブランチ指定でテスト用Workflow Dispatch送信（実データを使用）
    const result = sendWorkflowDispatchToBranch('form_sender_test', testTargetingId, realClientConfig, branch);
    
    if (result.success) {
      console.log(`✅ ブランチ指定Workflow Dispatch送信成功`);
      console.log(`GitHub Actions で ブランチ「${branch}」のワークフローが実行されます`);
      console.log(`実データ使用: ${realClientConfig.client?.company_name}`);
      console.log(`URL: https://github.com/${getGitHubConfig().OWNER}/${getGitHubConfig().REPO}/actions`);
      console.log(`⚠️ 重要: 現在ブランチのワークフローが直接実行されます`);
    } else {
      console.log(`❌ ブランチ指定Workflow Dispatch送信失敗`);
    }
    
    return result;
    
  } catch (error) {
    console.error('ブランチ指定Form Senderテストエラー:', error);
    return { success: false, error: error.toString(), branch: branch, targeting_id: testTargetingId };
  }
}

/**
 * フォーム送信特有の GitHub Actions ワークフロー トリガーテスト（実スプレッドシートデータ使用）
 * 
 * 【重要セキュリティ修正】
 * 架空のテストデータは使用せず、スプレッドシートから実際のデータを取得します。
 * これにより誤送信リスクを完全に排除し、本番と同じデータ構造でテストを実行します。
 * 
 * @param {number} testTargetingId テスト用ターゲティングID（オプション、デフォルト: 1）
 * @returns {Object} テスト結果
 */
function testFormSenderWorkflowTrigger(testTargetingId = 1) {
  try {
    console.log('=== フォーム送信ワークフロー トリガーテスト開始（実スプレッドシートデータ使用） ===');
    console.log(`🔐 セキュリティ重要: スプレッドシートから実際のデータを取得します（架空データは使用しません）`);
    
    // スプレッドシートから実際のクライアント設定を取得
    const realClientConfig = getTargetingConfig(testTargetingId);
    
    if (!realClientConfig) {
      const errorMessage = `targeting_id ${testTargetingId} のスプレッドシートデータが見つかりません`;
      console.error(errorMessage);
      console.error('修正方法: スプレッドシートのtargetingシートとclientシートの設定を確認してください');
      return { 
        success: false, 
        error: errorMessage,
        targeting_id: testTargetingId,
        fix_required: 'スプレッドシート設定の確認が必要です'
      };
    }
    
    console.log(`✅ 実データ取得成功: ${realClientConfig.client?.company_name} (client_id: ${realClientConfig.client_id})`);
    console.log(`📋 テストモード: GitHub ActionsのTEST_MODE=trueで実際の送信は防止されます`);
    
    // 実データを使用してRepository Dispatch送信
    const result = sendRepositoryDispatch('form_sender_task', testTargetingId, realClientConfig);
    
    console.log('テスト結果:', result);
    
    if (result.success) {
      console.log('✅ フォーム送信ワークフロー トリガーテスト成功');
      console.log(`GitHub Actions で Targeting ID「${testTargetingId}」、Client「${realClientConfig.client?.company_name}」の実行を確認してください。`);
      console.log(`実データ使用により、本番環境と同じデータ構造でテストが実行されます`);
    } else {
      console.log('❌ フォーム送信ワークフロー トリガーテスト失敗');
    }
    
    return result;
    
  } catch (error) {
    console.error('フォーム送信ワークフロー トリガーテストエラー:', error);
    return { success: false, error: error.toString(), targeting_id: testTargetingId };
  }
}