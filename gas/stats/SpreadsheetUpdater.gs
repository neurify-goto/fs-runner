/**
 * スプレッドシート更新モジュール
 * 統計情報をスプレッドシートの固定セルに上書き更新する機能
 */

// スプレッドシート設定
const SPREADSHEET_CONFIG = {
  SHEET_ID: '1qdkfJHBMKhbRlkkXI7v_0S1RRP_9jKxGI4QI-IeNGiI',
  SHEET_NAME: '統計情報',
  LOG_SHEET_NAME: '統計ログ'
};

// targetingシート設定（列ヘッダーをもとに出力列を判定）
const TARGETING_SHEET_CONFIG = {
  ID_COLUMN: 2, // B列 - targeting_id（ヘッダー名が不変のため固定）
  HEADER_ROW_INDEX: 1
};

const TARGETING_COLUMN_HEADERS = {
  SUBMISSIONS_TOTAL_ALL: '送信試行数',
  SUBMISSIONS_SUCCESS_ALL: '送信成功数',
  SUBMISSIONS_TOTAL_TODAY: '本日送信試行数',
  SUBMISSIONS_SUCCESS_TODAY: '本日送信成功数',
  SUBMISSIONS_SUCCESS_RATE_TODAY: '本日送信成功率'
};

/**
 * targetingシートのヘッダー行から列名と列番号のマップを構築
 * @param {GoogleAppsScript.Spreadsheet.Sheet} sheet 対象のシート
 * @param {number} headerRowIndex ヘッダー行の行番号（1-based）
 * @returns {Object} 列名をキー、列番号を値とするマップ
 */
function buildTargetingHeaderIndexMap(sheet, headerRowIndex) {
  const lastColumn = sheet.getLastColumn();
  if (lastColumn <= 0) {
    return {};
  }

  const headerValues = sheet
    .getRange(headerRowIndex, 1, 1, lastColumn)
    .getValues()[0]
    .map(value => (typeof value === 'string' ? value.trim() : value));

  const headerIndexMap = {};
  headerValues.forEach((header, index) => {
    if (!header) {
      return;
    }
    headerIndexMap[header] = index + 1; // 列番号は1-based
  });

  return headerIndexMap;
}

/**
 * ヘッダーマップから指定した列名に対応する列番号を取得
 * @param {Object} headerIndexMap 列名→列番号マップ
 * @param {string} headerName 列名
 * @returns {number} 列番号（1-based）
 */
function resolveTargetingColumnIndex(headerIndexMap, headerName) {
  const columnIndex = headerIndexMap[headerName];
  if (!columnIndex) {
    throw new Error(`targetingシートのヘッダー「${headerName}」が見つかりません`);
  }
  return columnIndex;
}

// セル配置設定（1行目：タイトル行、2行目以降：統計データ）
const CELL_MAPPING = {
  lastUpdate: 'A2', // 最終更新日時
  lastUpdateValue: 'B2',
  lastUpdateChange1Hour: 'C2', // 1時間変化（空欄）
  lastUpdateChange24Hour: 'D2', // 24時間変化（空欄）
  totalCount: 'A3', // 全企業数
  totalCountValue: 'B3',
  totalCountChange1Hour: 'C3', // 1時間変化
  totalCountChange24Hour: 'D3', // 24時間変化
  withCompanyUrl: 'A4', // 企業URLあり
  withCompanyUrlValue: 'B4',
  withCompanyUrlChange1Hour: 'C4', // 1時間変化
  withCompanyUrlChange24Hour: 'D4', // 24時間変化
  formNotExplored: 'A5', // フォーム未探索
  formNotExploredValue: 'B5',
  formNotExploredChange1Hour: 'C5', // 1時間変化
  formNotExploredChange24Hour: 'D5', // 24時間変化
  withFormUrl: 'A6', // フォームURLあり
  withFormUrlValue: 'B6',
  withFormUrlChange1Hour: 'C6', // 1時間変化
  withFormUrlChange24Hour: 'D6', // 24時間変化
  validForm: 'A7', // 有効フォーム（form_found=true AND prohibition_detected/duplication/black が NULL）
  validFormValue: 'B7',
  validFormChange1Hour: 'C7',
  validFormChange24Hour: 'D7'
};

/**
 * スプレッドシートに統計情報を更新
 * @param {Object} stats 統計データ
 * @param {Object} changes1Hour 1時間変化データ（オプション）
 * @param {Object} changes24Hour 24時間変化データ（オプション）
 * @returns {Object} 更新結果
 */
function updateSpreadsheet(stats, changes1Hour = null, changes24Hour = null) {
  try {
    console.log('スプレッドシート更新開始');
    
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_CONFIG.SHEET_ID);
    let sheet;
    
    try {
      sheet = spreadsheet.getSheetByName(SPREADSHEET_CONFIG.SHEET_NAME);
    } catch (error) {
      console.log(`シート「${SPREADSHEET_CONFIG.SHEET_NAME}」が存在しないため新規作成します`);
      sheet = spreadsheet.insertSheet(SPREADSHEET_CONFIG.SHEET_NAME);
    }
    
    // JST（日本時間）での現在時刻を取得
    const jstTime = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy/MM/dd HH:mm:ss');
    
    // セルの更新データを準備（タイトル行は更新せず、統計データのみ更新）
    const updates = [
      // 項目名の設定（A列）
      [CELL_MAPPING.lastUpdate, '最終更新'],
      [CELL_MAPPING.totalCount, '全企業数'],
      [CELL_MAPPING.withCompanyUrl, '企業URLあり'],
      [CELL_MAPPING.formNotExplored, 'フォーム未探索'],
      [CELL_MAPPING.withFormUrl, 'フォームURLあり'],
      [CELL_MAPPING.validForm, '有効フォーム'],

      // 数値データの設定（B列）
      [CELL_MAPPING.lastUpdateValue, jstTime],
      [CELL_MAPPING.totalCountValue, stats.totalCount || 0],
      [CELL_MAPPING.withCompanyUrlValue, stats.withCompanyUrl || 0],
      [CELL_MAPPING.formNotExploredValue, stats.formNotExplored || 0],
      [CELL_MAPPING.withFormUrlValue, stats.withFormUrl || 0],
      [CELL_MAPPING.validFormValue, stats.validForm || 0]
    ];
    
    // 1時間変化データの追加（C列）
    if (changes1Hour) {
      updates.push(
        [CELL_MAPPING.lastUpdateChange1Hour, ''], // 最終更新の変化は空欄
        [CELL_MAPPING.totalCountChange1Hour, changes1Hour.totalCount || '-'],
        [CELL_MAPPING.withCompanyUrlChange1Hour, changes1Hour.withCompanyUrl || '-'],
        [CELL_MAPPING.formNotExploredChange1Hour, changes1Hour.formNotExplored || '-'],
        [CELL_MAPPING.withFormUrlChange1Hour, changes1Hour.withFormUrl || '-'],
        [CELL_MAPPING.validFormChange1Hour, changes1Hour.validForm || '-']
      );
    }
    
    // 24時間変化データの追加（D列）
    if (changes24Hour) {
      updates.push(
        [CELL_MAPPING.lastUpdateChange24Hour, ''], // 最終更新の変化は空欄
        [CELL_MAPPING.totalCountChange24Hour, changes24Hour.totalCount || '-'],
        [CELL_MAPPING.withCompanyUrlChange24Hour, changes24Hour.withCompanyUrl || '-'],
        [CELL_MAPPING.formNotExploredChange24Hour, changes24Hour.formNotExplored || '-'],
        [CELL_MAPPING.withFormUrlChange24Hour, changes24Hour.withFormUrl || '-'],
        [CELL_MAPPING.validFormChange24Hour, changes24Hour.validForm || '-']
      );
    }
    
    // 各セルを個別に更新（値のみ）
    updates.forEach(([cellAddress, value]) => {
      const range = sheet.getRange(cellAddress);
      range.setValue(value);
    });
    
    console.log(`スプレッドシート更新完了 (${jstTime})`);
    
    return {
      success: true,
      message: `統計情報を正常に更新しました (${jstTime})`,
      updatedAt: jstTime,
      stats: stats
    };
    
  } catch (error) {
    console.error('スプレッドシート更新エラー:', error);
    return {
      success: false,
      error: error.toString(),
      timestamp: Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy/MM/dd HH:mm:ss')
    };
  }
}

/**
 * スプレッドシート接続テスト
 * @returns {Object} テスト結果
 */
function testSpreadsheetConnection() {
  try {
    console.log('スプレッドシート接続テスト開始');
    
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_CONFIG.SHEET_ID);
    const spreadsheetName = spreadsheet.getName();
    
    console.log(`スプレッドシート接続成功: ${spreadsheetName}`);
    
    // シートの存在確認
    let sheet;
    try {
      sheet = spreadsheet.getSheetByName(SPREADSHEET_CONFIG.SHEET_NAME);
      console.log(`シート「${SPREADSHEET_CONFIG.SHEET_NAME}」が存在します`);
    } catch (error) {
      console.log(`シート「${SPREADSHEET_CONFIG.SHEET_NAME}」は存在しません（必要に応じて作成されます）`);
      sheet = null;
    }
    
    return {
      success: true,
      message: `スプレッドシート接続成功: ${spreadsheetName}`,
      spreadsheetName: spreadsheetName,
      sheetExists: sheet !== null
    };
    
  } catch (error) {
    console.error('スプレッドシート接続テストエラー:', error);
    return {
      success: false,
      error: error.toString()
    };
  }
}

/**
 * テスト用のダミーデータでスプレッドシート更新
 * @returns {Object} 更新結果
 */
function testSpreadsheetUpdate() {
  console.log('=== スプレッドシート更新テスト（5項目対応） ===');
  
  // テスト用ダミーデータ
  const testStats = {
    totalCount: 10000,
    withCompanyUrl: 8500,
    formNotExplored: 2000,
    withFormUrl: 6500,
    validForm: 6200
  };
  
  // テスト用1時間変化データ
  const testChanges1Hour = {
    totalCount: '5',
    withCompanyUrl: '3',
    formNotExplored: '-2',
    withFormUrl: '3',
    validForm: '2'
  };
  
  // テスト用24時間変化データ
  const testChanges24Hour = {
    totalCount: '100',
    withCompanyUrl: '80',
    formNotExplored: '-20',
    withFormUrl: '65',
    validForm: '60'
  };
  
  const result = updateSpreadsheet(testStats, testChanges1Hour, testChanges24Hour);
  
  if (result.success) {
    console.log('スプレッドシート更新テスト成功:', result.message);
  } else {
    console.error('スプレッドシート更新テスト失敗:', result.error);
  }
  
  return result;
}

/**
 * スプレッドシートの初期セットアップ
 * ヘッダー行と基本フォーマットを設定
 * @returns {Object} セットアップ結果
 */
function setupSpreadsheet() {
  try {
    console.log('スプレッドシート初期セットアップ開始');
    
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_CONFIG.SHEET_ID);
    let sheet;
    
    try {
      sheet = spreadsheet.getSheetByName(SPREADSHEET_CONFIG.SHEET_NAME);
    } catch (error) {
      console.log(`シート「${SPREADSHEET_CONFIG.SHEET_NAME}」を新規作成します`);
      sheet = spreadsheet.insertSheet(SPREADSHEET_CONFIG.SHEET_NAME);
    }
    
    // 既存のデータをクリア
    sheet.clear();
    
    // ヘッダー行の設定（5項目行 × 4列）
    const headers = [
      ['項目', '数値', '1h変化', '24h変化'],
      ['最終更新', '', '', ''],
      ['全企業数', '', '', ''],
      ['企業URLあり', '', '', ''],
      ['フォーム未探索', '', '', ''],
      ['フォームURLあり', '', '', ''],
      ['有効フォーム', '', '', '']
    ];
    
    // ヘッダーデータを一括設定（値のみ）
    const range = sheet.getRange(1, 1, headers.length, 4);
    range.setValues(headers);
    
    console.log('スプレッドシート初期セットアップ完了');
    
    return {
      success: true,
      message: 'スプレッドシートの初期セットアップが完了しました'
    };
    
  } catch (error) {
    console.error('スプレッドシート初期セットアップエラー:', error);
    return {
      success: false,
      error: error.toString()
    };
  }
}

/**
 * 統計ログシート管理関数群
 * 1441行固定の24時間循環ログシート
 */

/**
 * 現在時刻に対応する統計ログシートの行番号を取得
 * @param {string} time HH:MM形式の時刻
 * @returns {number} 行番号（2-1441）
 */
function getTimeRowIndex(time) {
  const [hours, minutes] = time.split(':').map(Number);
  return (hours * 60 + minutes) + 2; // +2はヘッダー行のオフセット
}

/**
 * 統計ログシートの初期セットアップ
 * 1441行（ヘッダー + 00:00-23:59）の固定構造を作成
 * @returns {Object} セットアップ結果
 */
function setupStatsLogSheet() {
  try {
    console.log('統計ログシート初期セットアップ開始');
    
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_CONFIG.SHEET_ID);
    let logSheet;
    
    try {
      logSheet = spreadsheet.getSheetByName(SPREADSHEET_CONFIG.LOG_SHEET_NAME);
      console.log(`シート「${SPREADSHEET_CONFIG.LOG_SHEET_NAME}」が既に存在します`);
    } catch (error) {
      console.log(`シート「${SPREADSHEET_CONFIG.LOG_SHEET_NAME}」を新規作成します`);
      logSheet = spreadsheet.insertSheet(SPREADSHEET_CONFIG.LOG_SHEET_NAME);
    }
    
    // 既存のデータをクリア
    logSheet.clear();
    
    // ヘッダー行の作成
    const headers = [
      '時刻', '全企業数', '企業URLあり', 'フォーム未探索', 'フォームURLあり', '有効フォーム'
    ];
    
    // 1441行のデータ配列を準備（ヘッダー + 1440分）
    const allData = [headers];
    
    // 00:00から23:59まで1440行のタイムスロットを作成
    for (let hour = 0; hour < 24; hour++) {
      for (let minute = 0; minute < 60; minute++) {
        const timeStr = `${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}`;
        allData.push([timeStr, '', '', '', '', '']); // 時刻 + 5項目の空データ
      }
    }
    
    // 1441行を一括設定
    const range = logSheet.getRange(1, 1, allData.length, headers.length);
    range.setValues(allData);
    
    console.log(`統計ログシート初期セットアップ完了: ${allData.length}行作成`);
    
    return {
      success: true,
      message: `統計ログシートを作成しました (${allData.length}行)`,
      totalRows: allData.length
    };
    
  } catch (error) {
    console.error('統計ログシート初期セットアップエラー:', error);
    return {
      success: false,
      error: error.toString()
    };
  }
}

/**
 * 統計ログシートから1時間前（同分）のデータを取得
 * @param {string} time HH:MM形式の時刻
 * @returns {Object} 1時間前の統計データ
 */
function get1HourAgoData(time) {
  try {
    console.log(`1時間前データ取得: ${time}`);
    
    const [hours, minutes] = time.split(':').map(Number);
    
    // 1時間前の時刻を計算
    let oneHourAgoHours = hours - 1;
    if (oneHourAgoHours < 0) {
      oneHourAgoHours = 23; // 前日の23時
    }
    
    const oneHourAgoTime = `${oneHourAgoHours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}`;
    console.log(`1時間前の時刻: ${oneHourAgoTime}`);
    
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_CONFIG.SHEET_ID);
    const logSheet = spreadsheet.getSheetByName(SPREADSHEET_CONFIG.LOG_SHEET_NAME);
    
    const rowIndex = getTimeRowIndex(oneHourAgoTime);
    
    // 該当行のデータを取得（B列からF列：統計データ部分）
    const dataRange = logSheet.getRange(rowIndex, 2, 1, 5);
    const values = dataRange.getValues()[0];
    
    // データが空の場合（初回実行時など）
    if (values.every(value => value === '')) {
      console.log(`1時間前データなし（初回実行）: ${oneHourAgoTime}`);
      return {
        hasData: false,
        data: null
      };
    }
    
    // 統計データオブジェクトに変換
    const data1HourAgo = {
      totalCount: values[0] || 0,
      withCompanyUrl: values[1] || 0,
      formNotExplored: values[2] || 0,
      withFormUrl: values[3] || 0,
      validForm: values[4] || 0
    };
    
    console.log(`1時間前データ取得成功: ${oneHourAgoTime}`, data1HourAgo);
    
    return {
      hasData: true,
      data: data1HourAgo,
      time: oneHourAgoTime
    };
    
  } catch (error) {
    console.error(`1時間前データ取得エラー (時刻: ${time}):`, error);
    return {
      hasData: false,
      data: null,
      error: error.toString()
    };
  }
}

/**
 * 統計ログシートから24時間前（同時刻）のデータを取得
 * @param {string} time HH:MM形式の時刻
 * @returns {Object} 24時間前の統計データ
 */
function get24HourAgoData(time) {
  try {
    console.log(`24時間前データ取得: ${time}`);
    
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_CONFIG.SHEET_ID);
    const logSheet = spreadsheet.getSheetByName(SPREADSHEET_CONFIG.LOG_SHEET_NAME);
    
    const rowIndex = getTimeRowIndex(time);
    
    // 該当行のデータを取得（B列からF列：統計データ部分）
    const dataRange = logSheet.getRange(rowIndex, 2, 1, 5);
    const values = dataRange.getValues()[0];
    
    // データが空の場合（初回実行時など）
    if (values.every(value => value === '')) {
      console.log(`24時間前データなし（初回実行）: ${time}`);
      return {
        hasData: false,
        data: null
      };
    }
    
    // 統計データオブジェクトに変換
    const data24HourAgo = {
      totalCount: values[0] || 0,
      withCompanyUrl: values[1] || 0,
      formNotExplored: values[2] || 0,
      withFormUrl: values[3] || 0,
      validForm: values[4] || 0
    };
    
    console.log(`24時間前データ取得成功: ${time}`, data24HourAgo);
    
    return {
      hasData: true,
      data: data24HourAgo
    };
    
  } catch (error) {
    console.error(`24時間前データ取得エラー (時刻: ${time}):`, error);
    return {
      hasData: false,
      data: null,
      error: error.toString()
    };
  }
}

/**
 * 統計ログシートに最新データを保存
 * @param {string} time HH:MM形式の時刻
 * @param {Object} stats 統計データ
 * @returns {Object} 保存結果
 */
function updateStatsLog(time, stats) {
  try {
    console.log(`統計ログ更新: ${time}`, stats);
    
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_CONFIG.SHEET_ID);
    const logSheet = spreadsheet.getSheetByName(SPREADSHEET_CONFIG.LOG_SHEET_NAME);
    
    const rowIndex = getTimeRowIndex(time);
    
    // 統計データを配列形式で準備
    const statsValues = [
      stats.totalCount || 0,
      stats.withCompanyUrl || 0,
      stats.formNotExplored || 0,
      stats.withFormUrl || 0,
      stats.validForm || 0
    ];
    
    // 該当行のデータ部分を更新（B列からF列）
    const dataRange = logSheet.getRange(rowIndex, 2, 1, 5);
    dataRange.setValues([statsValues]);
    
    console.log(`統計ログ更新完了: ${time} (行${rowIndex})`);
    
    return {
      success: true,
      message: `統計ログを更新しました (${time})`,
      time: time,
      rowIndex: rowIndex
    };
    
  } catch (error) {
    console.error(`統計ログ更新エラー (時刻: ${time}):`, error);
    return {
      success: false,
      error: error.toString(),
      time: time
    };
  }
}

/**
 * 1時間変化を計算
 * @param {Object} currentStats 現在の統計
 * @param {Object} data1HourAgo 1時間前の統計
 * @returns {Object} 1時間変化データ
 */
function calculate1HourChanges(currentStats, data1HourAgo) {
  if (!data1HourAgo || !data1HourAgo.hasData) {
    // 1時間前のデータがない場合（初回実行時など）
    return {
      totalCount: '-',
      withCompanyUrl: '-',
      formNotExplored: '-',
      withFormUrl: '-',
      validForm: '-'
    };
  }
  
  const oldData = data1HourAgo.data;
  
  // 各項目の変化を計算
  const changes = {
    totalCount: (currentStats.totalCount || 0) - (oldData.totalCount || 0),
    withCompanyUrl: (currentStats.withCompanyUrl || 0) - (oldData.withCompanyUrl || 0),
    formNotExplored: (currentStats.formNotExplored || 0) - (oldData.formNotExplored || 0),
    withFormUrl: (currentStats.withFormUrl || 0) - (oldData.withFormUrl || 0),
    validForm: (currentStats.validForm || 0) - (oldData.validForm || 0)
  };
  
  // 表示形式に変換（純粋な整数で出力）
  Object.keys(changes).forEach(key => {
    const change = changes[key];
    changes[key] = change.toString();
  });
  
  return changes;
}

/**
 * 24時間変化を計算
 * @param {Object} currentStats 現在の統計
 * @param {Object} data24HourAgo 24時間前の統計
 * @returns {Object} 変化データ
 */
function calculateChanges(currentStats, data24HourAgo) {
  if (!data24HourAgo || !data24HourAgo.hasData) {
    // 24時間前のデータがない場合（初回実行時など）
    return {
      totalCount: '-',
      withCompanyUrl: '-',
      formNotExplored: '-',
      withFormUrl: '-',
      validForm: '-'
    };
  }
  
  const oldData = data24HourAgo.data;
  
  // 各項目の変化を計算
  const changes = {
    totalCount: (currentStats.totalCount || 0) - (oldData.totalCount || 0),
    withCompanyUrl: (currentStats.withCompanyUrl || 0) - (oldData.withCompanyUrl || 0),
    formNotExplored: (currentStats.formNotExplored || 0) - (oldData.formNotExplored || 0),
    withFormUrl: (currentStats.withFormUrl || 0) - (oldData.withFormUrl || 0),
    validForm: (currentStats.validForm || 0) - (oldData.validForm || 0)
  };
  
  // 表示形式に変換（純粋な整数で出力）
  Object.keys(changes).forEach(key => {
    const change = changes[key];
    changes[key] = change.toString();
  });
  
  return changes;
}

/**
 * 統計ログシート関連のテスト
 * @returns {Object} テスト結果
 */
function testStatsLogFunctions() {
  console.log('=== 統計ログ関数テスト開始 ===');
  
  const results = {
    setup: null,
    timeRowIndex: null,
    updateLog: null,
    get24HourAgo: null
  };
  
  try {
    // 1. セットアップテスト
    console.log('1. 統計ログシートセットアップテスト');
    results.setup = setupStatsLogSheet();
    
    // 2. 時刻→行番号変換テスト
    console.log('2. 時刻→行番号変換テスト');
    const testTime = '12:34';
    const rowIndex = getTimeRowIndex(testTime);
    results.timeRowIndex = {
      success: true,
      testTime: testTime,
      rowIndex: rowIndex,
      expected: (12 * 60 + 34) + 2 // 754 + 2 = 756
    };
    console.log(`時刻${testTime} → 行${rowIndex} (期待値: 756)`);
    
    // 3. ログ更新テスト
    console.log('3. 統計ログ更新テスト');
    const testStats = {
      totalCount: 1000,
      withCompanyUrl: 800,
      formNotExplored: 200,
      withFormUrl: 600,
      validForm: 580
    };
    results.updateLog = updateStatsLog(testTime, testStats);
    
    // 4. 24時間前データ取得テスト
    console.log('4. 24時間前データ取得テスト');
    results.get24HourAgo = get24HourAgoData(testTime);
    
    const allSuccess = Object.values(results).every(result => result && result.success);
    
    console.log('=== 統計ログ関数テスト完了 ===');
    console.log(`全体結果: ${allSuccess ? '成功' : '一部失敗'}`);
    
    return {
      success: allSuccess,
      results: results
    };
    
  } catch (error) {
    console.error('統計ログ関数テストエラー:', error);
    return {
      success: false,
      error: error.toString(),
      results: results
    };
  }
}

/**
 * targetingシートのヘッダー名（送信試行数など）を用いて submissions 統計を更新
 * 送信試行数: 各targeting_idの submissions 総数（通算）
 * 送信成功数: 成功 submissions 数（通算、success=true）
 * 本日送信試行数: 当日 submissions 総数
 * 本日送信成功数: 当日成功 submissions 数（success=true）
 * 本日送信成功率: 当日成功数 / 当日送信数 × 100（小数第2位を四捨五入）
 * @returns {Object} 更新結果
 */
function updateTargetingSubmissionsStats() {
  try {
    console.log('targeting submissions統計更新開始');
    
    console.log('targeting統計更新対象をスプレッドシートから取得します');
    
    // スプレッドシートを取得
    const spreadsheet = SpreadsheetApp.openById(SPREADSHEET_CONFIG.SHEET_ID);
    
    // targetingシートの取得（一般的に 'targeting' という名前のシートを想定）
    let targetingSheet;
    try {
      targetingSheet = spreadsheet.getSheetByName('targeting');
    } catch (error) {
      console.error('targetingシートが見つかりません。シート名を確認してください。');
      return {
        success: false,
        error: 'targetingシートが見つかりません',
        timestamp: new Date()
      };
    }
    
    // 列配置をヘッダー名から解決
    const headerIndexMap = buildTargetingHeaderIndexMap(
      targetingSheet,
      TARGETING_SHEET_CONFIG.HEADER_ROW_INDEX
    );

    const idColumnIndex = TARGETING_SHEET_CONFIG.ID_COLUMN;
    const totalAllColumnIndex = resolveTargetingColumnIndex(
      headerIndexMap,
      TARGETING_COLUMN_HEADERS.SUBMISSIONS_TOTAL_ALL
    );
    const successAllColumnIndex = resolveTargetingColumnIndex(
      headerIndexMap,
      TARGETING_COLUMN_HEADERS.SUBMISSIONS_SUCCESS_ALL
    );
    const totalTodayColumnIndex = resolveTargetingColumnIndex(
      headerIndexMap,
      TARGETING_COLUMN_HEADERS.SUBMISSIONS_TOTAL_TODAY
    );
    const successTodayColumnIndex = resolveTargetingColumnIndex(
      headerIndexMap,
      TARGETING_COLUMN_HEADERS.SUBMISSIONS_SUCCESS_TODAY
    );
    const successRateTodayColumnIndex = resolveTargetingColumnIndex(
      headerIndexMap,
      TARGETING_COLUMN_HEADERS.SUBMISSIONS_SUCCESS_RATE_TODAY
    );
    
    console.log(
      'targetingシート列配置:',
      JSON.stringify(
        {
          id: idColumnIndex,
          totalAll: totalAllColumnIndex,
          successAll: successAllColumnIndex,
          totalToday: totalTodayColumnIndex,
          successToday: successTodayColumnIndex,
          successRateToday: successRateTodayColumnIndex
        }
      )
    );
    
    // データ行の範囲を取得（ヘッダー行を除く）
    const dataRowCount = targetingSheet.getLastRow() - 1;
    
    if (dataRowCount <= 0) {
      console.log('targetingシートにデータ行が存在しません');
      return {
        success: false,
        error: 'targetingシートにデータ行が存在しません',
        timestamp: new Date()
      };
    }
    
    // B列（id列）のデータを取得
    const idRange = targetingSheet.getRange(2, idColumnIndex, dataRowCount, 1);
    const idValues = idRange.getValues().map(row => parseInt(row[0]));
    
    console.log(`targetingシートB列から取得したid一覧: [${idValues.join(', ')}] (${dataRowCount}行)`);
    
    // 有効なtargeting_idのみを抽出（NaN、0以下を除外）
    const validTargetingIds = idValues.filter(id => !isNaN(id) && id > 0);
    console.log(`有効なtargeting_id: ${validTargetingIds.length}件 (${validTargetingIds.join(', ')})`);
    
    // 【通算統計の一括取得】
    console.log('通算統計の一括取得を実行中...');
    const batchStartTime = new Date();
    
    const allStats = getAllSubmissionsStatsByTargeting(validTargetingIds);
    
    const batchEndTime = new Date();
    const batchTime = batchEndTime.getTime() - batchStartTime.getTime();
    console.log(`通算統計一括取得完了: ${Object.keys(allStats).length}件の統計を取得, 処理時間: ${batchTime}ms`);
    
    // 【本日統計の一括取得】
    console.log('本日統計の一括取得を実行中（UTC→JST変換対応）...');
    const todayBatchStartTime = new Date();
    
    const allTodayStats = getAllSubmissionsStatsByTargetingToday(validTargetingIds);
    
    const todayBatchEndTime = new Date();
    const todayBatchTime = todayBatchEndTime.getTime() - todayBatchStartTime.getTime();
    console.log(`本日統計一括取得完了: ${Object.keys(allTodayStats).length}件の本日統計を取得, 処理時間: ${todayBatchTime}ms`);
    
    // 各行のsubmissions統計を設定（通算＋本日）
    const totalAllColumnValues = [];
    const successAllColumnValues = [];
    const totalTodayColumnValues = [];
    const successTodayColumnValues = [];
    const successRateTodayColumnValues = [];
    
    let successCount = 0;
    let invalidCount = 0;
    
    for (let i = 0; i < dataRowCount; i++) {
      const rowNumber = i + 2; // 実際の行番号（1-based, ヘッダー行を除く）
      const targetingId = idValues[i];
      
      if (isNaN(targetingId) || targetingId <= 0) {
        console.log(`行 ${rowNumber}: id が無効な値 (${targetingId}) - 空文字を設定`);
        totalAllColumnValues.push(['']);
        successAllColumnValues.push(['']);
        totalTodayColumnValues.push(['']);
        successTodayColumnValues.push(['']);
        successRateTodayColumnValues.push(['']);
        invalidCount++;
        continue;
      }
      
      // 一括取得結果から通算統計を取得
      const stats = allStats[targetingId];
      const totalCount = stats ? (stats.total_count || 0) : 0;
      const successSubmissionsCount = stats ? (stats.success_count || 0) : 0;
      
      // 一括取得結果から本日統計を取得
      const todayStats = allTodayStats[targetingId];
      const todayTotalCount = todayStats ? (todayStats.total_count_today || 0) : 0;
      const todaySuccessSubmissionsCount = todayStats ? (todayStats.success_count_today || 0) : 0;
      
      // 各列にデータを設定
      const todaySuccessRate = todayTotalCount > 0
        ? Math.round((todaySuccessSubmissionsCount / todayTotalCount) * 10000) / 100
        : 0;

      totalAllColumnValues.push([totalCount]);
      successAllColumnValues.push([successSubmissionsCount]);
      totalTodayColumnValues.push([todayTotalCount]);
      successTodayColumnValues.push([todaySuccessSubmissionsCount]);
      successRateTodayColumnValues.push([todaySuccessRate]);
      
      console.log(
        `行 ${rowNumber} (targeting_id=${targetingId}): 通算(${totalCount}, ${successSubmissionsCount}) 本日(${todayTotalCount}, ${todaySuccessSubmissionsCount}) 成功率(${todaySuccessRate}%)`
      );
      successCount++;
    }
    
    // N列・O列・P列・Q列を一括更新（値のみ）
    const totalAllRange = targetingSheet.getRange(2, totalAllColumnIndex, dataRowCount, 1);
    totalAllRange.setValues(totalAllColumnValues);
    
    const successAllRange = targetingSheet.getRange(2, successAllColumnIndex, dataRowCount, 1);
    successAllRange.setValues(successAllColumnValues);
    
    const totalTodayRange = targetingSheet.getRange(2, totalTodayColumnIndex, dataRowCount, 1);
    totalTodayRange.setValues(totalTodayColumnValues);
    
    const successTodayRange = targetingSheet.getRange(2, successTodayColumnIndex, dataRowCount, 1);
    successTodayRange.setValues(successTodayColumnValues);

    const successRateTodayRange = targetingSheet.getRange(2, successRateTodayColumnIndex, dataRowCount, 1);
    successRateTodayRange.setValues(successRateTodayColumnValues);
    
    const jstTime = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy/MM/dd HH:mm:ss');
    
    console.log(`targeting submissions統計更新完了 (${jstTime})`);
    console.log(`更新対象: ${dataRowCount}行, 成功: ${successCount}行, 無効値: ${invalidCount}行`);
    console.log(`処理時間: 通算統計取得 ${batchTime}ms, 本日統計取得 ${todayBatchTime}ms`);
    
    return {
      success: true,
      message: `targeting submissions統計を正常に更新しました (通算+本日統計対応) (${jstTime})`,
      updatedAt: jstTime,
      totalRows: dataRowCount,
      successRows: successCount,
      invalidRows: invalidCount,
      batchProcessingTime: batchTime,
      todayBatchProcessingTime: todayBatchTime,
      targetingIds: idValues
    };
    
  } catch (error) {
    console.error('targeting submissions統計更新エラー:', error);
    return {
      success: false,
      error: error.toString(),
      timestamp: Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy/MM/dd HH:mm:ss')
    };
  }
}

/**
 * targeting submissions統計更新のテスト実行
 * @returns {Object} テスト結果
 */
function testTargetingSubmissionsStatsUpdate() {
  console.log('=== targeting submissions統計更新テスト ===');
  
  const result = updateTargetingSubmissionsStats();
  
  if (result.success) {
    console.log('targeting submissions統計更新テスト成功:', result.message);
    console.log(`更新サマリー: 対象${result.totalRows}行, 成功${result.successRows}行, エラー${result.errorRows}行`);
  } else {
    console.error('targeting submissions統計更新テスト失敗:', result.error);
  }
  
  return result;
}
