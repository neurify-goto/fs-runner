/**
 * トリガー管理および次回実行時刻計算ロジック
 */

function deleteFormSenderTriggers() {
  try {
    console.log('form-sender用トリガーの削除を開始');

    const triggers = ScriptApp.getProjectTriggers();
    let deletedCount = 0;

    triggers.forEach(trigger => {
      const handlerFunction = trigger.getHandlerFunction();

      if (handlerFunction === 'startFormSenderFromTrigger') {
        ScriptApp.deleteTrigger(trigger);
        deletedCount++;
        console.log(`トリガー削除: ${handlerFunction}, 種類: ${trigger.getTriggerSource()}`);
      }
    });

    console.log(`form-sender用トリガー削除完了: ${deletedCount}件削除`);
    return {
      success: true,
      deleted_count: deletedCount,
      message: `${deletedCount}件のトリガーを削除しました`
    };

  } catch (error) {
    console.error(`トリガー削除エラー: ${error.message}`);
    return {
      success: false,
      error: error.message
    };
  }
}

function createSpecificTimeTrigger(executeDateTime) {
  try {
    console.log(`特定日時トリガー作成開始: ${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);

    const now = new Date();
    if (executeDateTime <= now) {
      throw new Error(`指定日時が過去です: ${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);
    }

    const trigger = ScriptApp.newTrigger('startFormSenderFromTrigger')
      .timeBased()
      .at(executeDateTime)
      .create();

    console.log(`特定日時トリガー作成完了: ID=${trigger.getUniqueId()}, 実行予定=${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);

    return {
      success: true,
      trigger_id: trigger.getUniqueId(),
      execute_at: executeDateTime.toISOString(),
      execute_at_jst: executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'}),
      message: `${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}に実行予定のトリガーを作成しました`
    };

  } catch (error) {
    console.error(`特定日時トリガー作成エラー: ${error.message}`);
    return {
      success: false,
      error: error.message,
      execute_at: executeDateTime ? executeDateTime.toISOString() : null
    };
  }
}

function createSpecificTimeTriggerFor(handlerFunction, executeDateTime) {
  try {
    console.log(`特定日時トリガー作成開始(handler=${handlerFunction}): ${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);
    const now = new Date();
    if (executeDateTime <= now) {
      throw new Error(`指定日時が過去です: ${executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);
    }
    const trigger = ScriptApp.newTrigger(handlerFunction)
      .timeBased()
      .at(executeDateTime)
      .create();
    console.log(`特定日時トリガー作成完了(handler=${handlerFunction}): ID=${trigger.getUniqueId()}`);
    return {
      success: true,
      trigger_id: trigger.getUniqueId(),
      execute_at: executeDateTime.toISOString(),
      execute_at_jst: executeDateTime.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'}),
      handler: handlerFunction
    };
  } catch (error) {
    console.error(`特定日時トリガー作成エラー(handler=${handlerFunction}): ${error.message}`);
    return { success: false, error: error.message, handler: handlerFunction };
  }
}

function getNextExecutionTime() {
  const jstNow = new Date();

  let candidate = new Date(jstNow.getTime() + CONFIG.MILLISECONDS_PER_DAY);
  candidate.setMinutes(0, 0, 0);

  let pushedDays = 0;
  let iter = 0;

  while (iter < CONFIG.MAX_SKIP_DAYS) {
    const dow = candidate.getDay();
    if (dow === 0 || dow === 6) {
      candidate = new Date(candidate.getTime() + CONFIG.MILLISECONDS_PER_DAY);
      pushedDays += 1;
      iter += 1;
      continue;
    }

    const holiday = isJapanHolidayJst_(candidate);
    if (holiday === true) {
      candidate = new Date(candidate.getTime() + CONFIG.MILLISECONDS_PER_DAY);
      pushedDays += 1;
      iter += 1;
      continue;
    }

    break;
  }
  if (iter >= CONFIG.MAX_SKIP_DAYS) {
    console.warn(`${CONFIG.MAX_SKIP_DAYS}日以上の連続非営業日/判定失敗を検出。強制的に翌日設定で続行（週末/祝日再検査は打ち切り）`);
  }

  const dowFinal = candidate.getDay();
  if (dowFinal === 6) {
    candidate = new Date(candidate.getTime() + 2 * CONFIG.MILLISECONDS_PER_DAY);
    pushedDays += 2;
  } else if (dowFinal === 0) {
    candidate = new Date(candidate.getTime() + CONFIG.MILLISECONDS_PER_DAY);
    pushedDays += 1;
  }

  const dayNames = ['日曜日', '月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日'];
  const currentDayName = dayNames[jstNow.getDay()];
  const nextDayName = dayNames[candidate.getDay()];

  const reason = pushedDays === 0
    ? '翌日が営業日のためそのまま設定'
    : `非営業日（週末/祝日）を ${pushedDays} 日スキップ（上限=${CONFIG.MAX_SKIP_DAYS}）`;

  console.log(`次回実行時刻計算（祝日/週末回避+上限付き）: 現在=${currentDayName} ${jstNow.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}, 次回=${nextDayName} ${candidate.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'})}`);
  console.log(`回避理由: ${reason}`);

  return candidate;
}

function createNextDayTrigger() {
  try {
    console.log('次回トリガー作成開始（土日回避機能付き）');

    const nextExecutionTime = getNextExecutionTime();
    const result = createSpecificTimeTrigger(nextExecutionTime);

    if (result.success) {
      console.log(`次回トリガー作成成功: ${result.execute_at_jst}`);
    } else {
      console.error(`次回トリガー作成失敗: ${result.error}`);
    }

    return result;

  } catch (error) {
    console.error(`次回トリガー作成エラー: ${error.message}`);
    return {
      success: false,
      error: error.message
    };
  }
}

function listFormSenderTriggers() {
  try {
    console.log('=== form-senderトリガー一覧取得開始 ===');
    const triggers = ScriptApp.getProjectTriggers();
    const formSenderTriggers = triggers.filter(trigger => {
      const h = trigger.getHandlerFunction();
      return h === 'startFormSenderFromTrigger' ||
             h === 'startFormSenderFromTriggerAt7' ||
             h === 'startFormSenderFromTriggerAt13';
    });

    const triggerList = formSenderTriggers.map(trigger => {
      const source = trigger.getTriggerSource();
      let details = {};

      if (source === ScriptApp.TriggerSource.CLOCK) {
        const eventType = trigger.getEventType();
        if (eventType === ScriptApp.EventType.ON_FORM_SUBMIT) {
          details.type = 'unknown';
        } else {
          details.type = 'specific_time';
        }
      }

      return { id: trigger.getUniqueId(), handler: trigger.getHandlerFunction(), source: source.toString(), details: details };
    });

    console.log(`form-senderトリガー数: ${triggerList.length}件`);
    triggerList.forEach(trigger => {
      console.log(`ID: ${trigger.id}, ハンドラー: ${trigger.handler}, ソース: ${trigger.source}, タイプ: ${trigger.details.type}`);
    });

    console.log('=== form-senderトリガー一覧取得完了 ===');

    return {
      success: true,
      trigger_count: triggerList.length,
      triggers: triggerList,
      message: `${triggerList.length}件のform-senderトリガーが設定されています`
    };

  } catch (error) {
    console.error(`トリガー一覧取得エラー: ${error.message}`);
    return {
      success: false,
      error: error.message
    };
  }
}

function deleteTriggersByHandler(handlerFunction) {
  try {
    const triggers = ScriptApp.getProjectTriggers();
    let deleted = 0;
    triggers.forEach(tr => {
      if (tr.getHandlerFunction() === handlerFunction) {
        ScriptApp.deleteTrigger(tr);
        deleted++;
      }
    });
    console.log(`deleteTriggersByHandler: handler=${handlerFunction}, deleted=${deleted}`);
    return { success: true, deleted };
  } catch (e) {
    console.error('deleteTriggersByHandler error:', e.message);
    return { success: false, error: e.message };
  }
}

function getNextWeekdayExecutionTimeAt(hour) {
  const jstNow = new Date();

  let jstTarget = new Date(jstNow);
  jstTarget.setDate(jstTarget.getDate() + 1);
  jstTarget.setHours(hour, 0, 0, 0);

  let pushedDays = 0;
  let iter = 0;

  while (iter < CONFIG.MAX_SKIP_DAYS) {
    const dow = jstTarget.getDay();
    if (dow === 0 || dow === 6) {
      jstTarget = new Date(jstTarget.getTime() + CONFIG.MILLISECONDS_PER_DAY);
      pushedDays += 1;
      iter += 1;
      continue;
    }

    const holiday = isJapanHolidayJst_(jstTarget);
    if (holiday === true) {
      jstTarget = new Date(jstTarget.getTime() + CONFIG.MILLISECONDS_PER_DAY);
      pushedDays += 1;
      iter += 1;
      continue;
    }
    break;
  }
  if (iter >= CONFIG.MAX_SKIP_DAYS) {
    console.warn(`${CONFIG.MAX_SKIP_DAYS}日以上の連続非営業日/判定失敗を検出。強制的に翌営業日扱いで続行（週末/祝日再検査は打ち切り）`);
  }

  const dowFinal = jstTarget.getDay();
  if (dowFinal === 6) {
    jstTarget = new Date(jstTarget.getTime() + 2 * CONFIG.MILLISECONDS_PER_DAY);
    pushedDays += 2;
  } else if (dowFinal === 0) {
    jstTarget = new Date(jstTarget.getTime() + CONFIG.MILLISECONDS_PER_DAY);
    pushedDays += 1;
  }

  if (pushedDays > 0) {
    console.log(`次の営業日(${hour}:00)まで ${pushedDays} 日スキップ（週末/祝日回避、上限=${CONFIG.MAX_SKIP_DAYS}）`);
  }

  return jstTarget;
}

function deleteCurrentFormSenderTrigger() {
  try {
    console.log('実行中form-senderトリガーの削除を開始します');

    const currentTriggers = ScriptApp.getProjectTriggers().filter(trigger =>
      trigger.getHandlerFunction() === 'startFormSenderFromTrigger'
    );

    if (currentTriggers.length === 0) {
      console.log('対象関数のトリガーが見つかりません');
      return { success: true, message: '削除対象なし', deletedCount: 0 };
    }

    console.log(`対象トリガー数: ${currentTriggers.length}個`);

    let bestMatch = null;

    for (const trigger of currentTriggers) {
      try {
        if (trigger.getTriggerSource() === ScriptApp.TriggerSource.CLOCK) {
          const triggerInfo = {
            id: trigger.getUniqueId(),
            source: trigger.getTriggerSource()
          };

          console.log(`トリガー確認: ID=${triggerInfo.id}, Source=${triggerInfo.source}`);

          if (!bestMatch || triggerInfo.id < bestMatch.id) {
            bestMatch = {
              trigger: trigger,
              id: triggerInfo.id
            };
          }
        }
      } catch (error) {
        console.warn(`トリガー情報取得エラー (ID: ${trigger.getUniqueId()}):`, error);
      }
    }

    if (bestMatch) {
      try {
        const triggerId = bestMatch.id;
        ScriptApp.deleteTrigger(bestMatch.trigger);
        console.log(`実行済form-senderトリガーを削除しました: ID=${triggerId}`);

        return {
          success: true,
          deletedCount: 1,
          deletedTriggerId: triggerId,
          message: '実行済form-senderトリガー削除完了'
        };
      } catch (deleteError) {
        console.error('form-senderトリガー削除エラー:', deleteError);
        return {
          success: false,
          error: deleteError.toString(),
          deletedCount: 0
        };
      }
    } else {
      console.log('削除対象の実行済form-senderトリガーを特定できませんでした');
      return {
        success: true,
        message: '削除対象を特定できず',
        deletedCount: 0
      };
    }

  } catch (error) {
    console.error('実行中form-senderトリガー削除エラー:', error);
    return {
      success: false,
      error: error.toString(),
      deletedCount: 0
    };
  }
}
