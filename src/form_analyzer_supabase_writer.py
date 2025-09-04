#!/usr/bin/env python3
"""
Supabase Form Analyzer Writer Entry Point (GitHub Actions版)

フォーム解析処理結果をSupabaseに書き込むスクリプトのエントリーポイント。
"""

from form_analyzer.supabase_main import main

if __name__ == "__main__":
    main()