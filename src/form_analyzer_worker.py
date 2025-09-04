
#!/usr/bin/env python3
"""
Form Analyzer Worker Entry Point (GitHub Actions版)

企業フォームページを解析し、営業禁止検出とプロンプト生成を行うワーカーのエントリーポイント。
"""

from form_analyzer.main import run_main

if __name__ == '__main__':
    run_main()
