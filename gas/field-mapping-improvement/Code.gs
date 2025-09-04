/**
 * Field Mapping Improvement 用 GAS エントリポイント
 * - .github/workflows/field-mapping-improvement.yml を repository_dispatch で起動
 * - 時間ベーストリガー（任意）を用意
 *
 * セキュリティ:
 * - GitHub トークンは Script Properties の `GITHUB_TOKEN` に保存してください（コードへ直書き禁止）
 * - リポジトリは `GITHUB_OWNER`, `GITHUB_REPO`（任意）で上書き可能
 *
 * 期待する GitHub Actions 側のイベント:
 *   repository_dispatch.types: [field_mapping_improvement_task]
 */

// モジュール固有の設定（他モジュールと衝突しないようプレフィックスを付与）
const FM_CONFIG = {
  GITHUB_API_BASE: 'https://api.github.com',
  EVENT_TYPE: 'field_mapping_improvement_task',
  TIMEOUT_MS: 60000,
  MAX_RETRIES: 3,
  RETRY_DELAY_MS: 1500,
  // デフォルトの実行時刻（JST）: 03:15
  TRIGGER_HOUR_JST: 3,
  TRIGGER_MINUTE_JST: 15
};

/**
 * GitHub リポジトリ設定を取得（Script Properties から上書き可）
 */
function getGitHubConfigFM() {
  const props = PropertiesService.getScriptProperties();
  const owner = props.getProperty('GITHUB_OWNER') || 'neurify-goto';
  // デフォルトは本リポジトリ名に合わせる（必要に応じて Script Properties で上書き）
  const repo = props.getProperty('GITHUB_REPO') || 'fs-runner';
  return { OWNER: owner, REPO: repo };
}

/**
 * マスク用ユーティリティ（ログに秘匿情報を出さない）
 */
function mask(value, visible = 4) {
  if (!value || typeof value !== 'string') return '';
  const head = value.slice(0, Math.min(visible, value.length));
  return `${head}${'*'.repeat(Math.max(0, value.length - head.length))}`;
}

/**
 * 手動実行用：即時にワークフローを起動
 */
function dispatchFieldMappingImprovementNow() {
  const result = dispatchFieldMappingImprovementWithRetry();
  console.log('dispatch result:', JSON.stringify(result));
  return result;
}

/**
 * 時間ベーストリガーから呼び出すエントリポイント
 */
function startFieldMappingImprovementFromTrigger() {
  console.log('Field Mapping Improvement: 時間ベーストリガー起動');
  return dispatchFieldMappingImprovementWithRetry();
}

/**
 * repository_dispatch を送信（リトライ付き）
 */
function dispatchFieldMappingImprovementWithRetry(maxRetries = FM_CONFIG.MAX_RETRIES) {
  let lastError = null;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    const result = sendRepositoryDispatchFM();
    if (result.success) {
      if (attempt > 1) {
        console.log(`Dispatch 成功（${attempt}回目で成功）`);
      }
      return result;
    }
    lastError = result.error || 'unknown error';
    if (attempt < maxRetries) {
      const backoff = FM_CONFIG.RETRY_DELAY_MS * attempt; // 線形バックオフ
      console.warn(`Dispatch 失敗（試行 ${attempt}/${maxRetries}）: ${lastError} -> ${backoff}ms 待機して再試行`);
      Utilities.sleep(backoff);
    }
  }
  console.error(`Dispatch 最終失敗: ${lastError}`);
  return { success: false, error: lastError };
}

/**
 * GitHub Repository Dispatch を送信
 */
function sendRepositoryDispatchFM() {
  try {
    const props = PropertiesService.getScriptProperties();
    const githubToken = props.getProperty('GITHUB_TOKEN');
    if (!githubToken) {
      throw new Error('GITHUB_TOKEN が設定されていません（Script Properties）');
    }

    const repoCfg = getGitHubConfigFM();
    const url = `${FM_CONFIG.GITHUB_API_BASE}/repos/${repoCfg.OWNER}/${repoCfg.REPO}/dispatches`;

    // GAS 側では機密情報をログに出さない
    console.log(`Dispatch 送信先: owner=${mask(repoCfg.OWNER)}, repo=${mask(repoCfg.REPO)}`);

    // 参考: 現行ワークフローは client_payload を必須としていないが、
    // トレース容易化のため最小限のメタ情報を付与
    const timestampJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', "yyyy-MM-dd'T'HH:mm:ssXXX");
    const body = {
      event_type: FM_CONFIG.EVENT_TYPE,
      client_payload: {
        initiator: 'gas',
        module: 'field-mapping-improvement',
        triggered_at_jst: timestampJst
      }
    };

    const res = UrlFetchApp.fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${githubToken}`,
        Accept: 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'User-Agent': 'GAS-FieldMappingImprovement/1.0'
      },
      payload: JSON.stringify(body),
      muteHttpExceptions: true,
      // GAS の UrlFetchApp は `timeout`（ms）指定に対応
      timeout: FM_CONFIG.TIMEOUT_MS
    });

    const code = res.getResponseCode();
    const text = res.getContentText();

    if (code === 204) {
      console.log('Repository Dispatch 送信成功');
      return { success: true };
    }

    // エラー時詳細
    console.error(`Repository Dispatch 送信失敗: HTTP ${code} - ${text}`);
    return { success: false, error: `HTTP ${code}: ${text}` };
  } catch (e) {
    console.error('Repository Dispatch 送信エラー:', e);
    return { success: false, error: e && e.toString ? e.toString() : 'unknown error' };
  }
}

/**
 * 1日1回の時間ベーストリガーを作成（JST 指定）
 * - handler: startFieldMappingImprovementFromTrigger
 */
function setupDailyTriggerForFieldMappingImprovement() {
  // 既存の同一ハンドラトリガーを削除して重複回避
  const triggers = ScriptApp.getProjectTriggers();
  triggers
    .filter(t => t.getHandlerFunction() === 'startFieldMappingImprovementFromTrigger')
    .forEach(t => ScriptApp.deleteTrigger(t));

  ScriptApp.newTrigger('startFieldMappingImprovementFromTrigger')
    .timeBased()
    .atHour(FM_CONFIG.TRIGGER_HOUR_JST) // JST での時刻指定
    .nearMinute(FM_CONFIG.TRIGGER_MINUTE_JST)
    .everyDays(1)
    .inTimezone('Asia/Tokyo')
    .create();

  console.log(`時間ベーストリガー作成: JST ${FM_CONFIG.TRIGGER_HOUR_JST}:${('0' + FM_CONFIG.TRIGGER_MINUTE_JST).slice(-2)}`);
}

/**
 * 当該モジュールの時間ベーストリガーを削除
 */
function deleteFieldMappingImprovementTriggers() {
  const triggers = ScriptApp.getProjectTriggers();
  const targets = triggers.filter(t => t.getHandlerFunction() === 'startFieldMappingImprovementFromTrigger');
  targets.forEach(t => ScriptApp.deleteTrigger(t));
  console.log(`削除したトリガー数: ${targets.length}`);
}

/**
 * GitHub API 接続テスト（/user）
 */
function testGitHubConnectionForFieldMappingImprovement() {
  try {
    const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
    if (!token) {
      return { success: false, error: 'GITHUB_TOKEN が設定されていません' };
    }
    const res = UrlFetchApp.fetch(`${FM_CONFIG.GITHUB_API_BASE}/user`, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'User-Agent': 'GAS-FieldMappingImprovement/1.0'
      },
      muteHttpExceptions: true,
      timeout: FM_CONFIG.TIMEOUT_MS
    });
    const code = res.getResponseCode();
    if (code === 200) {
      const user = JSON.parse(res.getContentText());
      console.log(`GitHub 接続 OK: ${user.login}`);
      return { success: true, user: user.login };
    }
    return { success: false, error: `HTTP ${code}: ${res.getContentText()}` };
  } catch (e) {
    return { success: false, error: e && e.toString ? e.toString() : 'unknown error' };
  }
}
