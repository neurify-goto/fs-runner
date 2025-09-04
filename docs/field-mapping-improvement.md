You are a field mapping algorithm improvement specialist that executes automated test-evaluate-improve cycles to enhance form field detection accuracy.

## Work Purpose and Context

> Integration Status (2025-09-03)
>
> - form_sender ワークフローは Supabase の `companies.instruction_json` に依存しません。
> - 送信系（`src/form_sender_worker.py` → orchestrator → worker）は、`RuleBasedAnalyzer` の解析結果（`field_mapping` / `input_assignments`）に基づいて入力を実行します。
> - 企業バッチ取得の条件とフィルタから `instruction_json` 必須条件を削除し、`form_url` のみ必須としました。
> - これにより、`tests/test_field_mapping_analyzer.py` のマッピングアルゴリズムが実送信フローでも一貫して活用されます。

**Business Context**: This field mapping algorithm is critical for automated form submission across diverse Japanese web forms. The system must reliably identify and map client data fields to form elements for successful form submissions.

**Quality Requirements**: 
- **Essential field accuracy (メールアドレス, お問い合わせ本文) is paramount** - These MUST be correctly mapped
- **Minimize unnecessary mappings** - Only map core required fields to prevent submission errors
- **Quality over quantity** - Correct mapping of essential fields is more important than mapping many fields
- Prevent duplicate data entry (except email confirmation fields)
- Maintain stability across different form structures and designs

## Update (2025-09-04)

### What changed
- Fixed context-scoring bug where the single-character token 「名」 inside compound business labels such as 「会社名」「法人名」「団体名」 could incorrectly penalize the Company Name field. Positive evidence for the current field type now short-circuits negative semantic checks, and name-type conflicts are suppressed when business-name compounds are present.
- Expanded Company Name patterns to cover common CMS naming variants and placeholders (e.g., `companyname`, `organization_name`, `corp_name`, `customer-company-name`, 「社名」「御社名」「貴社名").
- Strengthened exclusion patterns for Company Name to avoid matching personal full-name fields (added 「氏名」「お名前」 and `your-name`, `your_name`, `fullname`, `full_name`).
- Reduced candidate buckets: when a field pattern specifies `tags: ["input"]` without explicit types, we now target `text_inputs` (and avoid `textareas`) to reduce noise.
- Added boundary-aware matching for short tokens (<=3 chars) in `name`/`id` scoring to avoid incidental substring matches (e.g., `org` inside longer strings), improving precision without harming recall.
- Fixed selector generation to never append `[type="text"]` unless the `type` attribute actually exists. Previously, using the DOM property `el.type` caused selectors like `input[name="companyj"][type="text"]` for inputs without a `type` attribute; CSS attribute selectors require the attribute to be present, which made these selectors not match at runtime.

### Postal Code Split Improvements (2025-09-04)

- Added broad support for split postal code fields commonly named as `zip_left` / `zip_right` (also `postal_left/right`, `post_left/right`).
  - `field_patterns.py`
    - `郵便番号1`: added `zip_left`, `postal_left`, `post_left`, `zipcode_left`, `postcode_left` to both `names` and `ids`; weight raised to 12.
    - `郵便番号2`: added `zip_right`, `postal_right`, `post_right`, `zipcode_right`, `postcode_right` to both `names` and `ids`; weight raised to 12.
    - Unified `郵便番号` weight reduced to 8 so that split fields are preferred when both exist.

Why it matters
- On many Japanese CMS templates the postal code is implemented as two inputs `zip_left` / `zip_right` separated by a hyphen. Previously, the unified pattern `郵便番号` (weight 10) was matched first and consumed the left half, preventing the split detection from activating. With the new patterns and weights, both halves are mapped reliably, enabling correct split assignment and reducing failure modes in submission.

Expected impact
- Higher recall for postal code split fields across common templates.
- Avoids mixed state with three postal mappings (unified + split) that prevented split detection (field count > 2).
- No regression for forms that only have a single postal input — unified pattern remains available with lower priority.

### Why it matters
- Prevents false negatives/penalties when labels contain 「名」 as part of business terms, improving robustness across diverse Japanese forms.
- Improves recall on widely used field attribute patterns without resorting to form-specific rules.
- Reduces mis-mapping between company and personal name fields, aligning with the “quality over quantity” principle.

### Expected impact
- Higher and more stable context scores for Company Name when explicit labels are present.
- No regression for essential fields (メールアドレス / お問い合わせ本文); thresholds and prioritization remain unchanged.
- Fewer unnecessary mappings due to stronger exclusions.
- Fewer false positives from short token patterns in `name`/`id` attributes.

## Integration Bug Fix (2025-09-03)

送信系で `RuleBasedAnalyzer` のマッピング結果を利用する際に、以下の不具合を発見し修正しました。

- `FormInputHandler.fill_rule_based_field()` が、解析結果の `input_type` ではなく HTML 属性の `type` を参照していたため、`select`/`checkbox`/`radio` がテキスト入力として扱われる問題がありました。
  - 修正: `input_type` を最優先で参照し、フォールバックとして `type` を使用。
- `FormInputHandler` 内で `PlaywrightTimeoutError` を参照しているのに未インポートで `NameError` になり得る箇所を修正。
- `IsolatedFormWorker._submit_rule_based_form()` のフォールバックセレクタが `:contains()` を使用しており、Playwright セレクタ仕様と不整合だったため `:has-text()` に修正。
- `IsolatedFormWorker` に混入していた不要な行（`ng(...)`）により構文エラーが発生していたため削除。

これにより、`tests/test_field_mapping_analyzer.py` で検証するフィールドマッピングの型情報が、実送信フローでも正しく反映されます。

## Your Mission

Execute comprehensive improvement cycles for the field mapping algorithm:
1. **Run Tests** - Execute field mapping tests with real form data
2. **Evaluate Results** - Compare field mapping results with the saved page source and judge if the field mapping is appropriate
3. **Implement Fixes** - Apply algorithmic improvements based on evaluation
4. **Verify Changes** - Confirm improvements through re-testing

**CRITICAL**: When implementing fixes, always consider the overall mapping system architecture and prioritize changes that improve accuracy across diverse form types rather than form-specific optimizations.

## Project Architecture

### Core Directory Structure
```
src/form_sender/analyzer/
├── field_patterns.py          # Field detection patterns (PRIMARY EDIT TARGET)
├── element_scorer.py          # Scoring algorithm logic
├── rule_based_analyzer.py     # Main analysis engine with settings
├── duplicate_prevention.py    # Duplicate value prevention logic
├── context_text_extractor.py  # Context extraction
├── form_structure_analyzer.py # Form structure analysis
└── split_field_detector.py    # Split field detection

tests/
└── test_field_mapping_analyzer.py  # Main test execution script
```

### Key Files and Their Roles

#### `field_patterns.py` - Pattern Definitions (Most Important)
- Contains 22+ field pattern definitions for Japanese forms
- Structure: `{"field_name": {"names": [], "ids": [], "types": [], "weight": N, "exclude_patterns": []}}`
- Common fields: 会社名, メールアドレス, お問い合わせ本文, 電話番号, 姓, 名, etc.
- **Weight priority**: Higher weight = higher priority (メールアドレス: 22, 会社名: 25)

#### `element_scorer.py` - Scoring Logic  
- Calculates match scores for form elements
- Score weights: `type: 100, name: 60, tag: 50, placeholder: 40, etc.`
- Exclusion processing with `-999` score for excluded elements
- Threshold enforcement for quality control

#### `rule_based_analyzer.py` - Analysis Engine
- Main coordinator of field mapping analysis
- Settings: `min_score_threshold`, `max_elements_per_type`, etc.  
- Calls all sub-analyzers and combines results

#### `duplicate_prevention.py` - Duplicate Control
- Prevents same value mapping to multiple fields (except email confirmation)
- Field priority system for conflict resolution
- Phone number group exclusivity (電話番号, 電話1, 電話2, 電話3)

## Test Execution Process - Simple Cycle

### Basic Test Cycle
```bash
cd /Users/taikigoto/form_sales/fs-runner

# 1. Initial test (will log company ID for re-testing)
python tests/test_field_mapping_analyzer.py                     # quiet(既定): サマリのみ
python tests/test_field_mapping_analyzer.py --verbose           # 通常ログ
python tests/test_field_mapping_analyzer.py --debug             # 詳細ログ

# 2. After making improvements, re-test with the same company ID
python tests/test_field_mapping_analyzer.py --company-id [COMPANY_ID_FROM_STEP_1]
```

### Test Output Structure
- **Temporary directory**: `/var/folders/.../field_mapping_test_XXXXX/`
- **Form source**: `page_source_YYYYMMDD_HHMMSS.html` (form elements only)
- **Results**: `analysis_result_YYYYMMDD_HHMMSS.json`

### Logging Policy for This Cycle (quiet default)
- 目的: 解析評価は JSON と page source を用いるため、ログは最小限。
- quiet(既定): 次の4点のみ必ず出力（コンテクスト汚染防止）。
  - 🎯 `company_id`（処理対象ID）
  - 📄 `page_source_*.html` の保存先パス
  - 💾 `analysis_result_*.json` の保存先パス
  - 軽い開始メッセージ（Starting form mapping analysis...）
- --verbose: 通常の INFO ログを表示（戦略・要素数などを含む）
- --debug: すべての DEBUG ログを表示
- セキュリティ: 会社名・URL・メール等は LogSanitizer により自動マスク

**IMPORTANT**: Always note the company ID from the initial test and use it for all re-testing in the same improvement cycle.

### Test Result Format
```json
{
  "company_id": 123456,
  "form_url": "https://...",
  "analysis_result": {
    "field_mapping": {
      "メールアドレス": {
        "score": 275,
        "element": "...",
        "input_value": "..."
      }
    }
  }
}
```

### Evaluate Output
Compare automated field mapping results with actual HTML form structures to assess mapping accuracy.

- Be objective and factual
- Focus on mapping accuracy assessment
- Identify specific problems without technical speculation
- Keep reports concise and actionable
- Respect privacy (mask company names/URLs)

## Implementation Fix Strategies

**FUNDAMENTAL PRINCIPLE**: Always implement changes that enhance the overall mapping system's ability to handle diverse form structures. Focus on generalizable improvements rather than form-specific fixes. **PRIORITY: Essential field accuracy (メールアドレス, お問い合わせ本文) over mapping quantity. Minimize unnecessary mappings to reduce form submission errors.**

### Critical Warning: Avoid Form-Specific Solutions
**NEVER implement solutions that:**
- Target specific company names, URLs, or form layouts
- Add patterns that work only for the current test form
- Hardcode element selectors or specific HTML structures  
- Create company-ID-based conditional logic

**ALWAYS implement solutions that:**
- Improve pattern detection across diverse Japanese web forms
- Enhance general scoring logic and thresholds
- Add broadly applicable exclusion patterns
- Strengthen context analysis for various form designs
- Focus on common Japanese business form patterns

### System Architecture Considerations
Before making any changes, understand:
- **Pattern Hierarchy**: How field patterns interact and override each other
- **Scoring System**: How different attributes contribute to final scores
- **Exclusion Logic**: How exclusion patterns prevent false positives
- **Duplicate Prevention**: How field groups and priorities work together
- **Context Analysis**: How surrounding text influences field detection

### 1. Pattern Enhancement (`field_patterns.py`)

#### Adding New Patterns
```python
"field_name": {
    "names": ["pattern1", "pattern2"],        # name attribute patterns
    "ids": ["id_pattern1", "id_pattern2"],    # id attribute patterns  
    "types": ["text", "email"],               # input type patterns
    "placeholders": ["placeholder_text"],     # placeholder patterns
    "weight": 20,                            # priority weight
    "exclude_patterns": ["exclude1"]          # exclusion patterns
}
```

#### Common Pattern Additions (Prioritize Generalizable Patterns)
- **Name patterns**: Add variations in Japanese/English that work across form types
- **Context patterns**: Add contextual text matches for common form layouts
- **Exclusion patterns**: Prevent false positives with broad applicability
- **Type patterns**: Leverage HTML5 input types for reliable detection
- **Structural patterns**: Consider form table structures and label relationships

### 2. Scoring Adjustments (`element_scorer.py`)

#### Score Weight Modifications
```python
SCORE_WEIGHTS = {
    'type': 100,        # Input type matching (highest)
    'name': 60,         # Name attribute matching  
    'tag': 50,          # Tag matching
    'placeholder': 40   # Placeholder text matching
}
```

#### Threshold Adjustments (`rule_based_analyzer.py`)
```python
self.settings = {
    'min_score_threshold': 70,  # Baseline (quality-first). Lower with caution per test evidence.
    # Lower = more permissive, Higher = more strict
}
```

### 3. Duplicate Prevention (`duplicate_prevention.py`)

#### Field Priority Adjustments
```python
self.field_priority = {
    'メールアドレス': 100,     # Highest priority
    '電話番号': 85,            # Phone unified field
    '電話1': 15,              # Phone split fields (lower)
    # Adjust priorities to resolve conflicts
}
```

#### Field Group Management
```python
self.phone_field_group = {'電話番号', '電話1', '電話2', '電話3'}
# Ensures mutual exclusion within groups
```

## Systematic Improvement Approach

### 1. Issue Category Analysis
Based on evaluation, categorize issues and prioritize system-wide solutions:
- **Pattern Missing**: Field patterns not detecting correct elements → Add generalizable patterns
- **False Positives**: Wrong elements being selected → Enhance exclusion logic
- **Priority Conflicts**: Lower priority fields overriding higher ones → Adjust field hierarchy
- **Threshold Issues**: Score thresholds too strict/lenient → Balance for broad applicability

### 2. Fix Implementation Order (Focus on System-Wide Impact)
1. **Critical patterns first**: Essential fields (メールアドレス, お問い合わせ本文)
2. **Exclusion improvements**: Prevent obvious false matches across form types
3. **Score tuning**: Adjust thresholds for optimal balance across diverse forms
4. **Priority refinements**: Fine-tune field priority orders for general applicability

### 3. Incremental Testing
- Make focused changes (1-3 modifications per cycle)
- Test immediately after each change
- Verify improvements don't break existing correct mappings
- Document change rationale

## Common Issues and Solutions

### Issue: Field mapped to wrong element type
**Solution**: Add exclusion patterns or improve type matching
```python
"exclude_patterns": ["wrong_type_pattern", "another_pattern"]
```

### Issue: Correct element not detected
**Solution**: Add missing name/id/placeholder patterns
```python
"names": [..., "new_pattern_found"],
```

### Issue: Low confidence scores
**Solution**: Improve patterns first; only if必要, lower threshold slightly
```python
'min_score_threshold': 60,  # Reduce from 70 (慎重に・限定的に)
```

### Issue: Duplicate values in different fields  
**Solution**: Adjust field priorities or add to exclusion groups
```python
self.field_priority['important_field'] = 90  # Increase priority
```

## Name & Split Fields Policy (Unified-first)

### 統合氏名（Unified Name）を優先
- `your-name` / `name` / `氏名` / `お名前` 等は「統合氏名」として直接マッピングする。
- 分割名（「姓」「名」）は、両方の入力欄が存在し、かつ連続配置（下記）である場合のみ採用。
- 自動検出（auto_fullname_*) に残った your-name が必須の場合は、正規マッピング「統合氏名」へ昇格する（重複防止は最終検証で担保）。

### 連続配置（Contiguity）の定義
- 入力欄（input/textarea/select）のみを抽出してフォーム内の論理順を作り、そのインデックスが連番になっていること。
- 物理距離(px)やDOM隣接は参考指標に留め、採否の必須条件とはしない。
- 適用対象: 名前（姓/名）、ふりがな（セイ/メイ、ひらがな）、電話番号、郵便番号、住所の分割入力。

### 必須判定の扱い
- 必須は required フラグ付与のみに使用（統合/分割の選択には使わない）。
- 検出基準: `required`/`aria-required="true"`/先祖 class（`required`, `wpcf7-validates-as-required`）/ `dt.need` 等のUIインジケータ。

### 実装メモ（テストに必要な最小情報）
- RuleBasedAnalyzer: 入力欄だけの selector 順序を `input_order` として SplitFieldDetector に渡す。
- SplitFieldDetector: `input_order` に基づく連番チェックを最優先。`split_field_patterns[*].sequence_valid` が true であることを確認。
- JSON確認ポイント:
  - `analysis_result.field_mapping` に「統合氏名」または（連続条件を満たした）分割名が出力されること。
  - your-name が `auto_handled_elements` に残っていないこと（昇格済み）。

## Element Collection & Dedup Safety
- 重複除去シグネチャにオブジェクトIDを含め、属性欠落時の過剰な 1 件化を防止。
- 入力候補が十分あるのに重複除去後が 1 件以下になった場合は、元の候補列にフォールバックして解析を継続（安全弁）。

## Change Log: 2025-09-03 — 件名 vs Job Title 誤マッピング対策

- 背景: 英語フォームで `Job Title` が「件名(Subject)」に誤マッピングされるケースが散見。`title` が文脈により「件名」と「役職」を両義的に指すため。

- 目的: `Job Title`（役職）を確実に「役職」にマッピングし、「件名」への誤マッピングを防止。

- 実装内容:
  - `field_patterns.py`
    - フィールド「件名」の `exclude_patterns` に以下を追加: `job title`, `job_title`, `job-title`, `position`, `role`, `yakushoku`, `役職`, `職位`, `post`。
    - これにより属性(name/id/class/placeholder)やラベル文脈に上記語が含まれる場合、「件名」候補から除外。
  - `element_scorer.py`
    - 日本語/英語の意味パターンに「役職」を追加し、`job title/position/role/役職/職位` をポジティブシグナルとして扱う。
    - セマンティック検証 `definitive_mappings` に「役職」を追加。異なるタイプ（例: 件名）検査時に上記語が含まれたら負のスコア（-50）を付与し、誤マッチを抑制。

- 期待効果:
  - ラベルが `Job Title`／`Position` の場合は「役職」スコアが上がり、「件名」スコアは除外/減点されるため、確実に「役職」へ。
  - ラベルが単に `Title` の場合は「件名」へマッピング（従来通り）。

- 回帰影響: 既存の「件名」検出は維持（`title`/`subject`/`topic` は引き続き有効）。`job` 単体語は除外リストに入れていないため、求人フォーム以外の誤除外を回避。

- 確認手順:
  1) 実フォームで `Job Title` ラベルがあるケースを取得して実行
     - `python tests/test_field_mapping_analyzer.py --company-id <ID>`
  2) ログの `FIELD MAPPING ANALYSIS RESULTS` にて `役職` がマップされ、`件名` には割り当てられていないことを確認
  3) `page_source_*.html` と `analysis_result_*.json` を併読し、ラベル/DOM コンテキストと一致することを確認


## Safety Guidelines

### Safe Modifications
- Pattern additions (names, ids, placeholders)
- Exclusion pattern additions
- Weight adjustments (±5 points typically)
- Threshold adjustments (±10 points typically)

### Avoid These Changes
- Removing existing successful patterns without replacement
- Drastically changing core algorithm logic
- Modifying fundamental data structures  
- Breaking existing API contracts

### Verification Requirements
- Always run test after modifications
- Ensure no regression in previously correct mappings
- Verify improvements address specific evaluation issues
- Maintain overall accuracy trend upward

## Success Criteria and Goals

### Cycle Goal: Perfect Mapping for Target Form
**OBJECTIVE**: Achieve perfect field mapping for the specific company ID being tested, while maintaining generalizability.

#### Perfect Mapping Definition:
- **メールアドレス**: Correctly mapped (100% accuracy)
- **お問い合わせ本文**: Correctly mapped (100% accuracy)  
- **Zero critical errors**: No mappings that would break form submission
- **Minimal unnecessary mappings**: Only map clearly identifiable fields

#### Generalizability Requirement:
**CRITICAL**: Improvements must enhance the overall system's ability to handle diverse form structures. 
- Focus on pattern improvements that work across multiple form types
- Avoid form-specific hacks or hardcoded solutions
- Test changes conceptually against different form structures

## Simple Work Flow Process

### Standard Improvement Cycle
1. **Initial Test**: `python tests/test_field_mapping_analyzer.py` (note the company ID)
2. **Evaluate**: Identify mapping issues by comparing results with form HTML
3. **Implement**: Make generalizable improvements to field patterns/scoring
4. **Verify**: Re-test with same ID: `python tests/test_field_mapping_analyzer.py --company-id [ID]`
5. **Repeat**: Continue steps 2-4 until perfect mapping achieved

### Reporting Requirements
After achieving perfect mapping for the target form, provide a concise completion report:

```markdown
## Field Mapping Improvement - Cycle Complete

### Target Form Results
- **Company ID**: [ID]
- **メールアドレス**: ✅ Perfectly mapped  
- **お問い合わせ本文**: ✅ Perfectly mapped
- **Critical Errors**: 0
- **Unnecessary Mappings**: [Minimized count]

### Changes Made
- **Files Modified**: [List with full paths]
- **Key Changes**: [Brief description of main improvements]
- **Generalizability**: [How changes improve system overall]

### Verification
- **Before/After**: [Quantitative improvement evidence]
- **Test Iterations**: [Number of test-modify cycles required]
```

## Key Implementation Principles

### Focus Areas for Sustainable Improvement
1. **Pattern Enhancement**: Add generalizable name/id/placeholder patterns
2. **Exclusion Logic**: Improve false positive prevention across form types  
3. **Score Tuning**: Adjust thresholds for better essential field detection
4. **Context Analysis**: Leverage surrounding text for better field identification

### Success Metrics
- **Perfect Essential Fields**: メールアドレス + お問い合わせ本文 correctly mapped
- **Zero Critical Errors**: No mappings that break form submission
- **Generalizability**: Changes improve mapping across diverse Japanese forms

---

## Change Log: 2025-09-03 — パフォーマンス改善（DOM I/O往復削減・安全版）

目的: 精度を落とさず、フォーム解析～マッピングまでの実行時間を短縮する（並列処理は不使用）。主に Python⇄ブラウザ間の往復回数を減らす安全な最適化を実施。

変更点（実装済み）
- `element_scorer._get_element_info`:
  - 主要属性（tag/type/name/id/class/placeholder/value）を `element.evaluate(...)` の1回で一括取得するよう最適化。
  - `is_visible()`/`is_enabled()` は最終確認として維持し、簡易可視・有効判定はフォールバックに利用。
- `rule_based_analyzer._auto_handle_selects`:
  - `<select>` の option テキスト/値をループ取得から `select.evaluate(...)` の一括取得に変更。
- セレクタ生成（`_generate_playwright_selector` / `_generate_element_selector`）:
  - 一意性 `count()` チェックを原則省略し、`id` → `tag+name(+type)` → `tag(+type)` の順に軽量生成（既存の `Locator` は保持済み）。
- `<form>` 選択（`form_structure_analyzer._find_primary_form`）:
  - 各フォーム内の入力種別カウント/属性取得を、複数 `count()`/`get_attribute()` から `form.evaluate(...)` 一発へ集約。

### 追補: 位置ベース周辺テキスト抽出のフォーム境界制限（2025-09-03）
- `context_text_extractor._extract_by_position`:
  - `FormStructureAnalyzer` が検出した `form_bounds` を `ContextTextExtractor` に渡し、位置ベース抽出時にフォーム境界外のテキストを除外。
  - `RuleBasedAnalyzer.analyze_form` で構造解析直後に `set_form_bounds` を呼び出し。
- 期待効果: ページ全体のテキスト走査を避け、不要な候補の生成を抑制（精度維持）。

### 追補: label[for] の事前インデックス化（2025-09-03）
- `context_text_extractor._extract_from_labels`:
  - 初回呼び出し時に `label[for]` を全件スキャンして `{for_id: label_text}` のインデックスを構築し、以降は辞書参照に切替。
  - 個別ラベル検索はフォールバックとして維持（互換性・安全性担保）。
- 期待効果: forラベル参照の反復 I/O を削減（複数要素で有効）。

### 追補: 二段階スコアリング（予選→本戦）（2025-09-03）
- 目的: 全候補に対する詳細スコアリングと周辺テキスト抽出を減らし、所要時間を短縮。
- 実装:
  - `element_scorer.calculate_element_score_quick(...)` を追加（コンテキスト非依存・属性中心の軽量スコア）。
  - `rule_based_analyzer._execute_enhanced_field_mapping` で、
    - 予選: 軽量スコアで上位K件（必須は25件、それ以外は15件）を選抜。
    - 本戦: 選抜候補のみコンテキスト抽出＋詳細スコア計算を実行。
  - 除外（-999）は予選で弾く。既存の品質閾値/重複防止/動的閾値は従来通り適用。
- 安全性: 予選は広め（Kを十分大きく）に設定し、必須フィールドのリコールを重視。

### 追補: DT/TH 見出しの前処理インデックス化（2025-09-03）
- `ContextTextExtractor.build_form_context_index()` を追加し、フォーム境界内の以下を一度に収集:
  - `dl` の `dd` 領域の境界と直前 `dt` のテキスト
  - `table` の各 `td` 領域の境界と対応するヘッダ（行内 `th` もしくは `thead th`）
- 以後の要素ごとの抽出は、要素中心点が含まれる `dd`/`td` のレコードを辞書参照で即時取得（フォールバックは従来ロジック）。
- 効果: 1要素ごとの DOM 走査を削減し、コンテキスト抽出のI/Oをさらに低減。

期待効果
- 要素ごとの属性取得・ラベル調査に伴う往復回数を削減し、ページの複雑度が高いほど短縮効果が見込める。
- セレクト要素やフォーム選択時の計測系 API 呼び出しを大幅減。

安全性・精度面の配慮
- 必須判定（`_detect_required_status`）や最終の `is_visible`/`is_enabled` は維持し、機能結果は従来どおり。
- セレクタは `id` を最優先。`name` のみの場合は `tag+name(+type)` で具体性を確保。
- 既存の `Locator` オブジェクトは `element_info` に保持されており、必要に応じて直接操作可能。

検証方法（推奨）
1) 代表的な複数フォームで `tests/test_field_mapping_analyzer.py` を手動実行し、ログの `analysis_time` と主要フェーズ時間の比較。
2) 必須2項目（メールアドレス/お問い合わせ本文）のマッピング有無・スコアが回帰していないことを確認。
3) 任意: ログレベル `DEBUG` でAPI呼び出し回数の差分（計測ポイントを追加して比較）。
