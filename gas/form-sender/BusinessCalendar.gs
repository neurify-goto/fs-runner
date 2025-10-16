/**
 * 祝日・営業時間判定などのビジネスカレンダー関連ユーティリティ
 */

function isBusinessDayJst_(jstDate) {
  try {
    const dow = jstDate.getDay();
    if (dow === 0 || dow === 6) return false;
    const isHoliday = isJapanHolidayJst_(jstDate);
    if (isHoliday === true) return false;
    if (isHoliday === null) {
      console.warn('祝日判定に失敗したため営業日として扱いました（フォールバック）');
      return true;
    }
    return true;
  } catch (e) {
    if (e && e.name === 'TypeError') {
      console.error('Date object invalid in isBusinessDayJst_:', e);
    } else {
      console.error('Unexpected error in isBusinessDayJst_:', e);
    }
    return true;
  }
}

function isJapanHolidayJst_(jstDate) {
  try {
    const cal = CalendarApp.getCalendarById(CONFIG.HOLIDAY_CALENDAR_ID);
    if (!cal) {
      console.warn('日本の祝日カレンダーを取得できませんでした。祝日回避は無効になります。');
      return null;
    }
    const dayStr = Utilities.formatDate(jstDate, 'Asia/Tokyo', 'yyyy-MM-dd');

    if (__HOLIDAY_CACHE.hasOwnProperty(dayStr)) {
      return __HOLIDAY_CACHE[dayStr];
    }

    const startJst = new Date(`${dayStr}T00:00:00+09:00`);
    const endJst = new Date(startJst.getTime() + CONFIG.MILLISECONDS_PER_DAY);
    const events = cal.getEvents(startJst, endJst);
    const isHoliday = !!(events && events.some(function(ev) { return ev.isAllDayEvent(); }));
    __HOLIDAY_CACHE[dayStr] = isHoliday;
    return isHoliday;
  } catch (e) {
    if (e && e.name === 'TypeError') {
      console.error('Date handling error in isJapanHolidayJst_:', e);
    } else {
      console.error('Unexpected error in isJapanHolidayJst_:', e);
    }
    return null;
  }
}

function isWithinBusinessHours(targetingConfig) {
  try {
    const now = new Date();
    const jstNow = new Date(now.getTime() + CONFIG.JST_OFFSET);

    const jsDay = jstNow.getDay();
    const currentDayOfWeek = (jsDay === 0) ? 6 : jsDay - 1;
    const allowedDays = targetingConfig.send_days_of_week || [0, 1, 2, 3, 4];

    if (isJapanHolidayJst_(jstNow)) {
      console.log('本日は日本の祝日のため非営業日扱い: 処理をスキップ');
      return false;
    }

    if (!allowedDays.includes(currentDayOfWeek)) {
      console.log(`営業日ではありません: 現在=${currentDayOfWeek}, 許可=${allowedDays}`);
      return false;
    }

    const currentHour = jstNow.getHours();
    const currentMinute = jstNow.getMinutes();
    const currentTimeMinutes = currentHour * 60 + currentMinute;

    const startTime = targetingConfig.send_start_time || '08:00';
    const endTime = targetingConfig.send_end_time || '19:00';

    const startTimeMinutes = parseTimeToMinutes(startTime);
    const endTimeMinutes = parseTimeToMinutes(endTime);

    const isWithinTime = currentTimeMinutes >= startTimeMinutes && currentTimeMinutes <= endTimeMinutes;

    console.log(`時間帯チェック: 現在=${formatMinutesToTime(currentTimeMinutes)}, 営業=${startTime}-${endTime}, 範囲内=${isWithinTime}`);

    return isWithinTime;
  } catch (error) {
    console.error(`営業時間チェックエラー: ${error.message}`);
    return false;
  }
}

function parseTimeToMinutes(timeString) {
  const [hours, minutes] = timeString.split(':').map(Number);
  return hours * 60 + minutes;
}

function formatMinutesToTime(minutes) {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}`;
}
