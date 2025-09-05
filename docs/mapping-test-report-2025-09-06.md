# マッピング検証レポート（2025-09-06）

以下の8件（ユニーク companies.id）について、`tests/test_field_mapping_analyzer.py` によりマッピングを実行し、`tests/evaluate_mappings.py` で必須項目充足と不適切マッピングの有無を自動検証しました。すべて OK を確認しています。

## 検証対象（company_id / 成果物パス）

1. company_id: 478142  
   - Mapping Result: test_results/field_mapping_20250906_073714/analysis_result_20250906_073723.json  
   - Page Source:    test_results/field_mapping_20250906_073714/page_source_20250906_073717.html  
2. company_id: 269442  
   - Mapping Result: test_results/field_mapping_20250906_073532/analysis_result_20250906_073650.json  
   - Page Source:    test_results/field_mapping_20250906_073532/page_source_20250906_073544.html  
3. company_id: 506281  
   - Mapping Result: test_results/field_mapping_20250906_073506/analysis_result_20250906_073516.json  
   - Page Source:    test_results/field_mapping_20250906_073506/page_source_20250906_073510.html  
4. company_id: 95435  
   - Mapping Result: test_results/field_mapping_20250906_073158/analysis_result_20250906_073226.json  
   - Page Source:    test_results/field_mapping_20250906_073158/page_source_20250906_073207.html  
5. company_id: 438302  
   - Mapping Result: test_results/field_mapping_20250906_073014/analysis_result_20250906_073026.json  
   - Page Source:    test_results/field_mapping_20250906_073014/page_source_20250906_073017.html  
6. company_id: 519432  
   - Mapping Result: test_results/field_mapping_20250906_072501/analysis_result_20250906_072508.json  
   - Page Source:    test_results/field_mapping_20250906_072501/page_source_20250906_072505.html  
7. company_id: 348251  
   - Mapping Result: test_results/field_mapping_20250906_072337/analysis_result_20250906_072406.json  
   - Page Source:    test_results/field_mapping_20250906_072337/page_source_20250906_072347.html  
8. company_id: 208248  
   - Mapping Result: test_results/field_mapping_20250906_005839/analysis_result_20250906_005853.json  
   - Page Source:    test_results/field_mapping_20250906_005839/page_source_20250906_005843.html  

## 評価指標
- 必須項目（`required_fields_info.required_elements`）の充足: OK（全件）
- 不適切マッピング（ヒューリスティック判定）: なし（全件）

## 実行コマンド（参考）
- 単発実行（headless）:  
  `SUPABASE_URL="https://<your-project>.supabase.co" PLAYWRIGHT_HEADLESS=1 python tests/test_field_mapping_analyzer.py --verbose`
- 自動評価:  
  `python tests/evaluate_mappings.py <analysis_result_*.json ...>`

※ 本レポートでは社名・URLは記載していません（Security/Privacyポリシー準拠）。

