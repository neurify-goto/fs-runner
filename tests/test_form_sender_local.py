#!/usr/bin/env python3
"""
Form Sender ローカルテスト実行スクリプト
Mac環境でGUIモードでの動作確認用

使用方法:
    cd /Users/taikigoto/form_sales/fs-runner
    python tests/test_form_sender_local.py

機能:
- 引数設定不要で即座に実行可能
- GUI モード強制 (--headless false)
- targeting_id=1で実行
- テスト用設定ファイル自動生成
"""

import argparse
import json
import os
import sys
import tempfile
import subprocess
from pathlib import Path

# 'src' 配下のモジュールを参照できるように先に追加
try:
    sys.path.insert(0, str((Path(__file__).parent.parent) / "src"))
except Exception:
    pass

# ログサニタイザー（機密情報のマスク）
try:
    from form_sender.security.log_sanitizer import LogSanitizer  # type: ignore
except Exception:
    LogSanitizer = None  # フォールバック: サニタイズなし

# .env file loading
try:
    from dotenv import load_dotenv
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

# Supabase client availability
try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except Exception:
    SUPABASE_AVAILABLE = False

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# テストデータをインポート
try:
    from tests.data.test_client_data import create_test_client_config
except ImportError:
    print("ERROR: テストデータファイルが見つかりません")
    print("tests/data/test_client_data.py が存在することを確認してください")
    sys.exit(1)


# サニタイズ付き出力
_SANITIZER = LogSanitizer() if LogSanitizer else None

def _safe_print(message: str) -> None:
    try:
        text = str(message)
        if _SANITIZER:
            text = _SANITIZER.sanitize_string(text)
        print(text)
    except Exception:
        # サニタイズ障害時はそのまま出力（テスト用途のため継続）
        print(message)

def _log_memory_usage(phase: str) -> None:
    try:
        import psutil  # type: ignore
        mem_mb = psutil.Process().memory_info().rss / 1024 / 1024
        _safe_print(f"💾 Memory usage ({phase}): {mem_mb:.1f} MB")
    except Exception:
        # 依存が無い場合等は黙ってスキップ
        pass


def create_test_config_file(company_id=None):
    """
    テスト用のクライアント設定ファイルを/tmp/に作成
    
    Args:
        company_id (int, optional): 指定企業ID。Noneの場合はテストデータを使用
    
    Returns:
        str: 作成した設定ファイルのパス
    """
    # テスト設定データを取得
    config_data = create_test_client_config(company_id)
    
    # tests/tmp/にテストファイルを作成
    tests_tmp_dir = project_root / "tests" / "tmp"
    tests_tmp_dir.mkdir(parents=True, exist_ok=True)  # ディレクトリが存在しない場合は作成
    config_file_path = tests_tmp_dir / "client_config_test.json"
    
    try:
        # JSONファイル作成
        with open(config_file_path, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        
        # ファイル権限を600に設定
        os.chmod(config_file_path, 0o600)
        
        _safe_print(f"✅ テスト設定ファイル作成: {config_file_path}")
        _safe_print("📄 設定内容:")
        # 機密値は必ずマスク
        _safe_print("   - Company: ***COMPANY_REDACTED***")
        _safe_print("   - Sender: ***NAME_REDACTED***")
        _safe_print("   - Subject: ***MESSAGE_REDACTED***")
        _safe_print(f"   - Max Daily Sends: {config_data['targeting']['max_daily_sends']}")
        
        return str(config_file_path)
        
    except Exception as e:
        _safe_print(f"❌ 設定ファイル作成エラー: {e}")
        sys.exit(1)


def load_env_variables():
    """
    .envファイルから環境変数を読み込み
    
    Returns:
        dict: 環境変数辞書
    """
    env_vars = os.environ.copy()
    
    # .envファイルの読み込み
    env_file_path = project_root / ".env"
    
    if DOTENV_AVAILABLE and env_file_path.exists():
        _safe_print("📄 .envファイルから環境変数を読み込み中...")
        load_dotenv(env_file_path)
        
        # 必要な環境変数を取得
        required_vars = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]
        loaded_vars = []
        
        for var in required_vars:
            value = os.getenv(var)
            if value:
                env_vars[var] = value
                loaded_vars.append(var)
                _safe_print(f"  ✅ {var}: 読み込み完了")
            else:
                _safe_print(f"  ❌ {var}: .envファイルに見つかりません")
        
        if loaded_vars:
            _safe_print(f"✅ 環境変数読み込み完了: {len(loaded_vars)}個")
        else:
            _safe_print("⚠️  必要な環境変数が.envファイルに見つかりませんでした")
            
    elif not DOTENV_AVAILABLE:
        _safe_print("⚠️  python-dotenvがインストールされていません")
        _safe_print("   pip install python-dotenv")
        _safe_print("   環境変数を手動で設定するか、dotenvをインストールしてください")
        
    elif not env_file_path.exists():
        _safe_print(f"⚠️  .envファイルが見つかりません: {env_file_path}")
        _safe_print("   環境変数を手動で設定してください")
    
    # 開発モードを設定（テスト実行時）
    env_vars["DEVELOPMENT_MODE"] = "true"
    _safe_print("✅ DEVELOPMENT_MODE=true を設定")
    
    return env_vars

def _queue_has_pending(targeting_id: int) -> bool:
    """send_queue に当日・指定targetingの pending があるか確認"""
    if not SUPABASE_AVAILABLE:
        return False
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        return False
    from datetime import datetime, timedelta, timezone
    jst = timezone(timedelta(hours=9))
    date_jst = datetime.now(jst).date().isoformat()
    sb = create_client(url, key)
    try:
        resp = (
            sb.table('send_queue')
              .select('id')
              .eq('target_date_jst', date_jst)
              .eq('targeting_id', targeting_id)
              .eq('status', 'pending')
              .limit(1)
              .execute()
        )
        return bool(resp.data)
    except Exception:
        return False


def run_form_sender_test(company_id=None, nolimit: bool = False):
    """
    Form Senderをローカルテストモードで実行
    
    Args:
        company_id (int, optional): 指定企業ID。Noneの場合はランダム取得
    """
    # 環境変数読み込み
    env_vars = load_env_variables()
    _safe_print("")
    
    # テスト設定ファイル作成
    config_file = create_test_config_file(company_id)
    
    # 実行コマンド構築（新Runner）
    form_sender_script = project_root / "src" / "form_sender_runner.py"

    if not form_sender_script.exists():
        print(f"❌ Form Sender Runner が見つかりません: {form_sender_script}")
        sys.exit(1)

    # ヘッドレス切替: 既定=GUI, --nolimit=ヘッドレス
    headless_flag = "true" if nolimit else "false"
    cmd = [
        sys.executable,
        str(form_sender_script),
        "--targeting-id", "1",
        "--config-file", config_file,
        "--headless", headless_flag,
    ]

    # 既定: 1ワーカー・1送信のみで終了（send_queueからclaim）
    if not nolimit:
        cmd += ["--num-workers", "1", "--max-processed", "1"]
        if company_id is None:
            # send_queue に pending が無い場合は案内して終了
            if not _queue_has_pending(1):
                _safe_print("⚠️ send_queue に当日 pending が見つかりませんでした（targeting_id=1）")
                _safe_print("   GAS で buildSendQueueForAllTargetings() を実行して当日キューを作成してください")
                _safe_print("   もしくは --company-id で単体企業を指定して実行できます")
                return
        else:
            # 明示指定があればキューを使わず単体処理
            cmd += ["--company-id", str(company_id)]
    else:
        # --nolimit: 4ワーカー・送信数無制限（キュー駆動推奨）
        cmd += ["--num-workers", "4"]
        if company_id is not None:
            cmd += ["--company-id", str(company_id)]

    # 既定で quiet。--show-mapping-logs 指定時のみ解除を伝播（Runnerは参照しないが互換維持）
    if '--show-mapping-logs' in sys.argv:
        cmd.append('--show-mapping-logs')
        env_vars['QUIET_MAPPING_LOGS'] = '0'
    
    _safe_print(f"ℹ️   Runnerテスト: send_queueを使用せず --company-id 指定時は直接処理で検証")
    
    if company_id is not None:
        _safe_print(f"🎯 企業指定モード: companies.id = {company_id}")
        _safe_print(f"    - 指定された企業のみを処理対象とします")
    else:
        _safe_print(f"📥 キューモード: send_queue から pending を1件claim (targeting_id=1)")
    
    _safe_print(f"🚀 Form Sender ローカルテスト実行開始")
    if nolimit:
        _safe_print("🧩 実行モード: 制限なし（件数・ワーカー抑制なし。既定設定に従う）")
    _safe_print(f"📋 実行コマンド: {' '.join(cmd)}")
    if headless_flag == "false":
        _safe_print(f"🖥️  GUI モード (ブラウザウィンドウが表示されます)")
    else:
        _safe_print(f"🤖 ヘッドレスモード (--nolimit)")
    if nolimit:
        _safe_print("📦 テスト設定: 抑制なし（バッチ・ワーカーは設定/実装に従う）")
    else:
        _safe_print(f"📦 テスト設定: 最大1件のみ送信処理")
        _safe_print(f"⏹️  1件完了後に自動終了します")
    _safe_print("")
    _safe_print("=" * 60)
    _safe_print("")
    
    try:
        # 実行タイムアウト（環境変数で上書き可、既定600秒）
        timeout_s = int(os.getenv('FORM_SENDER_LOCAL_TIMEOUT', '600'))
        _log_memory_usage("before run")
        # Form Sender実行（環境変数を渡す）
        subprocess.run(cmd, check=True, cwd=str(project_root), env=env_vars, timeout=timeout_s)
        _log_memory_usage("after run")
        
    except subprocess.TimeoutExpired:
        _safe_print(f"⏱️  タイムアウトによりForm Senderを中断しました（{timeout_s}秒）")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        _safe_print(f"❌ Form Sender実行エラー (終了コード: {e.returncode})")
        sys.exit(1)
    except KeyboardInterrupt:
        _safe_print("\n⏹️  ユーザーによって中断されました")
    finally:
        # テンポラリファイルのクリーンアップ
        try:
            if os.path.exists(config_file):
                os.remove(config_file)
                _safe_print(f"🧹 テスト設定ファイル削除: {config_file}")
        except Exception as e:
            _safe_print(f"⚠️  設定ファイル削除エラー: {e}")


def main():
    """
    メイン実行関数
    """
    # コマンドライン引数パース
    parser = argparse.ArgumentParser(
        description='Form Sender ローカルテスト実行スクリプト',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 従来通りのランダム取得モード
  python tests/test_form_sender_local.py
  
  # 特定企業(ID=123)を指定して実行
  python tests/test_form_sender_local.py --company-id 123
  
  # マッピング関連ログを表示（既定は抑制）
  python tests/test_form_sender_local.py --show-mapping-logs

  # 件数・ワーカー抑制なし（Runner本番相当：4ワーカー・上限なし）
  python tests/test_form_sender_local.py --nolimit
        """
    )
    parser.add_argument(
        '--company-id', 
        type=int, 
        help='処理対象企業ID (companies.id)。未指定時はランダム取得'
    )
    parser.add_argument(
        '--show-mapping-logs',
        action='store_true',
        help='マッピング関連ログ（INFO/DEBUG）を表示（既定は抑制）'
    )
    parser.add_argument(
        '--nolimit',
        action='store_true',
        help='件数・ワーカーの抑制を解除し、Runner本番相当（4ワーカー・上限なし）で実行する'
    )
    
    args = parser.parse_args()
    
    _safe_print("🧪 Form Sender ローカルテスト")
    _safe_print("=" * 40)
    
    # 前提条件チェック
    if not project_root.exists():
        _safe_print(f"❌ プロジェクトルートが見つかりません: {project_root}")
        sys.exit(1)
    
    # 必要な依存関係の簡易チェック
    try:
        import playwright
        _safe_print("✅ Playwright: OK")
    except ImportError:
        _safe_print("❌ Playwright がインストールされていません")
        _safe_print("   pip install playwright")
        _safe_print("   playwright install chromium")
        sys.exit(1)
    
    try:
        import supabase
        _safe_print("✅ Supabase: OK")
    except ImportError:
        _safe_print("❌ Supabase クライアントがインストールされていません")
        _safe_print("   pip install supabase")
        sys.exit(1)
    
    if DOTENV_AVAILABLE:
        _safe_print("✅ python-dotenv: OK")
    else:
        _safe_print("⚠️  python-dotenv: Not installed (環境変数手動設定が必要)")
    
    _safe_print("✅ 依存関係チェック完了")
    _safe_print("")
    
    # 環境変数は実行時に.envから読み込むため、ここでは警告のみ
    _safe_print("ℹ️  環境変数は実行時に.envファイルから自動読み込みされます")
    
    # 企業ID指定の場合は追加の情報を表示
    if args.company_id:
        _safe_print(f"🎯 企業ID指定: {args.company_id}")
        _safe_print("ℹ️  指定企業のデータをSupabaseから取得してテスト実行します")
    else:
        _safe_print("📥 キューモード（既定）")
        _safe_print("ℹ️  send_queue から targeting_id=1 の pending をclaimします")

    # ログモード案内（マッピング関連のみ）
    if args.show_mapping_logs:
        _safe_print("📝 ログモード: マッピング関連=詳細表示 (INFO/DEBUG 出力)")
    else:
        _safe_print("📝 ログモード: マッピング関連=QUIET (INFO/DEBUG 抑制・既定)")

    _safe_print("")
    
    # テスト実行
    run_form_sender_test(
        args.company_id if args.company_id else None,
        nolimit=args.nolimit,
    )


if __name__ == "__main__":
    main()
