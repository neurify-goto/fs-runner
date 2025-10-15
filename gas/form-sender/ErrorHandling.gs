/**
 * エラー分類ユーティリティ
 */

function getErrorType(errorMessage) {
  const message = errorMessage.toLowerCase();

  if (message.includes('spreadsheet') || message.includes('スプレッドシート') ||
      message.includes('シート') || message.includes('が見つかりません') ||
      message.includes('必須カラム') || message.includes('ヘッダー') ||
      message.includes('データがありません')) {
    return 'SPREADSHEET_CONFIG_ERROR';
  }

  if (message.includes('github') || message.includes('dispatch') ||
      message.includes('repository') || message.includes('authorization') ||
      message.includes('token') || message.includes('rate limit') ||
      message.includes('http 404') || message.includes('http 403')) {
    return 'GITHUB_API_ERROR';
  }

  if (message.includes('targeting_id') || message.includes('ターゲティング') ||
      message.includes('client_id') || message.includes('無効な値') ||
      message.includes('設定が見つからない')) {
    return 'TARGETING_CONFIG_ERROR';
  }

  if (message.includes('company_name') || message.includes('form_sender_name') ||
      message.includes('基本的なクライアント情報') || message.includes('結合') ||
      message.includes('client_config')) {
    return 'CLIENT_DATA_ERROR';
  }

  if (message.includes('json') || message.includes('parse') ||
      message.includes('解析') || message.includes('invalid json') ||
      message.includes('send_days_of_week')) {
    return 'JSON_PARSE_ERROR';
  }

  if (message.includes('営業時間') || message.includes('営業日') ||
      message.includes('実行時間') || message.includes('時間外') ||
      message.includes('営業時間外')) {
    return 'BUSINESS_HOURS_ERROR';
  }

  if (message.includes('network') || message.includes('timeout') ||
      message.includes('connection') || message.includes('ネットワーク') ||
      message.includes('接続') || message.includes('fetch')) {
    return 'NETWORK_ERROR';
  }

  if (message.includes('permission') || message.includes('権限') ||
      message.includes('unauthorized') || message.includes('forbidden') ||
      message.includes('認証')) {
    return 'PERMISSION_ERROR';
  }

  return 'SYSTEM_ERROR';
}
