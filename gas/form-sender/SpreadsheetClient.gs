/**
 * スプレッドシート連携モジュール（フォーム送信システム用）
 * GAS用スプレッドシートクライアント実装
 * 
 * FORM_SENDER.md の仕様に基づく実装:
 * - 2シート構成（client + targeting）
 * - targeting-id ごとのアクティブ状態管理
 * - client_idによるリレーショナル結合
 * - プレースホルダ変数システム対応
 */

/**
 * スプレッドシート設定を取得
 * @returns {Object} スプレッドシート設定
 */
function getSpreadsheetConfig() {
  const spreadsheetId = PropertiesService.getScriptProperties().getProperty('FORM_SENDER_SPREADSHEET_ID');
  
  if (!spreadsheetId) {
    throw new Error('スプレッドシート設定が不正です: FORM_SENDER_SPREADSHEET_ID が設定されていません');
  }
  
  return {
    spreadsheetId: spreadsheetId,
    clientSheetName: 'client',
    targetingSheetName: 'targeting'
  };
}

const DEFAULT_SESSION_HOURS_FALLBACK = 8;
const DEFAULT_BUSINESS_END_TIME_FALLBACK = '18:00';

function getScriptPropertyNumber_(key, fallback) {
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
    console.warn(`getScriptPropertyNumber_(${key}) error: ${e && e.message ? e.message : e}`);
    return fallback;
  }
}

function getScriptPropertyString_(key, fallback) {
  try {
    const props = PropertiesService.getScriptProperties();
    const raw = props.getProperty(key);
    if (!raw || String(raw).trim() === '') {
      return fallback;
    }
    return String(raw).trim();
  } catch (e) {
    console.warn(`getScriptPropertyString_(${key}) error: ${e && e.message ? e.message : e}`);
    return fallback;
  }
}

function normalizeTimeString_(timeString) {
  if (!timeString || typeof timeString !== 'string') {
    return null;
  }
  const trimmed = timeString.trim();
  const match = trimmed.match(/^(\d{1,2}):(\d{2})$/);
  if (!match) {
    return null;
  }
  const hours = parseInt(match[1], 10);
  const mins = parseInt(match[2], 10);
  if (!isFinite(hours) || !isFinite(mins) || hours < 0 || hours > 23 || mins < 0 || mins > 59) {
    return null;
  }
  return `${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}`;
}

function resolveSessionMaxHours_(rawValue) {
  const fromRow = (function(value) {
    if (value === null || typeof value === 'undefined') {
      return null;
    }
    if (typeof value === 'number') {
      return value;
    }
    if (typeof value === 'string') {
      const trimmed = value.trim();
      if (trimmed === '') {
        return null;
      }
      const parsed = parseFloat(trimmed);
      if (!isFinite(parsed)) {
        return null;
      }
      return parsed;
    }
    return null;
  })(rawValue);

  if (isFinite(fromRow) && fromRow > 0) {
    return fromRow;
  }

  const fromProps = getScriptPropertyNumber_('FORM_SENDER_MAX_SESSION_HOURS_DEFAULT', null);
  if (isFinite(fromProps) && fromProps > 0) {
    return fromProps;
  }

  if (typeof CONFIG !== 'undefined' && CONFIG && typeof CONFIG.MAX_SESSION_DURATION_HOURS === 'number' && CONFIG.MAX_SESSION_DURATION_HOURS > 0) {
    return CONFIG.MAX_SESSION_DURATION_HOURS;
  }

  return DEFAULT_SESSION_HOURS_FALLBACK;
}

function resolveSendEndTime_(rawValue) {
  const fromRow = normalizeTimeString_(typeof rawValue === 'string' ? rawValue : (rawValue !== null && typeof rawValue !== 'undefined' ? String(rawValue) : ''));
  if (fromRow) {
    return fromRow;
  }
  const fromProps = normalizeTimeString_(getScriptPropertyString_('FORM_SENDER_DEFAULT_SEND_END_TIME', ''));
  if (fromProps) {
    return fromProps;
  }
  return normalizeTimeString_(DEFAULT_BUSINESS_END_TIME_FALLBACK) || '18:00';
}

/**
 * アクティブなターゲティング設定をtargetingシートから取得
 * FORM_SENDER.md 1.3.1節準拠の実装
 * @returns {Array} アクティブなターゲティング設定の配列
 */
function getActiveTargetingIdsFromSheet() {
  try {
    console.log('targetingシートからアクティブなターゲティング設定を取得開始');
    
    const config = getSpreadsheetConfig();
    const spreadsheet = SpreadsheetApp.openById(config.spreadsheetId);
    const targetingSheet = spreadsheet.getSheetByName(config.targetingSheetName);
    
    if (!targetingSheet) {
      throw new Error(`targetingシート "${config.targetingSheetName}" が見つかりません`);
    }
    
    // ヘッダー行を含む全データを取得
    const data = targetingSheet.getDataRange().getValues();
    
    if (data.length <= 1) {
      console.log('targetingシートにデータが存在しません（ヘッダー行のみ）');
      return [];
    }
    
    // ヘッダー行を分析してカラムインデックスを取得
    const headers = data[0].map(header => {
      if (header === null || typeof header === 'undefined') return '';
      if (typeof header.toString !== 'function') return '';
      return header.toString().trim().toLowerCase();
    });

    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'targeting_sheet_headers_normalized',
        headers
      }));
    } catch (e) {}
    
    const indexOfHeader = function(name) {
      const normalized = name ? name.trim().toLowerCase() : '';
      const idx = headers.indexOf(normalized);
      return idx;
    };

    let extraIndex = indexOfHeader('extra');
    if (extraIndex === -1) extraIndex = indexOfHeader('use_extra_table');
    if (extraIndex === -1) extraIndex = indexOfHeader('use extra table');

    const colIndexes = {
      active: indexOfHeader('active'),
      id: indexOfHeader('id'),
      client_id: indexOfHeader('client_id'),
      description: indexOfHeader('description'),
      concurrent_workflow: indexOfHeader('concurrent_workflow'),
      extra: extraIndex
    };

    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'targeting_sheet_column_indexes',
        colIndexes
      }));
    } catch (e) {}
    
    // 必須カラムの存在確認
    if (colIndexes.id === -1) {
      throw new Error('必須カラム "id" が見つかりません');
    }
    
    if (colIndexes.active === -1) {
      throw new Error('必須カラム "active" が見つかりません');
    }
    
    if (colIndexes.client_id === -1) {
      throw new Error('必須カラム "client_id" が見つかりません');
    }
    
    console.log(`targetingシートカラム構成: id=${colIndexes.id}, active=${colIndexes.active}, client_id=${colIndexes.client_id}, description=${colIndexes.description}`);
    
    // データ行を処理
    const activeTargetings = [];
    
    for (let i = 1; i < data.length; i++) {
      const row = data[i];
      
      // targeting_id (id) を取得
      const targetingIdValue = row[colIndexes.id];
      if (!targetingIdValue) {
        console.log(`行 ${i + 1}: id が空のためスキップ`);
        continue;
      }
      
      const targetingId = parseInt(targetingIdValue);
      if (isNaN(targetingId) || targetingId <= 0) {
        console.log(`行 ${i + 1}: id が無効な値 (${targetingIdValue}) のためスキップ`);
        continue;
      }
      
      // active 状態を取得
      const activeValue = row[colIndexes.active];
      const isActive = (activeValue === true || activeValue === 'TRUE' || activeValue === 'true' || activeValue === 1);
      
      if (!isActive) {
        console.log(`行 ${i + 1}: targeting_id ${targetingId} は非アクティブのためスキップ`);
        continue;
      }
      
      // client_id を取得
      const clientIdValue = row[colIndexes.client_id];
      const clientId = parseInt(clientIdValue);
      if (isNaN(clientId) || clientId <= 0) {
        console.log(`行 ${i + 1}: targeting_id ${targetingId} の client_id が無効 (${clientIdValue}) のためスキップ`);
        continue;
      }
      
      // 管理用情報を取得
      const description = colIndexes.description >= 0 ? row[colIndexes.description] || '' : '';

      // 並列起動数（未定義/不正値は1）
      let cw = 1;
      try {
        if (colIndexes.concurrent_workflow >= 0) {
          const v = row[colIndexes.concurrent_workflow];
          const n = parseInt(v);
          cw = (isNaN(n) || n <= 0) ? 1 : n;
        }
      } catch (e) { cw = 1; }

      // extraテーブル利用フラグ（TRUE/1/"true" を許可）
      let useExtra = false;
      if (colIndexes.extra >= 0) {
        const extraValue = row[colIndexes.extra];
        if (extraValue === true || extraValue === 1) {
          useExtra = true;
        } else if (typeof extraValue === 'string') {
          const lowered = extraValue.toString().toLowerCase();
          useExtra = lowered === 'true' || lowered === '1';
        }
      }

      try {
        console.log(JSON.stringify({
          level: 'debug',
          event: 'targeting_row_extra_evaluation',
          row_number: i + 1,
          targeting_id: targetingId,
          extra_col_index: colIndexes.extra,
          extra_raw_value: colIndexes.extra >= 0 ? row[colIndexes.extra] : null,
          use_extra_table: useExtra
        }));
      } catch (e) {}

      activeTargetings.push({
        targeting_id: targetingId,
        client_id: clientId,
        description: description,
        row_number: i + 1,
        concurrent_workflow: cw,
        use_extra_table: useExtra
      });
      
      console.log(`行 ${i + 1}: targeting_id ${targetingId} (client_id: ${clientId}) をアクティブとして追加`);
    }
    
    console.log(`targetingシートからアクティブなターゲティング取得完了: ${activeTargetings.length}件`);
    return activeTargetings;
    
  } catch (error) {
    console.error(`targetingシートからの取得エラー: ${error.message}`);
    throw error;
  }
}

/**
 * スプレッドシートの設定状況を確認（2シート構成版）
 * @returns {Object} 設定状況の確認結果
 */
function validateSpreadsheetConfig() {
  try {
    console.log('スプレッドシート設定確認開始（2シート構成）');
    
    const config = getSpreadsheetConfig();
    console.log(`スプレッドシートID: ${config.spreadsheetId}`);
    console.log(`clientシート名: ${config.clientSheetName}`);
    console.log(`targetingシート名: ${config.targetingSheetName}`);
    
    // スプレッドシートにアクセス
    const spreadsheet = SpreadsheetApp.openById(config.spreadsheetId);
    console.log(`スプレッドシート名: ${spreadsheet.getName()}`);
    
    const availableSheets = spreadsheet.getSheets().map(s => s.getName());
    console.log(`利用可能シート: ${availableSheets.join(', ')}`);
    
    // clientシート確認
    const clientSheet = spreadsheet.getSheetByName(config.clientSheetName);
    if (!clientSheet) {
      return {
        success: false,
        error: `clientシート "${config.clientSheetName}" が見つかりません`,
        spreadsheet_name: spreadsheet.getName(),
        available_sheets: availableSheets
      };
    }
    
    // targetingシート確認
    const targetingSheet = spreadsheet.getSheetByName(config.targetingSheetName);
    if (!targetingSheet) {
      return {
        success: false,
        error: `targetingシート "${config.targetingSheetName}" が見つかりません`,
        spreadsheet_name: spreadsheet.getName(),
        available_sheets: availableSheets
      };
    }
    
    console.log(`2シート確認OK: ${clientSheet.getName()}, ${targetingSheet.getName()}`);
    
    // clientシートのヘッダー行確認
    const clientData = clientSheet.getDataRange().getValues();
    if (clientData.length === 0) {
      return {
        success: false,
        error: 'clientシートにデータがありません',
        spreadsheet_name: spreadsheet.getName(),
        sheet_name: clientSheet.getName()
      };
    }
    
    const clientHeaders = clientData[0].map(header => {
      if (header === null || typeof header === 'undefined') return '';
      if (typeof header.toString !== 'function') return '';
      return header.toString().trim().toLowerCase();
    });
    console.log(`clientシートヘッダー行: ${clientHeaders.join(', ')}`);
    
    // targetingシートのヘッダー行確認
    const targetingData = targetingSheet.getDataRange().getValues();
    if (targetingData.length === 0) {
      return {
        success: false,
        error: 'targetingシートにデータがありません',
        spreadsheet_name: spreadsheet.getName(),
        sheet_name: targetingSheet.getName()
      };
    }
    
    const targetingHeaders = targetingData[0].map(header => {
      if (header === null || typeof header === 'undefined') return '';
      if (typeof header.toString !== 'function') return '';
      return header.toString().trim().toLowerCase();
    });
    console.log(`targetingシートヘッダー行: ${targetingHeaders.join(', ')}`);
    
    // clientシートの必須カラム確認（FORM_SENDER.md 1.3.2節準拠）
    const clientRequiredColumns = [
      'id', 'company_name', 'company_name_kana', 'form_sender_name',
      'last_name', 'first_name', 'last_name_kana', 'first_name_kana',
      'last_name_hiragana', 'first_name_hiragana', 'position', 'department',
      'gender', 'email_1', 'email_2', 'website_url',
      'postal_code_1', 'postal_code_2', 'address_1', 'address_2', 'address_3',
      'phone_1', 'phone_2', 'phone_3'
    ];
    const clientMissingColumns = clientRequiredColumns.filter(col => !clientHeaders.includes(col));
    
    if (clientMissingColumns.length > 0) {
      return {
        success: false,
        error: `clientシートの必須カラムが不足しています: ${clientMissingColumns.join(', ')}`,
        spreadsheet_name: spreadsheet.getName(),
        sheet_name: clientSheet.getName(),
        headers: clientHeaders,
        missing_columns: clientMissingColumns
      };
    }
    
    // targetingシートの必須カラム確認（FORM_SENDER.md 1.3.1節準拠）
    // targeting_sql, ng_companiesは空文字許可のためカラム確認のみ
    const targetingRequiredColumns = [
      'id', 'active', 'client_id', 'description', 'subject', 'message',
      'targeting_sql', 'ng_companies', 'max_daily_sends',
      'send_start_time', 'send_end_time', 'send_days_of_week'
      // 'concurrent_workflow' はオプショナル（空/未定義なら 1 として扱う）
    ];
    const targetingMissingColumns = targetingRequiredColumns.filter(col => !targetingHeaders.includes(col));
    
    if (targetingMissingColumns.length > 0) {
      return {
        success: false,
        error: `targetingシートの必須カラムが不足しています: ${targetingMissingColumns.join(', ')}`,
        spreadsheet_name: spreadsheet.getName(),
        sheet_name: targetingSheet.getName(),
        headers: targetingHeaders,
        missing_columns: targetingMissingColumns
      };
    }
    
    // データ行数確認
    const clientDataRowCount = clientData.length - 1; // ヘッダー行を除く
    const targetingDataRowCount = targetingData.length - 1; // ヘッダー行を除く
    console.log(`clientシートデータ行数: ${clientDataRowCount}行`);
    console.log(`targetingシートデータ行数: ${targetingDataRowCount}行`);
    
    return {
      success: true,
      message: 'スプレッドシート2シート構成確認完了',
      spreadsheet_name: spreadsheet.getName(),
      client_sheet: {
        name: clientSheet.getName(),
        headers: clientHeaders,
        data_row_count: clientDataRowCount
      },
      targeting_sheet: {
        name: targetingSheet.getName(),
        headers: targetingHeaders,
        data_row_count: targetingDataRowCount
      }
    };
    
  } catch (error) {
    console.error(`スプレッドシート設定確認エラー: ${error.message}`);
    return {
      success: false,
      error: error.message
    };
  }
}

/**
 * スプレッドシートの内容をすべて表示（デバッグ用・2シート構成版）
 * @returns {Object} スプレッドシートの内容
 */
function showSpreadsheetContents() {
  try {
    const config = getSpreadsheetConfig();
    const spreadsheet = SpreadsheetApp.openById(config.spreadsheetId);
    
    // clientシート内容表示
    const clientSheet = spreadsheet.getSheetByName(config.clientSheetName);
    let clientData = null;
    if (clientSheet) {
      clientData = clientSheet.getDataRange().getValues();
      console.log('=== clientシート内容 ===');
      clientData.forEach((row, index) => {
        console.log(`行 ${index + 1}: ${row.join(' | ')}`);
      });
      console.log('=======================');
    }
    
    // targetingシート内容表示
    const targetingSheet = spreadsheet.getSheetByName(config.targetingSheetName);
    let targetingData = null;
    if (targetingSheet) {
      targetingData = targetingSheet.getDataRange().getValues();
      console.log('=== targetingシート内容 ===');
      targetingData.forEach((row, index) => {
        console.log(`行 ${index + 1}: ${row.join(' | ')}`);
      });
      console.log('==========================');
    }
    
    if (!clientSheet && !targetingSheet) {
      return { 
        success: false, 
        error: `clientシート "${config.clientSheetName}" とtargetingシート "${config.targetingSheetName}" の両方が見つかりません` 
      };
    }
    
    return {
      success: true,
      spreadsheet_name: spreadsheet.getName(),
      client_sheet: clientSheet ? {
        name: clientSheet.getName(),
        data: clientData
      } : null,
      targeting_sheet: targetingSheet ? {
        name: targetingSheet.getName(),
        data: targetingData
      } : null
    };
    
  } catch (error) {
    console.error(`スプレッドシート内容表示エラー: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * スプレッドシート設定のテンプレート情報を取得（FORM_SENDER.md 1.3.1節準拠の2シート構成）
 * @returns {Object} テンプレート情報
 */
function getSpreadsheetTemplate() {
  return {
    client_sheet: {
      headers: [
        'id', 'project_name', 'company_name', 'company_name_kana', 
        'form_sender_name', 'last_name', 'first_name', 
        'last_name_kana', 'first_name_kana', 'last_name_hiragana', 'first_name_hiragana',
        'position', 'department', 'gender', 'email_1', 'email_2', 'website_url',
        'postal_code_1', 'postal_code_2', 'address_1', 'address_2', 'address_3', 'address_4', 'address_5',
        'phone_1', 'phone_2', 'phone_3'
      ],
      sample_data: [
        [
          1, 'サンプルプロジェクト', '株式会社サンプル', 'カブシキガイシャサンプル',
          '田中太郎', '田中', '太郎', 'タナカ', 'タロウ', 'たなか', 'たろう',
          '営業部長', '営業部', 'male', 'tanaka', 'sample.com', 'https://sample.co.jp',
          '100', '0001', '東京都', '千代田区', '神田', '1-1-1', 'サンプルビル5F',
          '03', '1234', '5678'
        ]
      ]
    },
    targeting_sheet: {
      headers: [
        'active', 'id', 'client_id', 'description', 'subject', 'message',
        'targeting_sql', 'ng_companies', 'max_daily_sends', 'send_start_time', 'send_end_time', 'send_days_of_week'
      ],
      sample_data: [
        [
          true, 1, 1, 'IT企業向け新規営業', 'お問い合わせの件', 'お世話になっております。{client.form_sender_name}と申します。...',
          'WHERE industry = \'IT\'', '除外企業リスト', 100, '09:00', '18:00', '[0,1,2,3,4]'
        ]
      ]
    },
    notes: [
      '=== clientシート（FORM_SENDER.md 1.3.2節準拠） ===',
      'id: 数値型、1以上の整数（クライアントID）',
      'company_name: 会社名（{client.company_name}プレースホルダ対応）',
      'company_name_kana: カタカナ会社名（{client.company_name_kana}）',
      'form_sender_name: 送信者名（{client.form_sender_name}プレースホルダ対応）',
      'last_name/first_name: 姓名（{client.last_name}/{client.first_name}）',
      'last_name_kana/first_name_kana: カタカナ姓名（{client.last_name_kana}/{client.first_name_kana}）',
      'last_name_hiragana/first_name_hiragana: ひらがな姓名（{client.last_name_hiragana}/{client.first_name_hiragana}）',
      'position: 役職（{client.position}）、department: 部署（{client.department}）',
      'gender: 性別（{client.gender}）',
      'email_1/email_2: メールアドレスを@で分割して格納（{client.email_1}@{client.email_2}）',
      'website_url: WebサイトURL（{client.website_url}）',
      'phone_1/phone_2/phone_3: 電話番号を-で分割して格納（{client.phone_1}-{client.phone_2}-{client.phone_3}）',
      'postal_code_1/postal_code_2: 郵便番号を-で分割して格納（{client.postal_code_1}-{client.postal_code_2}）',
      'address_1-5: 住所を都道府県/市区町村/町名/番地/建物名に分割して格納',
      '',
      '=== targetingシート（FORM_SENDER.md 1.3.1節準拠） ===',
      'id: 数値型、1以上の整数（ターゲティングID）',
      'active: 真偽値（TRUE/FALSE）、アクティブ状態制御',
      'client_id: 数値型、clientシートのidとリンク（リレーショナル結合用）',
      'description: ターゲティング設定の説明',
      'subject: {targeting.subject}プレースホルダ対応の件名',
      'message: {targeting.message}プレースホルダ対応のメッセージ本文',
      'targeting_sql: 企業抽出SQL条件（FORM_SENDER.md 1.4.1節準拠）',
      'ng_companies: 除外企業リスト（カンマ区切り、FORM_SENDER.md 1.4.2節準拠）',
      'max_daily_sends: 日次送信上限数（FORM_SENDER.md 2.3.3節準拠）',
      'send_start_time/send_end_time: 営業時間（HH:MM形式）',
      'send_days_of_week: 送信曜日（JSON配列形式、0=月曜〜6=日曜）'
    ]
  };
}

/**
 * 指定したtargeting_idの完全なクライアント設定を取得（2シート構成版）
 * FORM_SENDER.md 1.3.1節準拠: targetingシート + clientシートのリレーショナル結合
 * @param {number} targetingId ターゲティングID
 * @returns {Object} 完全なクライアント設定データ
 */
function getTargetingConfig(targetingId) {
  try {
    console.log(`targeting_id ${targetingId} の完全設定を取得開始（2シート構成）`);
    
    const config = getSpreadsheetConfig();
    const spreadsheet = SpreadsheetApp.openById(config.spreadsheetId);
    
    // targetingシートからターゲティング設定を取得
    const targetingSheet = spreadsheet.getSheetByName(config.targetingSheetName);
    if (!targetingSheet) {
      throw new Error(`targetingシート "${config.targetingSheetName}" が見つかりません`);
    }
    
    const targetingData = targetingSheet.getDataRange().getValues();
    if (targetingData.length <= 1) {
      console.log('targetingシートにデータが存在しません（ヘッダー行のみ）');
      return null;
    }
    
    // targetingシートのヘッダー解析
    const targetingHeaders = targetingData[0].map(header => {
      if (header === null || typeof header === 'undefined') return '';
      if (typeof header.toString !== 'function') return '';
      return header.toString().trim().toLowerCase();
    });
    const targetingColMap = {};
    targetingHeaders.forEach((header, index) => {
      targetingColMap[header] = index;
    });

    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'targeting_config_headers',
        targeting_id: targetingId,
        headers: targetingHeaders
      }));
    } catch (e) {}

    if (typeof targetingColMap['extra'] !== 'number') {
      if (typeof targetingColMap['use_extra_table'] === 'number') {
        targetingColMap['extra'] = targetingColMap['use_extra_table'];
      } else if (typeof targetingColMap['use extra table'] === 'number') {
        targetingColMap['extra'] = targetingColMap['use extra table'];
      }
    }

    if (typeof targetingColMap['use_serverless'] !== 'number') {
      if (typeof targetingColMap['useserverless'] === 'number') {
        targetingColMap['use_serverless'] = targetingColMap['useserverless'];
      } else if (typeof targetingColMap['use serverless'] === 'number') {
        targetingColMap['use_serverless'] = targetingColMap['use serverless'];
      }
    }

    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'targeting_config_column_map',
        targeting_id: targetingId,
        column_map: targetingColMap
      }));
    } catch (e) {}

    function resolveColumnIndex(map, aliases) {
      if (!Array.isArray(aliases)) {
        return typeof map[aliases] === 'number' ? map[aliases] : -1;
      }
      for (var i = 0; i < aliases.length; i++) {
        var key = aliases[i];
        if (typeof map[key] === 'number') {
          return map[key];
        }
      }
      return -1;
    }

    function normalizeBoolean(value) {
      if (value === true || value === 1) {
        return true;
      }
      if (value === false || value === 0) {
        return false;
      }
      if (typeof value === 'string') {
        var lowered = value.trim().toLowerCase();
        if (lowered === 'true' || lowered === '1' || lowered === 'yes' || lowered === 'on') {
          return true;
        }
        if (lowered === 'false' || lowered === '0' || lowered === 'no' || lowered === 'off') {
          return false;
        }
      }
      return false;
    }

    function parseOptionalBoolean(value) {
      if (value === null || typeof value === 'undefined') {
        return undefined;
      }
      if (typeof value === 'string') {
        var trimmed = value.trim();
        if (trimmed === '') {
          return undefined;
        }
      }
      return normalizeBoolean(value);
    }

    function parseOptionalInteger(value) {
      if (value === null || typeof value === 'undefined' || value === '') {
        return null;
      }
      var parsed = parseInt(value, 10);
      if (!isFinite(parsed)) {
        return null;
      }
      return parsed;
    }

    // targeting_idに該当する行を検索
    let targetingRow = null;
    for (let i = 1; i < targetingData.length; i++) {
      const row = targetingData[i];
      const rowTargetingId = parseInt(row[targetingColMap['id']]);
      
      if (rowTargetingId === targetingId) {
        targetingRow = row;
        break;
      }
    }
    
    if (!targetingRow) {
      console.log(`targeting_id ${targetingId} がtargetingシートで見つかりません`);
      return null;
    }
    
    // client_idを取得
    const clientId = parseInt(targetingRow[targetingColMap['client_id']]);
    if (isNaN(clientId) || clientId <= 0) {
      throw new Error(`targeting_id ${targetingId} のclient_idが無効です: ${targetingRow[targetingColMap['client_id']]}`);
    }
    
    console.log(`targeting_id ${targetingId} -> client_id ${clientId} で結合処理開始`);
    
    // clientシートからクライアント設定を取得
    const clientSheet = spreadsheet.getSheetByName(config.clientSheetName);
    if (!clientSheet) {
      throw new Error(`clientシート "${config.clientSheetName}" が見つかりません`);
    }
    
    const clientData = clientSheet.getDataRange().getValues();
    if (clientData.length <= 1) {
      console.log('clientシートにデータが存在しません（ヘッダー行のみ）');
      return null;
    }
    
    // clientシートのヘッダー解析
    const clientHeaders = clientData[0].map(header => {
      if (header === null || typeof header === 'undefined') return '';
      if (typeof header.toString !== 'function') return '';
      return header.toString().trim().toLowerCase();
    });
    const clientColMap = {};
    clientHeaders.forEach((header, index) => {
      clientColMap[header] = index;
    });
    
    // client_idに該当する行を検索
    let clientRow = null;
    for (let i = 1; i < clientData.length; i++) {
      const row = clientData[i];
      const rowClientId = parseInt(row[clientColMap['id']]);
      
      if (rowClientId === clientId) {
        clientRow = row;
        break;
      }
    }
    
    if (!clientRow) {
      throw new Error(`client_id ${clientId} がclientシートで見つかりません`);
    }
    
    console.log(`client_id ${clientId} のクライアント情報取得完了`);

    // extraテーブル利用判定
    const extraColIndex = typeof targetingColMap['extra'] === 'number' ? targetingColMap['extra'] : -1;
    const useExtraTable = (function(value) {
      if (value === true || value === 1) return true;
      if (typeof value === 'string') {
        const lowered = value.toLowerCase();
        return lowered === 'true' || lowered === '1';
      }
      return false;
    })(extraColIndex >= 0 ? targetingRow[extraColIndex] : false);

    const serverlessColIndex = typeof targetingColMap['use_serverless'] === 'number' ? targetingColMap['use_serverless'] : -1;
    const useServerless = serverlessColIndex >= 0 ? parseOptionalBoolean(targetingRow[serverlessColIndex]) : undefined;

    const useBatchColIndex = resolveColumnIndex(targetingColMap, ['use_gcp_batch', 'usegcpbatch', 'use gcp batch', 'usegcpbatch?', 'usegcpbatch', 'usegcpbatch ', 'use gcp_batch']);
    const batchMaxParallelIndex = resolveColumnIndex(targetingColMap, ['batch_max_parallelism', 'batchmaxparallelism']);
    const batchPreferSpotIndex = resolveColumnIndex(targetingColMap, ['batch_prefer_spot', 'batchpreferspot']);
    const batchAllowOnDemandIndex = resolveColumnIndex(targetingColMap, ['batch_allow_on_demand_fallback', 'batchallowondemandfallback']);
    const batchMachineTypeIndex = resolveColumnIndex(targetingColMap, ['batch_machine_type', 'batchmachinetype']);
    const batchInstanceCountIndex = resolveColumnIndex(targetingColMap, ['batch_instance_count', 'batchinstancecount']);
    const batchWorkersPerWorkflowIndex = resolveColumnIndex(targetingColMap, ['batch_workers_per_workflow', 'batchworkersperworkflow']);
    const batchSignedUrlTtlIndex = resolveColumnIndex(targetingColMap, ['batch_signed_url_ttl_hours', 'batchsignedurlttlhours']);
    const batchSignedUrlRefreshIndex = resolveColumnIndex(targetingColMap, ['batch_signed_url_refresh_threshold_seconds', 'batchsignedurlrefreshthresholdseconds']);
    const batchVcpuPerWorkerIndex = resolveColumnIndex(targetingColMap, ['batch_vcpu_per_worker', 'batchvcpuperworker']);
    const batchMemoryPerWorkerIndex = resolveColumnIndex(targetingColMap, ['batch_memory_per_worker_mb', 'batchmemoryperworkermb']);
    const batchMaxAttemptsIndex = resolveColumnIndex(targetingColMap, ['batch_max_attempts', 'batchmaxattempts']);

    const useGcpBatch = useBatchColIndex >= 0 ? parseOptionalBoolean(targetingRow[useBatchColIndex]) : undefined;
    const batchMaxParallelism = batchMaxParallelIndex >= 0 ? parseOptionalInteger(targetingRow[batchMaxParallelIndex]) : null;
    const batchPreferSpot = batchPreferSpotIndex >= 0 ? parseOptionalBoolean(targetingRow[batchPreferSpotIndex]) : undefined;
    const batchAllowOnDemand = batchAllowOnDemandIndex >= 0 ? parseOptionalBoolean(targetingRow[batchAllowOnDemandIndex]) : undefined;
    const batchMachineType = batchMachineTypeIndex >= 0 ? String(targetingRow[batchMachineTypeIndex] || '').trim() : '';
    const batchInstanceCount = batchInstanceCountIndex >= 0 ? parseOptionalInteger(targetingRow[batchInstanceCountIndex]) : null;
    const batchWorkersPerWorkflow = batchWorkersPerWorkflowIndex >= 0 ? parseOptionalInteger(targetingRow[batchWorkersPerWorkflowIndex]) : null;
    const batchSignedUrlTtlHours = batchSignedUrlTtlIndex >= 0 ? parseOptionalInteger(targetingRow[batchSignedUrlTtlIndex]) : null;
    const batchSignedUrlRefreshSeconds = batchSignedUrlRefreshIndex >= 0 ? parseOptionalInteger(targetingRow[batchSignedUrlRefreshIndex]) : null;
    const batchVcpuPerWorker = batchVcpuPerWorkerIndex >= 0 ? parseOptionalInteger(targetingRow[batchVcpuPerWorkerIndex]) : null;
    const batchMemoryPerWorkerMb = batchMemoryPerWorkerIndex >= 0 ? parseOptionalInteger(targetingRow[batchMemoryPerWorkerIndex]) : null;
    const batchMaxAttempts = batchMaxAttemptsIndex >= 0 ? parseOptionalInteger(targetingRow[batchMaxAttemptsIndex]) : null;

    try {
      console.log(JSON.stringify({
        level: 'debug',
        event: 'targeting_config_extra_evaluation',
        targeting_id: targetingId,
        extra_col_index: extraColIndex,
        extra_raw_value: extraColIndex >= 0 ? targetingRow[extraColIndex] : null,
        use_extra_table: useExtraTable,
        use_serverless_col_index: serverlessColIndex,
        use_serverless_raw_value: serverlessColIndex >= 0 ? targetingRow[serverlessColIndex] : null,
        use_serverless: useServerless,
        use_gcp_batch_col_index: useBatchColIndex,
        use_gcp_batch_raw_value: useBatchColIndex >= 0 ? targetingRow[useBatchColIndex] : null,
        use_gcp_batch: useGcpBatch
      }));
    } catch (e) {}
    
    // 2シートのデータを結合して完全なクライアント設定を構築（FORM_SENDER.md 1.3.2節のプレースホルダ変数対応）
    // FORM_SENDER.md:124仕様準拠: project_nameは除外してデータ結合
    
    // フィールドバリデーション（department, website_url, address_5のみ空文字許可）
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
    
    // 必須フィールドの検証
    for (const field of requiredFields) {
      const value = clientRow[clientColMap[field] || -1];
      if (!value || value.toString().trim() === '') {
        missingFields.push(field);
      }
    }
    
    if (missingFields.length > 0) {
      throw new Error(`client_id ${clientId} の必須フィールドが不足: ${missingFields.join(', ')}`);
    }
    
    const sessionMaxHoursIndex = resolveColumnIndex(targetingColMap, ['session_max_hours', 'max_session_hours', 'session_hours']);

    const sendEndTimeRaw = targetingRow[targetingColMap['send_end_time'] || -1];
    const resolvedSendEndTime = resolveSendEndTime_(sendEndTimeRaw);

    const sessionMaxHoursRaw = sessionMaxHoursIndex >= 0 ? targetingRow[sessionMaxHoursIndex] : null;
    const resolvedSessionMaxHours = resolveSessionMaxHours_(sessionMaxHoursRaw);

    const clientConfig = {
      // 基本管理情報
      targeting_id: targetingId,
      client_id: clientId,
      active: targetingRow[targetingColMap['active']] === true || targetingRow[targetingColMap['active']] === 'TRUE',
      description: targetingRow[targetingColMap['description'] || -1] || '',
      use_extra_table: useExtraTable,

      // clientシートからの情報をネスト構造化
      client: {
        // 必須フィールド（既に検証済みなので安全に取得）
        company_name: clientRow[clientColMap['company_name'] || -1] || '',
        company_name_kana: clientRow[clientColMap['company_name_kana'] || -1] || '',
        form_sender_name: clientRow[clientColMap['form_sender_name'] || -1] || '',
        last_name: clientRow[clientColMap['last_name'] || -1] || '',
        first_name: clientRow[clientColMap['first_name'] || -1] || '',
        last_name_kana: clientRow[clientColMap['last_name_kana'] || -1] || '',
        first_name_kana: clientRow[clientColMap['first_name_kana'] || -1] || '',
        last_name_hiragana: clientRow[clientColMap['last_name_hiragana'] || -1] || '',
        first_name_hiragana: clientRow[clientColMap['first_name_hiragana'] || -1] || '',
        position: clientRow[clientColMap['position'] || -1] || '',
        gender: clientRow[clientColMap['gender'] || -1] || '',
        email_1: clientRow[clientColMap['email_1'] || -1] || '',
        email_2: clientRow[clientColMap['email_2'] || -1] || '',
        postal_code_1: clientRow[clientColMap['postal_code_1'] || -1] || '',
        postal_code_2: clientRow[clientColMap['postal_code_2'] || -1] || '',
        address_1: clientRow[clientColMap['address_1'] || -1] || '',
        address_2: clientRow[clientColMap['address_2'] || -1] || '',
        address_3: clientRow[clientColMap['address_3'] || -1] || '',
        address_4: clientRow[clientColMap['address_4'] || -1] || '',
        phone_1: clientRow[clientColMap['phone_1'] || -1] || '',
        phone_2: clientRow[clientColMap['phone_2'] || -1] || '',
        phone_3: clientRow[clientColMap['phone_3'] || -1] || '',
        
        // 空文字許可フィールド
        department: clientRow[clientColMap['department'] || -1] || '',
        website_url: clientRow[clientColMap['website_url'] || -1] || '',
        address_5: clientRow[clientColMap['address_5'] || -1] || ''
      },
      
      // targetingシートからの情報をネスト構造化
      targeting: {
        id: targetingId,
        subject: processNewlinesInText(targetingRow[targetingColMap['subject'] || -1] || ''),
        message: processNewlinesInText(targetingRow[targetingColMap['message'] || -1] || ''),
        targeting_sql: targetingRow[targetingColMap['targeting_sql'] || -1] || '',
        ng_companies: targetingRow[targetingColMap['ng_companies'] || -1] || '',
        max_daily_sends: parseInt(targetingRow[targetingColMap['max_daily_sends'] || -1]) || 100,
        send_start_time: targetingRow[targetingColMap['send_start_time'] || -1] || '09:00',
        send_end_time: resolvedSendEndTime,
        send_days_of_week: parseSendDaysOfWeek(targetingRow[targetingColMap['send_days_of_week'] || -1]),
        use_extra_table: useExtraTable,
        session_max_hours: resolvedSessionMaxHours,
        // 追加: 並列起動数（新規 M 列）
        concurrent_workflow: (function() {
          try {
            const v = targetingRow[targetingColMap['concurrent_workflow'] || -1];
            const n = parseInt(v);
            if (isNaN(n) || n <= 0) return 1;
            return n;
          } catch (e) {
            return 1;
          }
        })()
      }
    };

    if (typeof useServerless !== 'undefined') {
      clientConfig.useServerless = useServerless;
      clientConfig.use_serverless = useServerless;
      clientConfig.targeting.useServerless = useServerless;
      clientConfig.targeting.use_serverless = useServerless;
    }

    if (typeof useGcpBatch !== 'undefined') {
      clientConfig.useGcpBatch = useGcpBatch;
      clientConfig.use_gcp_batch = useGcpBatch;
      clientConfig.targeting.useGcpBatch = useGcpBatch;
      clientConfig.targeting.use_gcp_batch = useGcpBatch;
    }

    const batchConfig = {};
    if (typeof useGcpBatch !== 'undefined') {
      batchConfig.enabled = useGcpBatch;
    }
    if (batchMaxParallelism !== null) {
      batchConfig.max_parallelism = batchMaxParallelism;
    }
    if (typeof batchPreferSpot !== 'undefined') {
      batchConfig.prefer_spot = batchPreferSpot;
    }
    if (typeof batchAllowOnDemand !== 'undefined') {
      batchConfig.allow_on_demand_fallback = batchAllowOnDemand;
    }
    if (batchMachineType) {
      batchConfig.machine_type = batchMachineType;
    }
    if (batchInstanceCount !== null && isFinite(batchInstanceCount) && batchInstanceCount >= 1) {
      batchConfig.instance_count = batchInstanceCount;
    }
    if (batchWorkersPerWorkflow !== null && isFinite(batchWorkersPerWorkflow) && batchWorkersPerWorkflow >= 1) {
      batchConfig.workers_per_workflow = Math.min(batchWorkersPerWorkflow, 16);
    }
    if (batchSignedUrlTtlHours !== null) {
      batchConfig.signed_url_ttl_hours = batchSignedUrlTtlHours;
    }
    if (batchSignedUrlRefreshSeconds !== null) {
      batchConfig.signed_url_refresh_threshold_seconds = batchSignedUrlRefreshSeconds;
    }
    if (batchVcpuPerWorker !== null) {
      batchConfig.vcpu_per_worker = batchVcpuPerWorker;
    }
    if (batchMemoryPerWorkerMb !== null) {
      batchConfig.memory_per_worker_mb = batchMemoryPerWorkerMb;
    }
    if (batchMaxAttempts !== null) {
      batchConfig.max_attempts = batchMaxAttempts;
    }

    console.log(`フィールドバリデーション完了: 必須フィールド ${requiredFields.length} 件OK, 空文字許可フィールド ${optionalFields.length} 件`);
    
    // targetingシート固有のバリデーション（targeting_sql, ng_companiesは空文字許可）
    const targetingRequiredFields = ['subject', 'message', 'max_daily_sends', 'send_start_time', 'send_days_of_week'];
    const targetingRequiredWithFallback = ['send_end_time'];
    const targetingOptionalFields = ['targeting_sql', 'ng_companies'];
    const targetingMissingFields = [];
    
    for (const field of targetingRequiredFields) {
      const value = targetingRow[targetingColMap[field] || -1];
      if (!value || (typeof value === 'string' && value.trim() === '')) {
        targetingMissingFields.push(field);
      }
    }

    const fallbackMissing = targetingRequiredWithFallback.filter(field => {
      if (field === 'send_end_time') {
        return !clientConfig.targeting.send_end_time || clientConfig.targeting.send_end_time.trim() === '';
      }
      return false;
    });

    const effectiveMissing = targetingMissingFields.filter(field => targetingRequiredWithFallback.indexOf(field) === -1);

    if (effectiveMissing.length > 0 || fallbackMissing.length > 0) {
      const combined = effectiveMissing.concat(fallbackMissing);
      throw new Error(`targeting_id ${targetingId} のtargetingシート必須フィールドが不足: ${combined.join(', ')}`);
    }
    
    // 空文字許可フィールドのログ出力
    const targeting_sql = clientConfig.targeting?.targeting_sql ? clientConfig.targeting.targeting_sql.trim() : '';
    const ng_companies = clientConfig.targeting?.ng_companies ? clientConfig.targeting.ng_companies.trim() : '';
    console.log(`targeting_sql: ${targeting_sql ? '設定あり' : '空文字（絞り込みなし）'}`);
    console.log(`ng_companies: ${ng_companies ? '設定あり' : '空文字（除外なし）'}`);
    console.log(`targetingシートバリデーション完了: 必須フィールド ${targetingRequiredFields.length} 件OK, 空文字許可フィールド ${targetingOptionalFields.length} 件`);
    
    console.log(`targeting_id ${targetingId} の2シート結合設定取得完了: ${clientConfig.client?.company_name} (client_id: ${clientConfig.client_id})`);
    clientConfig.batch = batchConfig;
    return clientConfig;
    
  } catch (error) {
    console.error(`targeting_id ${targetingId} の2シート結合設定取得エラー: ${error.message}`);
    throw error;
  }
}

/**
 * send_days_of_week の安全な解析
 * @param {string|Array} value スプレッドシートの値
 * @returns {Array} 曜日の配列（デフォルト: 月火水木金）
 */
function parseSendDaysOfWeek(value) {
  // デフォルト値（月火水木金）
  const defaultDays = [0, 1, 2, 3, 4];
  
  try {
    // 既に配列の場合
    if (Array.isArray(value)) {
      return value.filter(day => typeof day === 'number' && day >= 0 && day <= 6);
    }
    
    // null または undefined の場合
    if (!value) {
      console.log('send_days_of_week が空のためデフォルト値を使用: [0,1,2,3,4]');
      return defaultDays;
    }
    
    // 文字列の場合、JSONとして解析を試行
    if (typeof value === 'string') {
      const trimmed = value.trim();
      
      // 空文字列の場合
      if (!trimmed) {
        console.log('send_days_of_week が空文字列のためデフォルト値を使用: [0,1,2,3,4]');
        return defaultDays;
      }
      
      // JSON解析
      const parsed = JSON.parse(trimmed);
      
      // 解析結果が配列でない場合
      if (!Array.isArray(parsed)) {
        console.log(`send_days_of_week が配列でないためデフォルト値を使用: ${typeof parsed}`);
        return defaultDays;
      }
      
      // 有効な曜日値（0-6）のみフィルター
      const validDays = parsed.filter(day => typeof day === 'number' && day >= 0 && day <= 6);
      
      if (validDays.length === 0) {
        console.log('send_days_of_week に有効な曜日が含まれていないためデフォルト値を使用');
        return defaultDays;
      }
      
      console.log(`send_days_of_week 解析成功: ${JSON.stringify(validDays)}`);
      return validDays;
    }
    
    // その他の型の場合
    console.log(`send_days_of_week の型が不正のためデフォルト値を使用: ${typeof value}`);
    return defaultDays;
    
  } catch (parseError) {
    console.error(`send_days_of_week の JSON 解析エラー: ${parseError.message}, 値: ${value}`);
    console.log('デフォルト値を使用: [0,1,2,3,4]');
    return defaultDays;
  }
}

/**
 * スプレッドシート連携の統合テスト
 * @returns {Object} テスト結果
 */
function testSpreadsheetIntegration() {
  try {
    console.log('=== スプレッドシート連携統合テスト開始 ===');
    
    // 設定確認
    const configResult = validateSpreadsheetConfig();
    console.log('設定確認結果:', configResult.success ? '成功' : '失敗');
    
    if (!configResult.success) {
      return { success: false, error: '設定確認失敗', details: configResult };
    }
    
    // アクティブなターゲティング取得テスト
    const activeTargetings = getActiveTargetingIdsFromSheet();
    console.log(`アクティブなターゲティング取得: ${activeTargetings.length}件`);
    
    activeTargetings.forEach(targeting => {
      console.log(`- ID: ${targeting.targeting_id}, クライアントID: ${targeting.client_id}, 説明: ${targeting.description}`);
    });
    
    console.log('=== スプレッドシート連携統合テスト完了 ===');
    
    return {
      success: true,
      message: 'スプレッドシート連携テスト完了',
      config_result: configResult,
      active_targetings: activeTargetings,
      active_count: activeTargetings.length
    };
    
  } catch (error) {
    console.error(`スプレッドシート連携統合テストエラー: ${error.message}`);
    return { success: false, error: error.message };
  }
}

/**
 * テキスト内の改行コードを適切に処理する
 * スプレッドシートから取得したテキストの改行を正しくフォーマット
 * @param {string} text 処理対象のテキスト
 * @returns {string} 改行処理済みのテキスト
 */
function processNewlinesInText(text) {
  try {
    if (!text || typeof text !== 'string') {
      return text || '';
    }
    
    // スプレッドシートの改行は通常\nとして保存される
    // 必要に応じて追加の改行処理を実装
    let processed = text;
    
    // 明示的な\nがある場合は改行として処理（二重エスケープ対応）
    processed = processed.replace(/\\n/g, '\n');
    
    // その他のエスケープシーケンス
    processed = processed.replace(/\\t/g, '\t');
    processed = processed.replace(/\\r/g, '\r');
    
    console.log(`改行処理実行: 元長さ ${text.length} -> 処理後長さ ${processed.length}`);
    
    return processed;
    
  } catch (error) {
    console.error(`改行処理エラー: ${error.message}, 元テキストを返します`);
    return text || '';
  }
}
