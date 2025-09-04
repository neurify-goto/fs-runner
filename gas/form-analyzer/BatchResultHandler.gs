/**
 * Groq Batch API 結果処理ハンドラー
 * バッチ処理結果の取得・更新を行うGAS専用モジュール
 */

// 設定定数
const BATCH_CONFIG = {
  MAX_BATCHES_PER_RUN: 20,    // 1回の実行で処理するバッチ数上限
  GROQ_API_BASE: 'https://api.groq.com/openai/v1',
  RETRY_COUNT: 3,             // リトライ回数
  RETRY_DELAY: 1000,          // リトライ間隔（ミリ秒）
  BATCH_INTERVAL: 2000,       // バッチ処理間隔（2秒）
  
  // ⏱️ Timeout Handling設定
  MAX_EXECUTION_TIME: 300000,     // 最大実行時間（5分 = 300秒）
  GROQ_API_TIMEOUT: 30000,        // Groq API個別タイムアウト（30秒）
  SAFETY_BUFFER: 60000            // GAS実行時間制限に対する安全バッファ（1分）
  
  // Groq Batch API制限
  MAX_BATCH_LINES: 50000,     // バッチファイルの最大行数
  MAX_BATCH_SIZE_MB: 200,     // バッチファイルの最大サイズ（MB）
  RECOMMENDED_BATCH_SIZE: 1000,  // 推奨バッチサイズ（完了率向上のため）
  
  // エラーメッセージ定数
  ERROR_MESSAGES: {
    EMPTY_FORM_ELEMENTS: 'form_elementsが空または無効です',
    INVALID_JSON: 'レスポンスがJSON形式ではありません',
    NO_CHOICES: 'レスポンスにchoicesがありません'
  },
  
  // サンプルデータからプレースホルダーへの変換マッピング
  SAMPLE_TO_PLACEHOLDER_MAPPING: {
    "株式会社サンプル": "{client.company_name}",
    "カブシキガイシャサンプル": "{client.company_name_kana}",
    "yamada": "{client.email_1}",
    "gmail.com": "{client.email_2}",
    "03": "{client.phone_1}",
    "1234": "{client.phone_2}",
    "5678": "{client.phone_3}",
    "山田": "{client.last_name}",
    "太郎": "{client.first_name}",
    "ヤマダ": "{client.last_name_kana}",
    "タロウ": "{client.first_name_kana}",
    "やまだ": "{client.last_name_hiragana}",
    "たろう": "{client.first_name_hiragana}",
    "男性": "{client.gender}",
    "営業部": "{client.department}",
    "部長": "{client.position}",
    "100": "{client.postal_code_1}",
    "0001": "{client.postal_code_2}",
    "東京都": "{client.address_1}",
    "千代田区": "{client.address_2}",
    "神田": "{client.address_3}",
    "1-1-1": "{client.address_4}",
    "サンプルビル5F": "{client.address_5}",
    "https://www.sample-corp.co.jp": "{client.website_url}",
    "お問い合わせ": "{targeting.subject}",
    "御社のサービスについて詳しく教えてください。": "{targeting.message}"
  }
};

/**
 * form_elementsが空または無効かどうかを判定するヘルパー関数
 * @param {*} formElements 検証対象のform_elements
 * @returns {boolean} 空または無効の場合はtrue
 */
function isFormElementsEmpty(formElements) {
  // null または undefined の場合
  if (formElements === null || formElements === undefined) {
    return true;
  }
  
  // 配列の場合
  if (Array.isArray(formElements)) {
    return formElements.length === 0;
  }
  
  // オブジェクトの場合
  if (typeof formElements === 'object') {
    return Object.keys(formElements).length === 0;
  }
  
  // その他の型（文字列、数値など）の場合は有効とみなす
  return false;
}

/**
 * サンプルデータをプレースホルダーに変換する関数
 * @param {Object|Array|string} data 変換対象のデータ
 * @returns {Object|Array|string} プレースホルダーに変換されたデータ
 */
function convertSampleDataToPlaceholders(data) {
  try {
    // null または undefined の場合はそのまま返す
    if (data === null || data === undefined) {
      return data;
    }
    
    // 文字列の場合は置換処理を実行
    if (typeof data === 'string') {
      let convertedString = data;
      
      // SAMPLE_TO_PLACEHOLDER_MAPPINGに基づいて文字列置換
      Object.keys(BATCH_CONFIG.SAMPLE_TO_PLACEHOLDER_MAPPING).forEach(sampleText => {
        const placeholder = BATCH_CONFIG.SAMPLE_TO_PLACEHOLDER_MAPPING[sampleText];
        // グローバル置換（全ての出現箇所を置換）
        convertedString = convertedString.replace(new RegExp(escapeRegExp(sampleText), 'g'), placeholder);
      });
      
      return convertedString;
    }
    
    // 配列の場合は再帰的に各要素を変換
    if (Array.isArray(data)) {
      return data.map(item => convertSampleDataToPlaceholders(item));
    }
    
    // オブジェクトの場合は再帰的に各プロパティを変換
    if (typeof data === 'object') {
      const convertedObject = {};
      Object.keys(data).forEach(key => {
        convertedObject[key] = convertSampleDataToPlaceholders(data[key]);
      });
      return convertedObject;
    }
    
    // その他の型（数値、ブール値など）はそのまま返す
    return data;
    
  } catch (error) {
    console.error(`プレースホルダー変換エラー: ${error}`);
    return data; // エラーが発生した場合は元のデータを返す
  }
}

/**
 * 正規表現で使用する特殊文字をエスケープするヘルパー関数
 * @param {string} string エスケープ対象の文字列
 * @returns {string} エスケープされた文字列
 */
function escapeRegExp(string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * ★【メイン関数】Groq Batch API結果チェック・更新処理（Timeout対応版）
 * 定期実行で呼び出されるメイン処理
 */
function checkBatchResults() {
  const startTime = new Date().getTime();
  
  try {
    console.log('=== Groq Batch API 結果チェック開始 ===');
    console.log(`⏱️ 実行開始時刻: ${new Date().toISOString()}, 制限時間: ${BATCH_CONFIG.MAX_EXECUTION_TIME}ms`);
    
    // 1. 未完了のバッチリクエストを取得
    const pendingBatches = getPendingBatchRequests();
    
    if (!pendingBatches || pendingBatches.length === 0) {
      console.log('処理対象のバッチリクエストが見つかりません');
      return { success: true, message: '処理対象なし', processed: 0 };
    }
    
    console.log(`処理対象バッチ数: ${pendingBatches.length}件`);
    
    // 処理数制限
    const batchesToProcess = pendingBatches.slice(0, BATCH_CONFIG.MAX_BATCHES_PER_RUN);
    console.log(`今回処理するバッチ数: ${batchesToProcess.length}件`);
    
    let processedCount = 0;
    let completedCount = 0;
    let failedCount = 0;
    
    // 2. 各バッチの状況を確認・処理（Timeout監視付き）
    for (let i = 0; i < batchesToProcess.length; i++) {
      const batchRequest = batchesToProcess[i];
      
      // ⏱️ 実行時間チェック（安全バッファを考慮）
      const currentTime = new Date().getTime();
      const elapsedTime = currentTime - startTime;
      const remainingTime = BATCH_CONFIG.MAX_EXECUTION_TIME - BATCH_CONFIG.SAFETY_BUFFER - elapsedTime;
      
      if (remainingTime <= 0) {
        console.warn(`⏱️ TIMEOUT: 実行時間制限に到達。残り${batchesToProcess.length - i}件をスキップします`);
        console.warn(`⏱️ 経過時間: ${elapsedTime}ms, 制限: ${BATCH_CONFIG.MAX_EXECUTION_TIME}ms`);
        break;
      }
      
      console.log(`⏱️ バッチ処理進行: ${i + 1}/${batchesToProcess.length}, 残り時間: ${Math.floor(remainingTime / 1000)}秒`);
      
      try {
        const batchId = batchRequest.batch_id;
        console.log(`バッチ処理開始: ${batchId} (${i + 1}/${batchesToProcess.length})`);
        
        // Groq APIでバッチ状況確認（タイムアウト付き）
        const batchStatus = checkGroqBatchStatusWithTimeout(batchId, remainingTime);
        
        if (!batchStatus.success) {
          console.error(`バッチステータス確認失敗: ${batchId} - ${batchStatus.error}`);
          // ステータス確認失敗でも次のバッチを処理（継続処理）
          processedCount++;
          continue;
        }
        
        console.log(`バッチ ${batchId} ステータス: ${batchStatus.status}`);
        
        // request_counts情報をログ出力
        if (batchStatus.request_counts) {
          const counts = batchStatus.request_counts;
          console.log(`  リクエスト統計 - 合計: ${counts.total}, 完了: ${counts.completed}, 失敗: ${counts.failed}`);
        }
        
        // ステータスに応じた処理
        if (batchStatus.status === 'completed') {
          // 完了: 結果取得・更新処理
          const processResult = processCompletedBatch(batchId, batchStatus.data);
          
          if (processResult.success) {
            completedCount++;
            console.log(`バッチ処理完了: ${batchId}`);
          } else {
            // 処理失敗でも次のバッチに進む（継続処理）
            failedCount++;
            console.error(`バッチ処理失敗: ${batchId} - ${processResult.error}`);
          }
          
        } else if (['expired', 'cancelled', 'failed'].includes(batchStatus.status)) {
          // 失敗系: batch_requestを失敗として更新
          const updateResult = updateBatchRequestStatus([{
            batch_id: batchId,
            completed: false
          }]);
          
          if (updateResult.success) {
            failedCount++;
            console.log(`バッチ失敗処理完了: ${batchId} (${batchStatus.status})`);
          } else {
            console.error(`バッチ失敗更新失敗: ${batchId} - ${updateResult.error}`);
            failedCount++;
          }
          
        } else {
          // 処理中 (validating, in_progress, finalizing): スキップ
          console.log(`バッチ処理中のためスキップ: ${batchId} (${batchStatus.status})`);
        }
        
        processedCount++;
        
        // 次のバッチ処理前に間隔を置く（最後のバッチを除く）
        if (i < batchesToProcess.length - 1) {
          console.log(`次のバッチ処理まで${BATCH_CONFIG.BATCH_INTERVAL}ms待機`);
          Utilities.sleep(BATCH_CONFIG.BATCH_INTERVAL);
        }
        
      } catch (error) {
        console.error(`バッチ処理エラー: ${batchRequest.batch_id} - ${error}`);
        // エラーが発生しても次のバッチを処理（継続処理）
        processedCount++;
        failedCount++;
      }
    }
    
    const totalElapsedTime = new Date().getTime() - startTime;
    console.log(`=== 処理完了 ===`);
    console.log(`⏱️ 総実行時間: ${totalElapsedTime}ms (${Math.floor(totalElapsedTime / 1000)}秒)`);
    console.log(`処理済み: ${processedCount}件`);
    console.log(`完了: ${completedCount}件`);
    console.log(`失敗: ${failedCount}件`);
    console.log(`⏱️ 制限時間残り: ${Math.floor((BATCH_CONFIG.MAX_EXECUTION_TIME - totalElapsedTime) / 1000)}秒`);
    
    return {
      success: true,
      processed: processedCount,
      completed: completedCount,
      failed: failedCount,
      total_pending: pendingBatches.length,
      execution_time: totalElapsedTime,
      timeout_occurred: totalElapsedTime >= (BATCH_CONFIG.MAX_EXECUTION_TIME - BATCH_CONFIG.SAFETY_BUFFER)
    };
    
  } catch (error) {
    console.error('バッチ結果チェック全体エラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * 未完了のバッチリクエストを取得
 * @returns {Array} バッチリクエスト配列
 */
function getPendingBatchRequests() {
  try {
    const supabase = getSupabaseClient();
    
    // batch_requestテーブルから未完了のリクエストを取得
    const query = `${supabase.url}/rest/v1/batch_request?select=*&requested=eq.true&completed=is.null&order=created_at.asc&limit=${BATCH_CONFIG.MAX_BATCHES_PER_RUN * 2}`;
    
    // リトライ機能付きリクエスト実行
    const result = supabaseRequestWithRetry(query, {
      method: 'GET',
      headers: supabase.headers
    }, 'バッチリクエスト取得');
    
    if (result.success) {
      console.log(`未完了バッチリクエスト取得: ${result.data.length}件`);
      return result.data;
    } else if (result.status_code === 404) {
      console.log('batch_requestテーブルが見つかりません（初回実行の可能性）');
      return [];
    } else {
      console.error(`バッチリクエスト取得失敗: ${result.error}`);
      return [];
    }
    
  } catch (error) {
    console.error('バッチリクエスト取得エラー:', error);
    return [];
  }
}

/**
 * タイムアウト付きGroq Batch APIステータス確認
 * @param {string} batchId バッチID
 * @param {number} maxTimeMs 最大実行時間（ミリ秒）
 * @returns {Object} ステータス結果
 */
function checkGroqBatchStatusWithTimeout(batchId, maxTimeMs = BATCH_CONFIG.GROQ_API_TIMEOUT) {
  const startTime = new Date().getTime();
  const timeoutMs = Math.min(maxTimeMs, BATCH_CONFIG.GROQ_API_TIMEOUT);
  
  console.log(`⏱️ Groq APIタイムアウト設定: ${timeoutMs}ms for batch ${batchId}`);
  
  try {
    const groqApiKey = PropertiesService.getScriptProperties().getProperty('GROQ_API_KEY');
    
    if (!groqApiKey) {
      return { success: false, error: 'GROQ_API_KEYが設定されていません' };
    }
    
    const url = `${BATCH_CONFIG.GROQ_API_BASE}/batches/${batchId}`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${groqApiKey}`,
        'Content-Type': 'application/json'
      },
      muteHttpExceptions: true,
      timeout: timeoutMs  // ⏱️ タイムアウト設定追加
    });
    
    const responseCode = response.getResponseCode();
    const responseText = response.getContentText();
    
    const elapsedTime = new Date().getTime() - startTime;
    console.log(`⏱️ Groq API応答時間: ${elapsedTime}ms for batch ${batchId}`);
    
    if (responseCode === 200) {
      const data = JSON.parse(responseText);
      return {
        success: true,
        status: data.status,
        data: data,
        errors: data.errors || [],
        request_counts: data.request_counts || { total: 0, completed: 0, failed: 0 },
        output_file_id: data.output_file_id,
        error_file_id: data.error_file_id,
        elapsed_time: elapsedTime
      };
    } else {
      return {
        success: false,
        error: `HTTP ${responseCode}: ${responseText}`,
        elapsed_time: elapsedTime
      };
    }
    
  } catch (error) {
    const elapsedTime = new Date().getTime() - startTime;
    console.error(`⏱️ Groq APIタイムアウトエラー: ${error} (${elapsedTime}ms)`);
    return { 
      success: false, 
      error: `API Timeout/Error: ${error.toString()}`,
      elapsed_time: elapsedTime
    };
  }
}

/**
 * Groq Batch APIでバッチステータスを確認（リトライ機能付き）- レガシー版
 * @param {string} batchId バッチID
 * @returns {Object} ステータス結果
 */
function checkGroqBatchStatus(batchId) {
  for (let attempt = 1; attempt <= BATCH_CONFIG.RETRY_COUNT; attempt++) {
    try {
      const groqApiKey = PropertiesService.getScriptProperties().getProperty('GROQ_API_KEY');
      
      if (!groqApiKey) {
        return { success: false, error: 'GROQ_API_KEYが設定されていません' };
      }
      
      const url = `${BATCH_CONFIG.GROQ_API_BASE}/batches/${batchId}`;
      
      const response = UrlFetchApp.fetch(url, {
        method: 'GET',
        headers: {
          'Authorization': `Bearer ${groqApiKey}`,
          'Content-Type': 'application/json'
        },
        muteHttpExceptions: true
      });
      
      const responseCode = response.getResponseCode();
      const responseText = response.getContentText();
      
      if (responseCode === 200) {
        const data = JSON.parse(responseText);
        return {
          success: true,
          status: data.status,
          data: data,
          errors: data.errors || [],
          request_counts: data.request_counts || { total: 0, completed: 0, failed: 0 },
          output_file_id: data.output_file_id,
          error_file_id: data.error_file_id
        };
      } else if (responseCode >= 500 && attempt < BATCH_CONFIG.RETRY_COUNT) {
        // サーバーエラーの場合はリトライ
        console.warn(`Groq API サーバーエラー (試行 ${attempt}/${BATCH_CONFIG.RETRY_COUNT}): HTTP ${responseCode}`);
        Utilities.sleep(BATCH_CONFIG.RETRY_DELAY * attempt); // 指数バックオフ
        continue;
      } else if (responseCode === 429 && attempt < BATCH_CONFIG.RETRY_COUNT) {
        // レート制限の場合はリトライ
        console.warn(`Groq API レート制限 (試行 ${attempt}/${BATCH_CONFIG.RETRY_COUNT}): HTTP ${responseCode}`);
        Utilities.sleep(BATCH_CONFIG.RETRY_DELAY * attempt * 2); // より長い待機時間
        continue;
      } else {
        return {
          success: false,
          error: `HTTP ${responseCode}: ${responseText}`,
          retry_attempts: attempt
        };
      }
      
    } catch (error) {
      if (attempt < BATCH_CONFIG.RETRY_COUNT) {
        console.warn(`Groq API 接続エラー (試行 ${attempt}/${BATCH_CONFIG.RETRY_COUNT}): ${error}`);
        Utilities.sleep(BATCH_CONFIG.RETRY_DELAY * attempt);
        continue;
      } else {
        console.error(`Groq APIステータス確認エラー: ${error}`);
        return { success: false, error: error.toString(), retry_attempts: attempt };
      }
    }
  }
}

/**
 * 完了したバッチの結果を処理
 * @param {string} batchId バッチID
 * @param {Object} batchData Groq APIから取得したバッチデータ
 * @returns {Object} 処理結果
 */
function processCompletedBatch(batchId, batchData) {
  try {
    console.log(`完了バッチ処理開始: ${batchId}`);
    
    let resultsData = { success: true, results: [] };
    let errorsData = { success: true, errors: [] };
    
    // 結果ファイル取得
    if (batchData.output_file_id) {
      resultsData = retrieveGroqBatchResults(batchData.output_file_id);
      
      if (!resultsData.success) {
        console.error(`結果取得失敗: ${batchId} - ${resultsData.error}`);
        
        // 結果取得失敗をbatch_requestに記録（エラーでも継続処理）
        try {
          updateBatchRequestStatus([{
            batch_id: batchId,
            completed: false
          }]);
        } catch (updateError) {
          console.error(`batch_request更新でもエラー: ${batchId} - ${updateError}`);
        }
        
        return { success: false, error: resultsData.error };
      }
      
      console.log(`結果取得成功: ${batchId} - ${resultsData.results.length}件の結果`);
    } else {
      console.log(`結果ファイルなし: ${batchId}`);
    }
    
    // エラーファイル取得
    if (batchData.error_file_id) {
      errorsData = retrieveGroqBatchErrors(batchData.error_file_id);
      
      if (errorsData.success) {
        console.log(`エラーファイル取得成功: ${batchId} - ${errorsData.errors.length}件のエラー`);
      } else {
        console.warn(`エラーファイル取得失敗: ${batchId} - ${errorsData.error}`);
        // エラーファイル取得失敗は致命的ではないので続行
      }
    }
    
    // 結果もエラーもない場合
    if (resultsData.results.length === 0 && errorsData.errors.length === 0) {
      try {
        const updateResult = updateBatchRequestStatus([{
          batch_id: batchId,
          completed: true
        }]);
        
        if (!updateResult.success) {
          console.error(`batch_request更新失敗（結果なし完了）: ${batchId} - ${updateResult.error}`);
        }
      } catch (updateError) {
        console.error(`batch_request更新エラー（結果なし完了）: ${batchId} - ${updateError}`);
      }
      
      return { success: true, message: '結果ファイルなしで完了' };
    }
    
    // 3. companiesテーブル更新（成功結果）- Transaction Safety実装
    let companiesUpdateCount = 0;
    let companiesUpdateSuccess = true;
    let companiesUpdateError = null;
    
    if (resultsData.results.length > 0) {
      const companiesUpdateResult = updateCompaniesWithResults(resultsData.results);
      
      if (companiesUpdateResult.success) {
        companiesUpdateCount = companiesUpdateResult.updated_count;
        companiesUpdateSuccess = true;
        console.log(`companies更新完了: ${companiesUpdateCount}件`);
      } else {
        companiesUpdateSuccess = false;
        companiesUpdateError = companiesUpdateResult.error;
        console.error(`❌ companies更新失敗: ${companiesUpdateResult.error}`);
        
        // ❌ 重要: companies更新失敗時は処理を中止してデータ不整合を防ぐ
        console.error(`❌ Transaction Safety: companies更新失敗のため、batch_request更新をスキップします`);
        return {
          success: false,
          error: `companies更新失敗: ${companiesUpdateResult.error}`,
          results_count: resultsData.results.length,
          errors_count: errorsData.errors.length,
          companies_updated: 0
        };
      }
    }
    
    // エラー結果はcompaniesテーブルを更新しない（ログ出力のみ）
    if (errorsData.errors.length > 0) {
      console.log(`エラー結果: ${errorsData.errors.length}件（companiesテーブルは更新しない）`);
    }
    
    // 5. batch_request更新（companies更新成功時のみ実行）
    if (companiesUpdateSuccess) {
      try {
        const batchUpdateResult = updateBatchRequestStatus([{
          batch_id: batchId,
          completed: true
        }]);
        
        if (!batchUpdateResult.success) {
          console.error(`❌ batch_request更新失敗: ${batchUpdateResult.error}`);
          
          // ❌ batch_request更新失敗は致命的エラー（companies更新成功だが状態不整合）
          return {
            success: false,
            error: `batch_request更新失敗（companies更新は成功済み）: ${batchUpdateResult.error}`,
            results_count: resultsData.results.length,
            errors_count: errorsData.errors.length,
            companies_updated: companiesUpdateCount
          };
        }
        
        console.log(`✅ Transaction完了: companies更新=${companiesUpdateCount}件, batch_request更新成功`);
        
      } catch (updateError) {
        console.error(`❌ batch_request更新例外エラー: ${batchId} - ${updateError}`);
        return {
          success: false,
          error: `batch_request更新例外エラー（companies更新は成功済み）: ${updateError}`,
          results_count: resultsData.results.length,
          errors_count: errorsData.errors.length,
          companies_updated: companiesUpdateCount
        };
      }
    }
    
    return {
      success: true,
      results_count: resultsData.results.length,
      errors_count: errorsData.errors.length,
      companies_updated: companiesUpdateCount
    };
    
  } catch (error) {
    console.error(`完了バッチ処理エラー: ${error}`);
    return { success: false, error: error.toString() };
  }
}

/**
 * Groq Batch APIからエラーファイルを取得
 * @param {string} errorFileId エラーファイルID
 * @returns {Object} エラーデータ
 */
function retrieveGroqBatchErrors(errorFileId) {
  try {
    const groqApiKey = PropertiesService.getScriptProperties().getProperty('GROQ_API_KEY');
    
    if (!groqApiKey) {
      return { success: false, error: 'GROQ_API_KEYが設定されていません' };
    }
    
    const url = `${BATCH_CONFIG.GROQ_API_BASE}/files/${errorFileId}/content`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${groqApiKey}`
      },
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode !== 200) {
      return {
        success: false,
        error: `エラーファイル取得失敗: HTTP ${responseCode} - ${response.getContentText()}`
      };
    }
    
    // JSONL形式のレスポンスを解析
    const jsonlContent = response.getContentText();
    const errors = parseJsonlErrors(jsonlContent);
    
    return {
      success: true,
      errors: errors
    };
    
  } catch (error) {
    console.error(`エラーファイル取得エラー: ${error}`);
    return { success: false, error: error.toString() };
  }
}

/**
 * JSONL形式のエラーを解析
 * @param {string} jsonlContent JSONL形式のコンテンツ
 * @returns {Array} 解析されたエラー配列
 */
function parseJsonlErrors(jsonlContent) {
  try {
    const errors = [];
    const lines = jsonlContent.split('\n').filter(line => line.trim());
    
    for (const line of lines) {
      try {
        const errorData = JSON.parse(line);
        
        // custom_idからrecord_idを抽出
        const customId = errorData.custom_id;
        const recordId = customId ? customId.replace('company_', '') : null;
        
        if (!recordId) {
          console.warn(`custom_idからrecord_id抽出失敗: ${customId}`);
          continue;
        }
        
        errors.push({
          record_id: parseInt(recordId),
          custom_id: customId,
          error: errorData.error || '未知のエラー',
          status: 'failed'
        });
        
      } catch (lineError) {
        console.error(`JSONLエラー行解析エラー: ${lineError} - 行: ${line}`);
      }
    }
    
    console.log(`JSONLエラー解析完了: ${errors.length}件のエラー`);
    return errors;
    
  } catch (error) {
    console.error(`JSONLエラー解析エラー: ${error}`);
    return [];
  }
}

/**
 * Groq Batch APIから結果ファイルを取得
 * @param {string} outputFileId 出力ファイルID
 * @returns {Object} 結果データ
 */
function retrieveGroqBatchResults(outputFileId) {
  try {
    const groqApiKey = PropertiesService.getScriptProperties().getProperty('GROQ_API_KEY');
    
    if (!groqApiKey) {
      return { success: false, error: 'GROQ_API_KEYが設定されていません' };
    }
    
    const url = `${BATCH_CONFIG.GROQ_API_BASE}/files/${outputFileId}/content`;
    
    const response = UrlFetchApp.fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${groqApiKey}`
      },
      muteHttpExceptions: true
    });
    
    const responseCode = response.getResponseCode();
    
    if (responseCode !== 200) {
      return {
        success: false,
        error: `結果ファイル取得失敗: HTTP ${responseCode} - ${response.getContentText()}`
      };
    }
    
    // JSONL形式のレスポンスを解析
    const jsonlContent = response.getContentText();
    const results = parseJsonlResults(jsonlContent);
    
    return {
      success: true,
      results: results
    };
    
  } catch (error) {
    console.error(`結果ファイル取得エラー: ${error}`);
    return { success: false, error: error.toString() };
  }
}

/**
 * JSONL形式の結果を解析
 * @param {string} jsonlContent JSONL形式のコンテンツ
 * @returns {Array} 解析された結果配列
 */
function parseJsonlResults(jsonlContent) {
  try {
    const results = [];
    const lines = jsonlContent.split('\n').filter(line => line.trim());
    
    for (const line of lines) {
      try {
        const resultData = JSON.parse(line);
        
        // custom_idからrecord_idを抽出 (company_123 → 123)
        const customId = resultData.custom_id;
        const recordId = customId ? customId.replace('company_', '') : null;
        
        if (!recordId) {
          console.warn(`custom_idからrecord_id抽出失敗: ${customId}`);
          continue;
        }
        
        // レスポンスステータス確認
        if (resultData.response && resultData.response.status_code === 200) {
          const responseBody = resultData.response.body;
          
          if (responseBody && responseBody.choices && responseBody.choices.length > 0) {
            const content = responseBody.choices[0].message.content;
            
            // JSONコンテンツの妥当性チェック
            try {
              const parsedContent = JSON.parse(content); // JSONとして解析してオブジェクトに変換
              
              // form_elementsが空または無効の場合は失敗扱い
              if (isFormElementsEmpty(parsedContent.form_elements)) {
                console.warn(`会社ID ${recordId}: form_elementsが空のため失敗扱い`);
                results.push({
                  record_id: parseInt(recordId),
                  custom_id: customId,
                  status: 'empty_form_elements',
                  error: BATCH_CONFIG.ERROR_MESSAGES.EMPTY_FORM_ELEMENTS
                });
              } else {
                // プレースホルダー変換を適用
                const convertedContent = convertSampleDataToPlaceholders(parsedContent);
                
                results.push({
                  record_id: parseInt(recordId),
                  instruction_json: convertedContent, // プレースホルダー変換済みオブジェクトとして格納
                  custom_id: customId,
                  status: 'success'
                });
              }
              
            } catch (jsonError) {
              console.warn(`会社ID ${recordId}: レスポンスがJSON形式ではありません`);
              results.push({
                record_id: parseInt(recordId),
                custom_id: customId,
                status: 'invalid_json',
                error: BATCH_CONFIG.ERROR_MESSAGES.INVALID_JSON
              });
            }
          } else {
            console.warn(`会社ID ${recordId}: レスポンスにchoicesがありません`);
            results.push({
              record_id: parseInt(recordId),
              custom_id: customId,
              status: 'no_choices',
              error: BATCH_CONFIG.ERROR_MESSAGES.NO_CHOICES
            });
          }
        } else {
          console.warn(`会社ID ${recordId}: APIレスポンスエラー`);
        }
        
      } catch (lineError) {
        console.error(`JSONL行解析エラー: ${lineError} - 行: ${line}`);
      }
    }
    
    console.log(`JSONL解析完了: ${results.length}件の有効な結果`);
    return results;
    
  } catch (error) {
    console.error(`JSONL解析エラー: ${error}`);
    return [];
  }
}

/**
 * 結果をcompaniesテーブルに更新（バルク最適化版）
 * @param {Array} results 結果配列
 * @returns {Object} 更新結果
 */
function updateCompaniesWithResults(results) {
  try {
    if (!results || results.length === 0) {
      return {
        success: true,
        updated_count: 0,
        message: '更新対象なし'
      };
    }
    
    // 全ての処理済み結果を統合してバルク更新用レコード配列を構築
    const updateRecords = [];
    
    results.forEach(result => {
      if (!result.record_id || !result.status) {
        return; // 無効なレコードをスキップ
      }
      
      const updateRecord = {
        id: result.record_id,
        form_analyzer_queued: null  // 処理完了したレコードは必ずnullにリセット
      };
      
      // 成功した結果の場合は追加フィールドも更新
      if (result.status === 'success' && result.instruction_json) {
        updateRecord.instruction_json = result.instruction_json;
        updateRecord.instruction_valid = null;
      }
      
      updateRecords.push(updateRecord);
    });
    
    if (updateRecords.length === 0) {
      console.log('バルク更新対象レコードがありません');
      return {
        success: true,
        updated_count: 0,
        message: '更新対象レコードなし'
      };
    }
    
    console.log(`companiesバルク更新開始: ${updateRecords.length}件`);
    
    // 新しいバルク更新メソッドを使用
    const bulkResult = bulkUpdateRecords(updateRecords, 'companies', 50);
    
    if (bulkResult.success) {
      console.log(`companiesバルク更新完了: ${bulkResult.updated_count}件`);
      
      // 統計情報を生成
      const successResults = results.filter(r => 
        r.status === 'success' && r.instruction_json
      );
      const queueResetResults = results.filter(r => 
        r.record_id && r.status && 
        ['success', 'empty_form_elements', 'invalid_json', 'no_choices'].includes(r.status)
      );
      
      return {
        success: true,
        updated_count: bulkResult.updated_count,
        error_count: bulkResult.error_count || 0,
        total_results: results.length,
        success_results: successResults.length,
        queue_reset_count: queueResetResults.length
      };
    } else {
      console.error(`companiesバルク更新失敗: ${bulkResult.error}`);
      return {
        success: false,
        updated_count: bulkResult.updated_count || 0,
        error_count: bulkResult.error_count || updateRecords.length,
        total_results: results.length,
        error: bulkResult.error
      };
    }
    
  } catch (error) {
    console.error('companies更新エラー:', error);
    return { 
      success: false, 
      error: error.toString(),
      updated_count: 0
    };
  }
}

/**
 * batch_requestテーブルのステータス更新
 * @param {Array} updates 更新データ配列
 * @returns {Object} 更新結果
 */
function updateBatchRequestStatus(updates) {
  try {
    const supabase = getSupabaseClient();
    
    let updatedCount = 0;
    let errorCount = 0;
    
    for (const update of updates) {
      const updateQuery = `${supabase.url}/rest/v1/batch_request?batch_id=eq.${update.batch_id}`;
      
      // 更新データ構築（completedのみ）
      const updateData = {
        completed: update.completed
      };
      
      // リトライ機能付き更新実行
      const result = supabaseRequestWithRetry(updateQuery, {
        method: 'PATCH',
        headers: supabase.headers,
        payload: JSON.stringify(updateData)
      }, `batch_request更新 (${update.batch_id})`);
      
      if (result.success) {
        updatedCount++;
        console.log(`batch_request更新完了: ${update.batch_id}`);
      } else {
        errorCount++;
        console.error(`batch_request更新失敗: ${update.batch_id} - ${result.error} (リトライ: ${result.retry_attempts || 1}回)`);
      }
    }
    
    console.log(`batch_request更新完了: 成功=${updatedCount}, 失敗=${errorCount}`);
    
    return {
      success: errorCount === 0,
      updated_count: updatedCount,
      error_count: errorCount
    };
    
  } catch (error) {
    console.error('batch_request更新エラー:', error);
    return { success: false, error: error.toString() };
  }
}

/**
 * ★【定期実行用】時間ベースのトリガーから呼び出される関数
 */
function checkBatchResultsFromTrigger() {
  console.log('定期実行トリガーによるバッチ結果チェック開始');
  return checkBatchResults();
}

/**
 * バッチサイズを検証するユーティリティ関数
 * @param {number} lineCount JSONLファイルの行数
 * @param {number} fileSizeBytes ファイルサイズ（バイト）
 * @returns {Object} 検証結果
 */
function validateBatchSize(lineCount, fileSizeBytes) {
  const warnings = [];
  const errors = [];
  
  // 行数チェック
  if (lineCount > BATCH_CONFIG.MAX_BATCH_LINES) {
    errors.push(`バッチファイルの行数が上限を超過しています: ${lineCount} > ${BATCH_CONFIG.MAX_BATCH_LINES}`);
  } else if (lineCount > BATCH_CONFIG.RECOMMENDED_BATCH_SIZE) {
    warnings.push(`バッチサイズが推奨値を超過しています: ${lineCount} > ${BATCH_CONFIG.RECOMMENDED_BATCH_SIZE}。完了率向上のため分割を検討してください。`);
  }
  
  // ファイルサイズチェック
  const fileSizeMB = fileSizeBytes / (1024 * 1024);
  if (fileSizeMB > BATCH_CONFIG.MAX_BATCH_SIZE_MB) {
    errors.push(`バッチファイルサイズが上限を超過しています: ${fileSizeMB.toFixed(2)}MB > ${BATCH_CONFIG.MAX_BATCH_SIZE_MB}MB`);
  }
  
  return {
    valid: errors.length === 0,
    warnings: warnings,
    errors: errors,
    line_count: lineCount,
    file_size_mb: fileSizeMB,
    recommendations: {
      max_lines: BATCH_CONFIG.MAX_BATCH_LINES,
      recommended_lines: BATCH_CONFIG.RECOMMENDED_BATCH_SIZE,
      max_size_mb: BATCH_CONFIG.MAX_BATCH_SIZE_MB
    }
  };
}

/**
 * ★【テスト用】手動バッチ結果チェック
 */
function testBatchResultsCheck() {
  console.log('=== テスト用バッチ結果チェック実行 ===');
  const result = checkBatchResults();
  console.log('テスト実行結果:', result);
  return result;
}

/**
 * 設定確認（BatchResultHandler用）
 */
function checkBatchResultHandlerConfiguration() {
  const properties = PropertiesService.getScriptProperties().getProperties();
  const requiredKeys = ['GROQ_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY'];
  
  console.log('=== BatchResultHandler 設定確認 ===');
  requiredKeys.forEach(key => {
    const hasValue = properties[key] ? 'OK' : 'MISSING';
    console.log(`${key}: ${hasValue}`);
  });
  
  // Supabase接続テスト
  try {
    const testResult = testSupabaseConnection();
    console.log('Supabase接続テスト:', testResult.success ? 'OK' : `FAILED - ${testResult.error}`);
  } catch (error) {
    console.log('Supabase接続テスト: ERROR -', error);
  }
  
  return properties;
}

/**
 * 処理統計表示（BatchResultHandler用）
 */
function getBatchResultsStatistics() {
  try {
    const supabase = getSupabaseClient();
    
    // batch_requestテーブルの統計
    const query = `${supabase.url}/rest/v1/batch_request?select=*&limit=1000`;
    
    const response = UrlFetchApp.fetch(query, {
      method: 'GET',
      headers: supabase.headers,
      muteHttpExceptions: true
    });
    
    if (response.getResponseCode() === 200) {
      const data = JSON.parse(response.getContentText());
      
      const stats = {
        total_batches: data.length,
        completed_true: data.filter(item => item.completed === true).length,
        completed_false: data.filter(item => item.completed === false).length,
        pending: data.filter(item => item.completed === null).length,
        requested: data.filter(item => item.requested === true).length,
        
        // 追加統計
        total_results: data.reduce((sum, item) => sum + (item.total_results || 0), 0),
        companies_updated: data.reduce((sum, item) => sum + (item.companies_updated || 0), 0),
        companies_failed: data.reduce((sum, item) => sum + (item.companies_failed || 0), 0)
      };
      
      console.log('=== Batch Results 統計 ===');
      console.log(`総バッチ数: ${stats.total_batches}`);
      console.log(`完了 (成功): ${stats.completed_true}`);
      console.log(`完了 (失敗): ${stats.completed_false}`);
      console.log(`未完了: ${stats.pending}`);
      console.log(`リクエスト済み: ${stats.requested}`);
      console.log(`総結果数: ${stats.total_results}`);
      console.log(`企業更新成功: ${stats.companies_updated}`);
      console.log(`企業更新失敗: ${stats.companies_failed}`);
      
      return stats;
    } else if (response.getResponseCode() === 404) {
      console.log('batch_requestテーブルが存在しません');
      return { error: 'batch_requestテーブルが存在しません' };
    } else {
      console.error('統計取得エラー:', response.getContentText());
      return { error: response.getContentText() };
    }
    
  } catch (error) {
    console.error('統計取得エラー:', error);
    return { error: error.toString() };
  }
}