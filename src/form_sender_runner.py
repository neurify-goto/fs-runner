#!/usr/bin/env python3
"""
Form Sender Runner (自走4ワーカー版)

GASで事前整列された send_queue から原子的に専有し、
IsolatedFormWorker で送信→結果を mark_done RPC で確定する。

想定起動: GitHub Actionsから
  python src/form_sender_runner.py \
    --targeting-id 1 \
    --config-file "/tmp/client_config_*.json" \
    [--num-workers 4] [--headless auto]
"""

import argparse
import asyncio
import json
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Dict, Any, List

from supabase import create_client
import random
import time as _time
import hashlib

from form_sender.worker.isolated_worker import IsolatedFormWorker
from form_sender.security.log_sanitizer import setup_sanitized_logging
from form_sender.utils.error_classifier import ErrorClassifier

# 既存のクライアントデータローダーを再利用
from form_sender_worker import _load_client_data_simple  # type: ignore
from config.manager import get_worker_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = setup_sanitized_logging(__name__)


class _LifecycleOnlyFilter(logging.Filter):
    """ワークフローの標準ログを最小化するためのフィルタ。

    - INFO以上は form_sender.lifecycle のみ通す
    - ERROR以上は全ロガー通す（致命的情報は見える化）
    - それ以外は抑制
    """

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        try:
            if record.levelno >= logging.ERROR:
                return True
            name = record.name or ""
            if record.levelno >= logging.INFO and name.startswith("form_sender.lifecycle"):
                return True
            return False
        except Exception:
            # フィルタで例外が出てもログ消失は避け ERROR のみ通す
            return record.levelno >= logging.ERROR


def _get_lifecycle_logger() -> logging.Logger:
    """開始/完了専用のライフサイクルロガーを作成（INFOを必ず表示）。"""
    log = logging.getLogger("form_sender.lifecycle")
    log.setLevel(logging.INFO)
    # 独自ハンドラー（rootに依存しない）
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        log.addHandler(handler)
        # サニタイズ適用
        setup_sanitized_logging("form_sender.lifecycle")
        # 親へは伝播しない
        log.propagate = False
    return log


def _install_logging_policy_for_ci():
    """CI/GitHub Actions用のログ抑制ポリシーを適用。

    - rootにフィルタを付与して非ライフサイクルのINFO/WARNを抑制
    - ワーカー配下は WARNING 以上でも出ないように（ERRORは許可）
    """
    try:
        if os.getenv('GITHUB_ACTIONS', '').lower() == 'true':
            root = logging.getLogger()
            # 二重追加防止（idで判定）
            if not any(isinstance(f, _LifecycleOnlyFilter) for f in getattr(root, 'filters', [])):
                root.addFilter(_LifecycleOnlyFilter())

            # ノイズが出やすいロガーはERROR以上のみ通す
            for noisy in [
                'form_sender.worker',
                'form_sender.analyzer',
                'playwright', 'urllib3', 'requests', 'supabase'
            ]:
                logging.getLogger(noisy).setLevel(logging.ERROR)
    except Exception:
        pass


def jst_today() -> date:
    return (datetime.utcnow() + timedelta(hours=9)).date()


def jst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))

def jst_utc_bounds(d: date):
    """指定JST日付のUTC境界 (start_utc, end_utc) を返す"""
    jst = timezone(timedelta(hours=9))
    start_jst = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=jst)
    end_jst = start_jst + timedelta(days=1)
    return (start_jst.astimezone(timezone.utc), end_jst.astimezone(timezone.utc))


def _build_supabase_client():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is required')
    # 基本妥当性検証（https強制）
    if not str(url).startswith('https://'):
        raise ValueError('SUPABASE_URL must start with https://')
    return create_client(url, key)

def _extract_max_daily_sends(client_data: Dict[str, Any]) -> Optional[int]:
    """max_daily_sends を安全に抽出（正の整数のみ有効）"""
    try:
        targeting = client_data.get('targeting', {})
        mds = targeting.get('max_daily_sends')
        if mds is None:
            return None
        if isinstance(mds, str):
            s = mds.strip()
            if not s.isdigit():
                return None
            mds = int(s)
        mds = int(mds)
        return mds if mds > 0 else None
    except Exception:
        return None

_SUCC_CACHE: Dict[str, Any] = {}
# 失敗分類の軽量キャッシュ（同一メッセージの連続多発時の負荷抑制）
_CLASSIFY_CACHE: Dict[str, Any] = {}
CLASSIFY_CACHE_MAX_SIZE = 256
CLASSIFY_CACHE_TTL_SEC = 600  # 10分で自然失効（設定で上書き可）


def _get_classify_cache_limits() -> (int, int):
    """config/worker_config.json の runner から制限値を取得（無ければデフォルト）。"""
    try:
        cfg = get_worker_config().get('runner', {})
        max_size = int(cfg.get('classify_cache_max_size', CLASSIFY_CACHE_MAX_SIZE))
        ttl = int(cfg.get('classify_cache_ttl_sec', CLASSIFY_CACHE_TTL_SEC))
        return max(16, max_size), max(60, ttl)
    except Exception:
        return CLASSIFY_CACHE_MAX_SIZE, CLASSIFY_CACHE_TTL_SEC


def _prune_classify_cache(now_ts: float) -> None:
    """TTL とサイズに基づいて簡易的にキャッシュを整理。"""
    try:
        max_size, ttl = _get_classify_cache_limits()
        # TTL 期限切れを最大16件だけ掃除（イテレータで軽掃除）
        import itertools
        removed = 0
        for k in itertools.islice(_CLASSIFY_CACHE.keys(), 64):
            ent = _CLASSIFY_CACHE.get(k)
            if not isinstance(ent, dict):
                _CLASSIFY_CACHE.pop(k, None)
                removed += 1
            elif now_ts - ent.get('ts', 0) > ttl:
                _CLASSIFY_CACHE.pop(k, None)
                removed += 1
            if removed >= 16:
                break
        # サイズ超過なら古い順に削除
        while len(_CLASSIFY_CACHE) > max_size:
            try:
                _CLASSIFY_CACHE.pop(next(iter(_CLASSIFY_CACHE)))
            except StopIteration:
                break
    except Exception:
        # キャッシュ管理失敗は無視
        pass


def _classify_failure_detail(err_msg: Optional[str], add_data: Optional[Dict[str, Any]], error_type: Optional[str]) -> (Optional[Dict[str, Any]], Optional[bool]):
    """失敗詳細を分類し、classify_detail と bot_protection補助フラグを返す。"""
    try:
        http_status = None
        page_content = ''
        is_bot_ctx = False
        if isinstance(add_data, dict):
            ctx = add_data.get('classify_context') or {}
            if isinstance(ctx, dict):
                http_status = ctx.get('http_status')
                page_content = ctx.get('page_content_snippet', '')
                is_bot_ctx = bool(ctx.get('is_bot_detected'))

        # 軽量キャッシュ
        em = (err_msg or '')[:160]
        pc = (page_content or '')[:160]
        raw_key = f"{em}|{http_status}|{error_type or ''}|{pc}"
        cache_key = hashlib.sha1(raw_key.encode('utf-8', errors='ignore')).hexdigest()

        now_ts = _time.time()
        ent = _CLASSIFY_CACHE.get(cache_key)
        max_size, ttl = _get_classify_cache_limits()
        if ent and isinstance(ent, dict) and (now_ts - ent.get('ts', 0) <= ttl):
            detail = ent.get('detail')
        else:
            detail = ErrorClassifier.classify_detail(
                error_message=err_msg or '',
                page_content=page_content or '',
                http_status=http_status,
                context={'error_type_hint': error_type} if error_type else None,
            )
            _CLASSIFY_CACHE[cache_key] = {'detail': detail, 'ts': now_ts}
            _prune_classify_cache(now_ts)

        # bot 補助判定
        bot_flag = None
        try:
            code = detail.get('code') if isinstance(detail, dict) else None
            if code in {'BOT_DETECTED', 'WAF_CHALLENGE'} or is_bot_ctx:
                bot_flag = True
        except Exception:
            pass

        return detail, bot_flag
    except RuntimeError as e:
        logger.warning(f"detail classification failed: {e}")
        return None, None
    except Exception as e:
        logger.debug(f"unexpected classification error: {e}")
        return None, None

def _get_success_count_today_jst(supabase, targeting_id: int, target_date: date) -> int:
    """当日(JST)成功数をUTC境界で集計"""
    try:
        # キャッシュキー（targeting_id + JST日付文字列）
        key = f"{targeting_id}:{target_date.isoformat()}"
        cfg = get_worker_config().get('runner', {})
        cache_sec = int(cfg.get('success_count_cache_seconds', 30))
        now = _time.time()
        ent = _SUCC_CACHE.get(key)
        if ent and (now - ent.get('ts', 0) < cache_sec):
            return int(ent.get('count', 0))

        start_utc, end_utc = jst_utc_bounds(target_date)
        resp = (
            supabase.table('submissions')
            .select('id', count='exact')
            .eq('targeting_id', targeting_id)
            .eq('success', True)
            .gte('submitted_at', start_utc.isoformat().replace('+00:00', 'Z'))
            .lt('submitted_at', end_utc.isoformat().replace('+00:00', 'Z'))
            .execute()
        )
        cnt = getattr(resp, 'count', None)
        if not isinstance(cnt, int):
            data = getattr(resp, 'data', None) or []
            cnt = len(data)
        # 更新
        _SUCC_CACHE[key] = {'count': int(cnt), 'ts': now}
        return int(cnt)
    except Exception:
        return 0


def _resolve_client_config_path(pattern: str) -> str:
    # ワイルドカード対応: 最も新しいファイルを選択
    if '*' in pattern:
        import glob
        files = glob.glob(pattern)
        if not files:
            raise FileNotFoundError(f'No client_config file matches: {pattern}')
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files[0]
    return pattern


def _within_business_hours(client_data: Dict[str, Any]) -> bool:
    try:
        targeting = client_data.get('targeting', {})
        days = targeting.get('send_days_of_week')
        start = targeting.get('send_start_time')  # 'HH:MM'
        end = targeting.get('send_end_time')

        if isinstance(days, str):
            try:
                days = json.loads(days)
            except Exception:
                days = None

        now_jst = jst_now()
        if isinstance(days, list) and len(days) > 0:
            # 0=Mon ... 6=Sun（Python weekday互換）
            if now_jst.weekday() not in days:
                return False

        def to_minutes(s: str) -> int:
            hh, mm = s.split(':')
            return int(hh) * 60 + int(mm)

        if not start or not end:
            return True
        cur_min = now_jst.hour * 60 + now_jst.minute
        return to_minutes(start) <= cur_min < to_minutes(end)
    except Exception:
        return True


async def _process_one(supabase, worker: IsolatedFormWorker, targeting_id: int, client_data: Dict[str, Any], target_date: date, run_id: str, shard_id: Optional[int] = None, fixed_company_id: Optional[int] = None) -> bool:
    """1件専有→処理→確定。処理が無ければFalseを返す。"""
    # 1) claim（固定 company_id が指定された場合は claim をスキップ）
    if fixed_company_id is None:
        params = {
            'p_target_date': str(target_date),
            'p_targeting_id': targeting_id,
            'p_run_id': run_id,
            'p_limit': 1,
            'p_shard_id': shard_id,
        }
        try:
            resp = supabase.rpc('claim_next_batch', params).execute()
            rows = resp.data or []
        except Exception as e:
            logger.error(f"claim_next_batch RPC error: {e}")
            # バックオフの初期値（設定化）
            try:
                runner_cfg = get_worker_config().get('runner', {})
                sleep_s = int(runner_cfg.get('backoff_initial', 2))
            except Exception:
                sleep_s = 2
            await asyncio.sleep(sleep_s)
            return False

        if not rows:
            return False

        company_id = rows[0]['company_id']
        # 処理開始ログ（最小限、IDのみ）
        try:
            wid = getattr(worker, 'worker_id', 0)
            _get_lifecycle_logger().info(
                f"process_start: company_id={company_id}, worker_id={wid}, targeting_id={targeting_id}"
            )
        except Exception:
            pass
    else:
        company_id = int(fixed_company_id)
        # 固定ID指定時も開始を記録
        try:
            wid = getattr(worker, 'worker_id', 0)
            _get_lifecycle_logger().info(
                f"process_start: company_id={company_id}, worker_id={wid}, targeting_id={targeting_id}"
            )
        except Exception:
            pass

    # 2) fetch company
    try:
        comp = supabase.table('companies').select('id, form_url').eq('id', company_id).limit(1).execute()
        if not comp.data:
            raise RuntimeError('company not found')
        company = comp.data[0]
    except Exception as e:
        logger.error(f"fetch company error ({company_id}): {e}")
        # mark failed quickly
        try:
            # 代表コードで詳細分類を付与
            classify_detail = {
                'code': 'NOT_FOUND',
                'category': 'HTTP',
                'retryable': False,
                'cooldown_seconds': 0,
                'confidence': 1.0,
            }
            supabase.rpc('mark_done', {
                'p_target_date': str(target_date),
                'p_targeting_id': targeting_id,
                'p_company_id': company_id,
                'p_success': False,
                'p_error_type': 'NOT_FOUND',
                'p_classify_detail': classify_detail,
                'p_bot_protection': False,
                'p_submitted_at': jst_now().isoformat()
            }).execute()
            # 失敗完了ログ
            try:
                wid = getattr(worker, 'worker_id', 0)
                _get_lifecycle_logger().info(
                    f"process_done: company_id={company_id}, worker_id={wid}, targeting_id={targeting_id}, success=False, reason=NOT_FOUND"
                )
            except Exception:
                pass
        except Exception:
            pass
        return True

    # 3) process via worker
    if not company.get('form_url'):
        # 送信対象外を即確定
        classify_detail = {
            'code': 'NO_FORM_URL',
            'category': 'CONFIG',
            'retryable': False,
            'cooldown_seconds': 0,
            'confidence': 1.0,
        }
        supabase.rpc('mark_done', {
            'p_target_date': str(target_date),
            'p_targeting_id': targeting_id,
            'p_company_id': company_id,
            'p_success': False,
            'p_error_type': 'NO_FORM_URL',
            'p_classify_detail': classify_detail,
            'p_bot_protection': False,
            'p_submitted_at': jst_now().isoformat()
        }).execute()
        # 失敗完了ログ
        try:
            wid = getattr(worker, 'worker_id', 0)
            _get_lifecycle_logger().info(
                f"process_done: company_id={company_id}, worker_id={wid}, targeting_id={targeting_id}, success=False, reason=NO_FORM_URL"
            )
        except Exception:
            pass
        return True

    task_data = {
        'task_id': f'run-{run_id}-{company_id}',
        'task_type': 'process_company',
        'company_data': company,
        'client_data': client_data,
        'targeting_id': targeting_id,
        'worker_id': getattr(worker, 'worker_id', 0)
    }

    try:
        result = await worker.process_company_task(task_data)
    except Exception as e:
        logger.error(f"worker error ({company_id}): {e}")
        result = {
            'status': 'failed',
            'error_type': 'WORKER_ERROR',
            'bot_protection_detected': False,
            'error_message': str(e),
        }

    # WorkerResult dataclass → dict 互換
    status = getattr(result, 'status', None)
    if hasattr(status, 'value'):
        status_val = status.value
    else:
        status_val = result.get('status') if isinstance(result, dict) else None
    is_success = (status_val == 'success')
    error_type = getattr(result, 'error_type', None) if not is_success else None
    bp = getattr(result, 'bot_protection_detected', False)

    # 失敗時は詳細分類を生成（HTTP/WAF/検証 等）
    classify_detail = None
    if not is_success:
        # WorkerResult/dataclass 互換: error_message/追加文脈の取得
        err_msg = getattr(result, 'error_message', None) if not isinstance(result, dict) else result.get('error_message')
        add_data = getattr(result, 'additional_data', None) if not isinstance(result, dict) else result.get('additional_data')
        classify_detail, bot_flag = _classify_failure_detail(err_msg, add_data, error_type)
        if bot_flag:
            bp = True

    # 4) finalize via RPC（固定 company_id の場合も submissions 記録目的で呼ぶ。send_queue更新は0件でも問題なし）
    try:
        # Bot保護が検出されている場合は error_type を BOT_DETECTED に寄せる（優先）
        if not is_success:
            try:
                if bp and (
                    not error_type or (isinstance(error_type, str) and error_type not in {"BOT_DETECTED", "WAF_CHALLENGE"})
                ):
                    error_type = "BOT_DETECTED"
            except Exception:
                pass
        supabase.rpc('mark_done', {
            'p_target_date': str(target_date),
            'p_targeting_id': targeting_id,
            'p_company_id': company_id,
            'p_success': bool(is_success),
            'p_error_type': error_type,
            'p_classify_detail': classify_detail,
            'p_bot_protection': bool(bp),
            'p_submitted_at': jst_now().isoformat()
        }).execute()
        # 成功時は当日成功数キャッシュを無効化（最新値を反映させる）
        if is_success:
            try:
                key = f"{targeting_id}:{target_date.isoformat()}"
                _SUCC_CACHE.pop(key, None)
            except Exception:
                pass
        # 完了ログ（成功/失敗）
        try:
            wid = getattr(worker, 'worker_id', 0)
            if is_success:
                _get_lifecycle_logger().info(
                    f"process_done: company_id={company_id}, worker_id={wid}, targeting_id={targeting_id}, success=True"
                )
            else:
                # 理由はエラー種別のみ（詳細メッセージは出さない）
                _get_lifecycle_logger().info(
                    f"process_done: company_id={company_id}, worker_id={wid}, targeting_id={targeting_id}, success=False, reason={error_type or 'UNKNOWN'}"
                )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"mark_done RPC error ({company_id}): {e}")

    return True


def _worker_entry(worker_id: int, targeting_id: int, config_file: str, headless_opt: Optional[bool], target_date: date, shard_id: Optional[int], run_id: str, max_processed: Optional[int], fixed_company_id: Optional[int]):
    # child process
    try:
        # 子プロセスにも抑制ポリシーを適用
        _install_logging_policy_for_ci()
        supabase = _build_supabase_client()
        worker = IsolatedFormWorker(worker_id=worker_id, headless=headless_opt)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _amain():
            ok = await worker.initialize()
            if not ok:
                logger.error(f"Worker {worker_id}: Playwright init failed")
                return
            client_data = _load_client_data_simple(config_file, targeting_id)
            max_daily = _extract_max_daily_sends(client_data)
            # バックオフ設定（config/worker_config.json → runner）
            try:
                runner_cfg = get_worker_config().get('runner', {})
                backoff_initial = int(runner_cfg.get('backoff_initial', 2))
                backoff_max = int(runner_cfg.get('backoff_max', 60))
            except Exception:
                backoff_initial, backoff_max = 2, 60
            backoff = backoff_initial
            processed = 0
            while True:
                if not _within_business_hours(client_data):
                    await asyncio.sleep(60)
                    continue
                # 当日成功上限（max_daily_sends）をDBのUTC時刻基準でJST境界に合わせて確認
                if max_daily is not None and max_daily > 0:
                    try:
                        success_cnt = _get_success_count_today_jst(supabase, targeting_id, target_date)
                        if success_cnt >= max_daily:
                            logger.info(
                                f"Targeting {targeting_id}: daily success cap reached ({success_cnt}/{max_daily}) - stopping worker {worker_id}"
                            )
                            return
                    except Exception as e:
                        logger.warning(f"daily cap check failed: {e}")
                had_work = await _process_one(supabase, worker, targeting_id, client_data, target_date, run_id, shard_id, fixed_company_id)
                if not had_work:
                    # ジッター付き指数バックオフ（コンボイ緩和）
                    try:
                        jitter_ratio = float(get_worker_config().get('runner', {}).get('backoff_jitter_ratio', 0.2))
                    except Exception:
                        jitter_ratio = 0.2
                    jitter = backoff * jitter_ratio
                    sleep_for = max(0.1, backoff + random.uniform(-jitter, jitter))
                    await asyncio.sleep(sleep_for)
                    backoff = min(backoff * 2, backoff_max)
                else:
                    backoff = backoff_initial
                    processed += 1
                    # テスト用: 規定数に達したら終了
                    if max_processed is not None and processed >= max_processed:
                        return

        loop.run_until_complete(_amain())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Worker {worker_id} fatal: {e}")


def main():
    p = argparse.ArgumentParser(description='Form Sender Runner (4 workers, queue driven)')
    p.add_argument('--targeting-id', type=int, required=True)
    p.add_argument('--config-file', required=True)
    p.add_argument('--num-workers', type=int, default=4)
    p.add_argument('--headless', choices=['true','false','auto'], default='auto')
    p.add_argument('--target-date', type=str, default=None, help='JST date YYYY-MM-DD (default: today JST)')
    p.add_argument('--shard-id', type=int, default=None)
    p.add_argument('--max-processed', type=int, default=None, help='Process this many companies then exit (for local testing)')
    p.add_argument('--company-id', type=int, default=None, help='Process only this company id (bypass queue claim)')
    args = p.parse_args()

    config_path = _resolve_client_config_path(args.config_file)

    headless_opt = None
    if args.headless == 'true':
        headless_opt = True
    elif args.headless == 'false':
        headless_opt = False

    t_date = jst_today() if not args.target_date else date.fromisoformat(args.target_date)

    # spawn workers
    try:
        mp.set_start_method('spawn', force=False)
    except RuntimeError:
        pass

    run_id = os.environ.get('GITHUB_RUN_ID') or f'local-{int(time.time())}'

    # 親プロセスにも抑制ポリシーを適用
    _install_logging_policy_for_ci()

    procs: List[mp.Process] = []
    # company_id 指定時は重複処理を避けるためワーカーは1に制限
    # 1〜4にクランプ（外部からの過大指定を抑止）
    worker_count = 1 if args.company_id is not None else min(4, max(1, args.num_workers))
    for wid in range(worker_count):
        pr = mp.Process(
            target=_worker_entry,
            args=(wid, args.targeting_id, config_path, headless_opt, t_date, args.shard_id, run_id, args.max_processed, args.company_id),
            name=f'fs-worker-{wid}'
        )
        pr.daemon = False
        pr.start()
        procs.append(pr)

    # 親はシグナル待ちして子を巻き取る
    def _term(signum, frame):
        for pr in procs:
            try:
                pr.terminate()
            except Exception:
                pass
        for pr in procs:
            try:
                pr.join(timeout=10)
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _term)
    signal.signal(signal.SIGTERM, _term)

    for pr in procs:
        pr.join()


if __name__ == '__main__':
    main()
