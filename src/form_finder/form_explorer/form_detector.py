#!/usr/bin/env python3
"""
GitHub Actions対応フォーム検出クラス（本家準拠版）

本家form-sales-fumaのFormDetectorアルゴリズムに完全準拠した
高度なフォーム検出システム。GitHub Actions環境向け最適化付き。
"""

import logging
import re
from typing import List, Dict, Any, Optional
from config.manager import get_form_finder_rules
from form_sender.security.log_sanitizer import sanitize_for_log
from playwright.async_api import Page

logger = logging.getLogger(__name__)


class FormDetector:
    """フォーム検出・品質チェッククラス（本家準拠版）"""
    
    def __init__(self):
        """初期化（GitHub Actions環境向け最適化）"""
        # 基本設定値（GitHub Actions 7GB環境向け）
        self.max_iframe_process = 5  # 処理するiframe数制限（7GB環境で安全な値）
        self.max_shadow_dom_depth = 3  # Shadow DOM探索深度制限（安全な値に調整）
        self.surrounding_text_limit = 500  # 周辺テキスト文字数制限（精度向上のため増加）
        
        # 除外対象のキーワード（本家準拠）
        self.search_keywords = ["search", "search-box", "search-form", "検索", "サーチ"]
        self.search_names = ["q", "query", "s", "keyword", "search"]
        self.login_keywords = ["login", "signin", "ログイン", "サインイン", "password"]
        
        # モーダル/動的フォーム検出用キーワード（本家準拠拡張）
        self.modal_contact_keywords = [
            'contact', 'inquiry', 'お問い合わせ', '問い合わせ', '相談', 'toiawase',
            'ご相談', 'お申し込み', '申込み', '申込', 'エントリー', 'entry',
            'お問合せ', '問合せ', '問い合わせフォーム', 'contactus', 'contact-us'
        ]
        
        # 外部フォームサービス検出用セレクタ（本家準拠拡張）
        self.external_form_services = [
            'iframe[src*="docs.google.com/forms"]',  # Google Forms
            'iframe[src*="typeform.com"]',           # Typeform
            'iframe[src*="form.run"]',               # formrun
            'iframe[src*="formstack.com"]',          # Formstack
            'iframe[src*="jotform.com"]',            # JotForm
            'iframe[src*="wufoo.com"]',              # Wufoo
            'iframe[src*="123formbuilder.com"]',     # 123FormBuilder
            'div[class*="hbspt-form"]',              # HubSpot Forms
            'div[id*="hubspot-form"]',               # HubSpot Forms (ID)
            'div[class*="typeform-widget"]',         # Typeform Widget
            'div[class*="mailchimp"]',               # Mailchimp Forms
            'div[class*="convertkit"]'               # ConvertKit Forms
        ]
        
        # 柔軟なフォーム要素検出用セレクタ（本家準拠拡張）
        self.flexible_form_selectors = [
            '[role="form"]',  # ARIA role form
            '.contact-form', '.inquiry-form', '.mail-form', '.contact', '.inquiry',  # クラス名
            '#contact-form', '#inquiry-form', '#mail-form', '#contact', '#inquiry',   # ID
            '.form-container', '.contact-section', '.inquiry-section',  # コンテナー系
            '[class*="contact"]', '[class*="inquiry"]', '[class*="form"]',  # 部分マッチ
            '[id*="contact"]', '[id*="inquiry"]', '[id*="form"]'        # ID部分マッチ
        ]

        # 採用専用フォーム除外（設定ファイル駆動）
        try:
            rules = get_form_finder_rules()
            rec = rules.get("recruitment_only_exclusion", {})
            self.recruitment_exclusion_keywords: List[str] = rec.get(
                "exclude_if_keywords_present_any",
                ["学歴", "大学", "出身", "経歴"],
            )
            self.general_contact_whitelist_keywords: List[str] = rec.get(
                "allow_if_general_contact_keywords_any",
                ["お問い合わせ", "お問合せ", "問い合わせ", "contact", "inquiry", "ご相談", "連絡"],
            )

            # 追加のフォームNGワード（例: 学校など）。存在しない場合は空配列。
            form_validation = rules.get("form_validation", {})
            self.form_ng_keywords: List[str] = [
                str(k) for k in form_validation.get("ng_keywords_any", []) if k
            ]

            # コメントフォーム除外（設定ファイル駆動）
            cmt = rules.get("comment_form_exclusion", {})
            self.comment_exclusion_keywords: List[str] = [
                str(k) for k in cmt.get("exclude_if_keywords_present_any", []) if k
            ] or [
                "コメント", "コメントを送信", "コメントする", "コメント投稿", "Leave a Reply", "Post Comment",
                "Add Comment", "reply", "respond", "comment"
            ]
            self.comment_attr_keywords: List[str] = [
                str(k) for k in cmt.get("exclude_if_form_attributes_contains_any", []) if k
            ] or [
                "commentform", "comment-form", "comment-submit", "commenttextarea", "comment-textarea",
                "comment-author", "comment-url", "reply-form", "respond"
            ]
            # 属性判定は単語境界/ハイフン/アンダースコアでの区切りを要求（誤検出対策: document など）
            self._comment_attr_patterns = [
                re.compile(rf"(^|[\s\-_]){re.escape(tok.lower())}($|[\s\-_])") for tok in self.comment_attr_keywords
            ]
        except Exception:
            # 設定読み込み失敗時のフォールバック（保守的に同じ規則を適用）
            self.recruitment_exclusion_keywords = ["学歴", "大学", "出身", "経歴"]
            self.general_contact_whitelist_keywords = [
                "お問い合わせ", "お問合せ", "問い合わせ", "contact", "inquiry", "ご相談", "連絡"
            ]
            self.form_ng_keywords = []
            self.comment_exclusion_keywords = [
                "コメント", "コメントを送信", "コメントする", "コメント投稿", "Leave a Reply", "Post Comment",
                "Add Comment", "reply", "respond"
            ]
            self.comment_attr_keywords = [
                "commentform", "comment-form", "comment-submit", "commenttextarea", "comment-textarea",
                "comment-author", "comment-url", "reply-form", "respond"
            ]
            self._comment_attr_patterns = [
                re.compile(rf"(^|[\s\-_]){re.escape(tok.lower())}($|[\s\-_])") for tok in self.comment_attr_keywords
            ]

    async def find_and_validate_forms(self, page: Page, html_content: str) -> List[Dict[str, Any]]:
        """ページ内のフォームを発見・検証（本家準拠版）"""
        try:
            logger.debug(f"フォーム検出開始: record_id={getattr(page, '_record_id', 'unknown')}")
            
            # メインDOM内のフォームを検索（本家準拠で強化）
            forms = await self._find_forms_in_main_dom(page)
            logger.debug(f"メインDOM検出フォーム: {len(forms)}個")
            
            # iframe内のフォーム検索（常に実行）
            iframe_forms = await self._find_forms_in_iframes(page)
            forms.extend(iframe_forms)
            logger.debug(f"iframe追加後フォーム総数: {len(forms)}個")
            
            # Shadow DOM内のフォーム検索（新機能・本家準拠）
            shadow_forms = await self._find_forms_in_shadow_dom(page)
            forms.extend(shadow_forms)
            logger.debug(f"Shadow DOM追加後フォーム総数: {len(forms)}個")
            
            # 各フォームを品質チェック（本家準拠強化）
            validated_forms = []
            for i, form_data in enumerate(forms):
                form_type = form_data.get('formType', 'standard')
                form_url = form_data.get('form_url', '')
                logger.debug(f"フォーム{i+1}の品質チェック中... (タイプ: {form_type}, URL: {form_url[:50] if form_url else 'None'}...)")
                
                if self._validate_form_quality(form_data, html_content):
                    # フォームデータを標準形式に変換
                    standardized_form = self._standardize_form_data(form_data)
                    if standardized_form is not None:  # 無効URLで除外されていないかチェック
                        validated_forms.append(standardized_form)
                        logger.debug(f"高品質フォーム発見: {form_data.get('source', 'unknown')}領域 ({form_type}) - URL有効")
                    else:
                        logger.debug(f"無効URLによりフォーム除外: {form_data.get('source', 'unknown')}領域 ({form_type}) - URL: {repr(form_url)[:50]}...")
                else:
                    logger.debug(f"品質チェック不合格: {form_data.get('source', 'unknown')}領域 ({form_type})")
            
            if validated_forms:
                # フォームタイプ別統計
                form_types = {}
                for form in validated_forms:
                    form_type = form.get('formType', 'standard')
                    form_types[form_type] = form_types.get(form_type, 0) + 1
                
                if form_types:
                    logger.debug(f"フォームタイプ別統計: {dict(form_types)}")
                
                # 複数フォーム処理（本家準拠強化）
                if len(validated_forms) > 1:
                    validated_forms = self._prioritize_multiple_forms(validated_forms)
                    logger.debug("複数フォーム優先度ソート完了")
                
                logger.debug(f"最終返却フォーム数: {len(validated_forms)}個")
            else:
                logger.debug("高品質フォームは発見されませんでした")
            
            return validated_forms
            
        except Exception as e:
            logger.error(f"フォーム検出エラー: {e}")
            return []

    def _sanitize_text_content(self, text: str, max_length: int = 300) -> str:
        """テキストコンテンツのサニタイゼーション"""
        if not isinstance(text, str):
            return ""
        # HTMLタグ除去、制御文字除去、長さ制限
        import re
        sanitized = re.sub(r'<[^>]*>', '', text)
        sanitized = re.sub(r'[\x00-\x1f\x7f]', '', sanitized)
        return sanitized[:max_length].strip()

    
    def _validate_config_for_js_injection(self):
        """JavaScript注入前の設定値検証（セキュリティ対策）"""
        if not isinstance(self.max_shadow_dom_depth, int) or self.max_shadow_dom_depth < 0:
            raise ValueError("max_shadow_dom_depth must be a positive integer")
        if not isinstance(self.max_iframe_process, int) or self.max_iframe_process < 0:
            raise ValueError("max_iframe_process must be a positive integer")
        if not isinstance(self.surrounding_text_limit, int) or self.surrounding_text_limit < 0:
            raise ValueError("surrounding_text_limit must be a positive integer")
    
    async def _find_forms_in_shadow_dom(self, page: Page) -> List[Dict[str, Any]]:
        """Shadow DOM内のフォームを検索（セキュリティ強化版）"""
        try:
            # セキュリティ: JavaScript注入前の設定値検証
            self._validate_config_for_js_injection()
            
            shadow_forms = await page.evaluate("""
                (config) => {
                    const formElements = [];
                    const searchDepth = config.maxShadowDepth;
                    const parentUrl = window.location.href; // 親ページのURL
                    
                    function searchShadowDOM(element, currentDepth = 0) {
                        if (currentDepth >= searchDepth) return;
                        
                        // Shadow rootを持つ要素を探索
                        if (element.shadowRoot) {
                            const shadowForms = element.shadowRoot.querySelectorAll('form, [role="form"]');
                            shadowForms.forEach((form, index) => {
                                try {
                                    const inputs = form.querySelectorAll('input, textarea, select');
                                    if (inputs.length === 0) return;
                                    
                                    const inputData = [];
                                    inputs.forEach(input => {
                                        inputData.push({
                                            type: input.type || input.tagName.toLowerCase(),
                                            tagName: input.tagName.toLowerCase(),
                                            name: input.name || '',
                                            id: input.id || '',
                                            placeholder: input.placeholder || ''
                                        });
                                    });
                                    
                                    const buttons = form.querySelectorAll('button, input[type="submit"]');
                                    const buttonData = [];
                                    buttons.forEach(button => {
                                        buttonData.push({
                                            type: button.type || '',
                                            text: button.textContent ? button.textContent.trim() : ''
                                        });
                                    });
                                    
                                    // Shadow DOM内での適切なURL設定（強化版）
                                    let formUrl = parentUrl;
                                    let formAction = form.action || parentUrl;
                                    
                                    // form.actionの妥当性チェック（about:、javascript:、data:を除外）
                                    if (form.action && 
                                        form.action.startsWith('http') && 
                                        !form.action.startsWith('about:') &&
                                        !form.action.startsWith('javascript:') &&
                                        !form.action.startsWith('data:')) {
                                        formAction = form.action;
                                    } else {
                                        formAction = parentUrl; // 無効な場合は親ページURLを使用
                                    }
                                    
                                    formElements.push({
                                        source: 'shadow',
                                        shadowDepth: currentDepth + 1,
                                        index: index,
                                        inputs: inputData,
                                        buttons: buttonData,
                                        surroundingText: form.textContent ? form.textContent.substring(0, 300) : '',
                                        formId: form.id || '',
                                        formClass: form.className || '',
                                        form_url: formUrl,
                                        form_action: formAction,
                                        form_method: (form.method || 'POST').toUpperCase(),
                                        absoluteY: 0,
                                        domOrder: index,
                                        formType: 'shadow-dom'
                                    });
                                } catch (e) {
                                    // エラー無視
                                }
                            });
                            
                            // 再帰的に子要素も探索
                            element.shadowRoot.querySelectorAll('*').forEach(child => {
                                searchShadowDOM(child, currentDepth + 1);
                            });
                        }
                        
                        // 通常の子要素も探索
                        element.children && Array.from(element.children).forEach(child => {
                            searchShadowDOM(child, currentDepth);
                        });
                    }
                    
                    // ドキュメント全体から開始
                    searchShadowDOM(document.body);
                    
                    return formElements;
                }
            """, {
                'maxShadowDepth': self.max_shadow_dom_depth
            })
            
            # セキュリティ: 取得したデータをサニタイゼーション
            sanitized_forms = []
            for form in shadow_forms:
                sanitized_form = form.copy()
                if 'surroundingText' in sanitized_form:
                    sanitized_form['surroundingText'] = self._sanitize_text_content(sanitized_form['surroundingText'])
                sanitized_forms.append(sanitized_form)
            
            logger.debug(f"Shadow DOM内で{len(sanitized_forms)}個のフォームを発見")
            return sanitized_forms
            
        except Exception as e:
            logger.warning(f"Shadow DOM フォーム検索エラー: {e}")
            return []
    
    async def _find_forms_in_main_dom(self, page: Page) -> List[Dict[str, Any]]:
        """メインDOM内のフォームを検索（本家準拠強化版）"""
        try:
            # セキュリティ: JavaScript注入前の設定値検証
            self._validate_config_for_js_injection()
            
            forms = await page.evaluate("""
                (config) => {
                    const modalKeywords = config.modalKeywords;
                    const externalSelectors = config.externalSelectors;
                    const flexibleSelectors = config.flexibleSelectors;
                    const formElements = [];
                    
                    function extractFormData(formElement, index, source, formType = 'standard') {
                        try {
                            const inputs = formElement.querySelectorAll('input, textarea, select');
                            const buttons = formElement.querySelectorAll('button, input[type="submit"]');
                            
                            const inputData = [];
                            inputs.forEach(input => {
                                inputData.push({
                                    type: input.type || input.tagName.toLowerCase(),
                                    tagName: input.tagName.toLowerCase(),
                                    name: input.name || '',
                                    id: input.id || '',
                                    placeholder: input.placeholder || '',
                                    ariaLabel: input.getAttribute('aria-label') || '',
                                    className: input.className || ''
                                });
                            });
                            
                            const buttonData = [];
                            buttons.forEach(button => {
                                buttonData.push({
                                    type: button.type || '',
                                    text: button.textContent ? button.textContent.trim() : '',
                                    className: button.className || ''
                                });
                            });
                            
                            // フォーム周辺のテキストを取得
                            const surroundingText = getSurroundingText(formElement);
                            
                            // Y座標取得
                            const rect = formElement.getBoundingClientRect();
                            const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
                            const absoluteY = rect.top + scrollTop;
                            
                            return {
                                source: source,
                                index: index,
                                inputs: inputData,
                                buttons: buttonData,
                                surroundingText: surroundingText,
                                formId: formElement.id || '',
                                formClass: formElement.className || '',
                                formRole: formElement.getAttribute('role') || '',
                                form_url: getRobustPageUrl(),
                                form_action: formElement.action || getRobustPageUrl(),
                                form_method: (formElement.method || 'POST').toUpperCase(),
                                absoluteY: absoluteY,
                                domOrder: index,
                                formType: formType
                            };
                        } catch (e) {
                            return null;
                        }
                    }
                    
                    // 堅牢なURL取得ヘルパー関数
                    function getRobustPageUrl() {
                        try {
                            // 複数の手法を試行してより信頼性の高いURLを取得
                            let candidates = [];
                            
                            // 1. window.location.href
                            try {
                                if (window.location && window.location.href) {
                                    candidates.push({method: 'location.href', url: window.location.href});
                                }
                            } catch (e) {}
                            
                            // 2. document.URL
                            try {
                                if (document.URL) {
                                    candidates.push({method: 'document.URL', url: document.URL});
                                }
                            } catch (e) {}
                            
                            // 3. document.location
                            try {
                                if (document.location && document.location.href) {
                                    candidates.push({method: 'document.location.href', url: document.location.href});
                                }
                            } catch (e) {}
                            
                            // 有効なURLを検証して返す
                            for (let candidate of candidates) {
                                const url = candidate.url;
                                // 基本的な妥当性チェック
                                if (url && 
                                    typeof url === 'string' && 
                                    url.trim() !== '' &&
                                    !url.startsWith('about:') &&
                                    (url.startsWith('http://') || url.startsWith('https://')) &&
                                    url.length <= 2048) {
                                    return url;
                                }
                            }
                            
                            // フォールバック（従来の方式）
                            return window.location.href || '';
                        } catch (e) {
                            // 最後の手段
                            try {
                                return window.location.href || '';
                            } catch (finalE) {
                                return '';
                            }
                        }
                    }
                    
                    function getSurroundingText(element) {
                        try {
                            let text = element.textContent || '';
                            
                            // 親要素のテキストも含める
                            let parent = element.parentElement;
                            let level = 0;
                            while (parent && level < 3) {
                                const parentText = parent.textContent || '';
                                if (parentText.length > text.length) {
                                    text = parentText;
                                }
                                parent = parent.parentElement;
                                level++;
                            }
                            
                            // 300文字以内に制限
                            return text.substring(0, 300);
                        } catch (e) {
                            return '';
                        }
                    }
                    
                    // 1. 基本的な<form>要素を検索
                    const forms = document.querySelectorAll('form');
                    forms.forEach((form, index) => {
                        // 簡単な除外チェック
                        if (form.classList.contains('search') || 
                            form.classList.contains('search-form')) {
                            return;
                        }
                        
                        const searchInput = form.querySelector('input[type="search"]');
                        if (searchInput) {
                            return;
                        }
                        
                        const queryInputs = form.querySelectorAll('input[name="q"], input[name="query"], input[name="s"]');
                        if (queryInputs.length > 0) {
                            return;
                        }
                        
                        // ヘッダー・フッター・ナビ内の除外
                        const inHeader = form.closest('header') !== null;
                        const inFooter = form.closest('footer') !== null;
                        const inNav = form.closest('nav') !== null;
                        
                        if (!inHeader && !inFooter && !inNav) {
                            const formData = extractFormData(form, index, 'main');
                            if (formData) formElements.push(formData);
                        }
                    });
                    
                    // 2. モーダルトリガー検出
                    const modalTriggers = document.querySelectorAll(
                        '[data-toggle="modal"], [data-bs-toggle="modal"], ' +
                        '[data-target*="contact"], [data-target*="inquiry"], [data-target*="form"], ' +
                        '[onclick*="contact"], [onclick*="inquiry"], [onclick*="modal"], [onclick*="popup"]'
                    );
                    
                    modalTriggers.forEach((trigger, triggerIndex) => {
                        try {
                            const triggerText = trigger.textContent?.trim() || '';
                            const triggerClass = trigger.className || '';
                            const triggerId = trigger.id || '';
                            
                            const hasContactKeyword = modalKeywords.some(keyword => 
                                triggerText.toLowerCase().includes(keyword) ||
                                triggerClass.toLowerCase().includes(keyword) ||
                                triggerId.toLowerCase().includes(keyword)
                            );
                            
                            if (hasContactKeyword) {
                                const virtualForm = {
                                    source: 'main',
                                    index: formElements.length,
                                    inputs: [{
                                        type: 'text',
                                        tagName: 'input',
                                        name: 'name',
                                        id: 'modal-name',
                                        placeholder: 'お名前',
                                        ariaLabel: '',
                                        className: ''
                                    }, {
                                        type: 'email',
                                        tagName: 'input',
                                        name: 'email',
                                        id: 'modal-email',
                                        placeholder: 'メールアドレス',
                                        ariaLabel: '',
                                        className: ''
                                    }],
                                    buttons: [{
                                        type: 'submit',
                                        text: triggerText,
                                        className: triggerClass
                                    }],
                                    surroundingText: triggerText + ' ' + getSurroundingText(trigger),
                                    formId: trigger.getAttribute('data-target') || 'modal-form',
                                    formClass: 'modal-form dynamic-form',
                                    formRole: 'form',
                                    form_url: getRobustPageUrl(),
                                    form_action: getRobustPageUrl(),
                                    form_method: 'POST',
                                    absoluteY: trigger.getBoundingClientRect().top + (window.pageYOffset || document.documentElement.scrollTop),
                                    domOrder: formElements.length,
                                    formType: 'modal-trigger'
                                };
                                formElements.push(virtualForm);
                            }
                        } catch (e) {
                            // エラー無視
                        }
                    });
                    
                    // 3. 外部フォームサービス検出
                    externalSelectors.forEach((selector) => {
                        const elements = document.querySelectorAll(selector);
                        elements.forEach((element) => {
                            try {
                                const src = element.src || '';
                                const serviceType = src.includes('google.com') ? 'Google Forms' :
                                                  src.includes('typeform.com') ? 'Typeform' :
                                                  src.includes('form.run') ? 'formrun' :
                                                  'External Form';
                                
                                // 外部フォームサービスのURL検証と適切な設定（強化版）
                                let formUrl = getRobustPageUrl(); // デフォルトは現在のページ
                                let formAction = getRobustPageUrl();
                                
                                // srcが有効なHTTP URLの場合のみ使用（about:、javascript:、data:を除外）
                                if (src && 
                                    src.startsWith('http') && 
                                    !src.startsWith('about:') &&
                                    !src.startsWith('javascript:') &&
                                    !src.startsWith('data:')) {
                                    formUrl = src;
                                    formAction = src;
                                }
                                
                                const externalForm = {
                                    source: 'main',
                                    index: formElements.length,
                                    inputs: [{
                                        type: 'email',
                                        tagName: 'input',
                                        name: 'email',
                                        id: 'external-email',
                                        placeholder: 'メールアドレス',
                                        ariaLabel: '',
                                        className: ''
                                    }],
                                    buttons: [{
                                        type: 'submit',
                                        text: '送信',
                                        className: 'submit-btn'
                                    }],
                                    surroundingText: serviceType + ' フォーム ' + getSurroundingText(element),
                                    formId: element.id || 'external-form',
                                    formClass: 'external-form',
                                    formRole: 'form',
                                    form_url: formUrl,
                                    form_action: formAction,
                                    form_method: 'POST',
                                    absoluteY: element.getBoundingClientRect().top + (window.pageYOffset || document.documentElement.scrollTop),
                                    domOrder: formElements.length,
                                    formType: 'external-service'
                                };
                                formElements.push(externalForm);
                            } catch (e) {
                                // エラー無視
                            }
                        });
                    });
                    
                    // 4. 柔軟なフォーム要素検出
                    flexibleSelectors.forEach((selector) => {
                        const elements = document.querySelectorAll(selector);
                        elements.forEach((element) => {
                            try {
                                if (element.tagName.toLowerCase() === 'form') return;
                                
                                const inputs = element.querySelectorAll('input, textarea, select');
                                const buttons = element.querySelectorAll('button, input[type="submit"]');
                                
                                const hasEmailInput = Array.from(inputs).some(input => 
                                    input.type === 'email' || 
                                    input.name?.toLowerCase().includes('mail') ||
                                    input.placeholder?.toLowerCase().includes('mail')
                                );
                                
                                const hasSubmitButton = Array.from(buttons).some(button =>
                                    button.type === 'submit' ||
                                    button.textContent?.includes('送信') ||
                                    button.textContent?.includes('問い合わせ')
                                );
                                
                                if (hasEmailInput && hasSubmitButton && inputs.length >= 1) {
                                    const formData = extractFormData(element, formElements.length, 'main', 'flexible-form');
                                    if (formData) {
                                        formElements.push(formData);
                                    }
                                }
                            } catch (e) {
                                // エラー無視
                            }
                        });
                    });
                    
                    return formElements;
                }
            """, {
                'modalKeywords': self.modal_contact_keywords,
                'externalSelectors': self.external_form_services,
                'flexibleSelectors': self.flexible_form_selectors
            })
            
            logger.debug(f"メインDOM検索結果: {len(forms)}個のフォーム要素")
            return forms
            
        except Exception as e:
            logger.warning(f"メインDOM フォーム検索エラー: {e}")
            return []

    async def _find_forms_in_iframes(self, page: Page) -> List[Dict[str, Any]]:
        """iframe内のフォームを検索（簡素版）"""
        forms = []
        try:
            # 親ページのURLを取得してiframe内で使用
            parent_page_url = await page.evaluate("(() => { " + 
                """
                // 堅牢なURL取得
                function getRobustPageUrl() {
                    try {
                        let candidates = [];
                        try { if (window.location && window.location.href) candidates.push(window.location.href); } catch (e) {}
                        try { if (document.URL) candidates.push(document.URL); } catch (e) {}
                        try { if (document.location && document.location.href) candidates.push(document.location.href); } catch (e) {}
                        
                        for (let url of candidates) {
                            if (url && typeof url === 'string' && url.trim() !== '' &&
                                !url.startsWith('about:') && 
                                (url.startsWith('http://') || url.startsWith('https://')) &&
                                url.length <= 2048) {
                                return url;
                            }
                        }
                        return window.location.href || '';
                    } catch (e) {
                        try { return window.location.href || ''; } catch (finalE) { return ''; }
                    }
                }
                return getRobustPageUrl();
                """ + " })()")
            iframe_handles = await page.query_selector_all('iframe')
            
            # 最大3個のiframeのみ処理（処理時間短縮）
            for i, iframe_handle in enumerate(iframe_handles[:self.max_iframe_process]):
                try:
                    frame = await iframe_handle.content_frame()
                    if not frame:
                        continue
                    
                    iframe_forms = await frame.evaluate("""
                        (config) => {
                            const formElements = [];
                            // 堅牢なURL取得関数（iframe内でも使用）
                            function getRobustPageUrl() {
                                try {
                                    let candidates = [];
                                    try { if (window.location && window.location.href) candidates.push(window.location.href); } catch (e) {}
                                    try { if (document.URL) candidates.push(document.URL); } catch (e) {}
                                    try { if (document.location && document.location.href) candidates.push(document.location.href); } catch (e) {}
                                    
                                    for (let url of candidates) {
                                        if (url && typeof url === 'string' && url.trim() !== '' &&
                                            !url.startsWith('about:') && 
                                            (url.startsWith('http://') || url.startsWith('https://')) &&
                                            url.length <= 2048) {
                                            return url;
                                        }
                                    }
                                    return window.location.href || '';
                                } catch (e) {
                                    try { return window.location.href || ''; } catch (finalE) { return ''; }
                                }
                            }
                            
                            const forms = document.querySelectorAll('form, [role="form"]');
                            const parentUrl = config.parentUrl;
                            const iframeIndex = config.iframeIndex;
                            
                            forms.forEach((form, index) => {
                                try {
                                    const inputs = form.querySelectorAll('input, textarea, select');
                                    const buttons = form.querySelectorAll('button, input[type="submit"]');
                                    
                                    if (inputs.length === 0) return;
                                    
                                    const inputData = [];
                                    inputs.forEach(input => {
                                        inputData.push({
                                            type: input.type || input.tagName.toLowerCase(),
                                            tagName: input.tagName.toLowerCase(),
                                            name: input.name || '',
                                            id: input.id || '',
                                            placeholder: input.placeholder || ''
                                        });
                                    });
                                    
                                    const buttonData = [];
                                    buttons.forEach(button => {
                                        buttonData.push({
                                            type: button.type || '',
                                            text: button.textContent ? button.textContent.trim() : ''
                                        });
                                    });
                                    
                                    const rect = form.getBoundingClientRect();
                                    const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
                                    const absoluteY = rect.top + scrollTop;
                                    
                                    // iframe内URLの妥当性チェック（強化版）
                                    let currentUrl = getRobustPageUrl();
                                    let formUrl = parentUrl; // デフォルトは親ページURL
                                    
                                    // iframe内URLが有効かチェック（about:、javascript:、data:を除外）
                                    if (currentUrl && 
                                        currentUrl.startsWith('http') && 
                                        !currentUrl.startsWith('about:') &&
                                        !currentUrl.startsWith('javascript:') &&
                                        !currentUrl.startsWith('data:')) {
                                        formUrl = currentUrl;
                                    }
                                    
                                    // form_actionも同様にチェック
                                    let formAction = form.action || formUrl;
                                    if (form.action) {
                                        if (form.action.startsWith('http') && 
                                            !form.action.startsWith('about:') &&
                                            !form.action.startsWith('javascript:') &&
                                            !form.action.startsWith('data:')) {
                                            formAction = form.action;
                                        } else {
                                            formAction = formUrl; // 無効な場合は親ページURLを使用
                                        }
                                    }
                                    
                                    formElements.push({
                                        source: 'iframe',
                                        iframe_index: iframeIndex,
                                        index: index,
                                        inputs: inputData,
                                        buttons: buttonData,
                                        surroundingText: form.textContent ? form.textContent.substring(0, 300) : '',
                                        formId: form.id || '',
                                        formClass: form.className || '',
                                        form_url: formUrl,
                                        form_action: formAction,
                                        form_method: (form.method || 'POST').toUpperCase(),
                                        absoluteY: absoluteY,
                                        domOrder: index,
                                        formType: 'iframe'
                                    });
                                } catch (e) {
                                    // エラー無視
                                }
                            });
                            
                            return formElements;
                        }
                    """, {'parentUrl': parent_page_url, 'iframeIndex': i})
                    
                    forms.extend(iframe_forms)
                    
                except Exception as e:
                    logger.debug(f"iframe {i} 処理エラー: {e}")
                    continue
            
            logger.debug(f"iframe内で{len(forms)}個のフォームを発見")
            return forms
            
        except Exception as e:
            logger.warning(f"iframe フォーム検索エラー: {e}")
            return []

    def _standardize_form_data(self, form_data: Dict[str, Any]) -> Dict[str, Any]:
        """フォームデータを標準形式に変換"""
        try:
            inputs = form_data.get('inputs', [])
            buttons = form_data.get('buttons', [])
            
            # 統一化されたform_urlの妥当性チェック
            from ..utils import is_valid_form_url
            form_url = form_data.get('form_url', '')
            if not is_valid_form_url(form_url):
                logger.warning(f"無効なform_urlを検出、除外: {repr(form_url)} (source: {form_data.get('source', 'unknown')}, type: {form_data.get('formType', 'standard')})")
                return None  # 無効なURLの場合はNoneを返して除外
            
            # form_actionの決定
            form_action = form_data.get('form_action', '')
            if not form_action:
                form_action = form_url
            elif not is_valid_form_url(form_action):
                form_action = form_url  # form_actionも無効な場合はform_urlを使用
            
            # form_methodの決定
            form_method = form_data.get('form_method', 'POST').upper()
            
            # form_fieldsを標準形式に変換
            form_fields = []
            for input_field in inputs:
                field_data = {
                    'type': input_field.get('type', 'text'),
                    'name': input_field.get('name', ''),
                    'id': input_field.get('id', ''),
                    'placeholder': input_field.get('placeholder', ''),
                    'required': input_field.get('required', False),
                    'value': input_field.get('value', '')
                }
                form_fields.append(field_data)
            
            standardized_form = {
                'form_url': form_url,
                'form_action': form_action,
                'form_method': form_method,
                'form_fields': form_fields,
                'is_valid': True,
                'source': form_data.get('source', 'main'),
                'form_id': form_data.get('formId', ''),
                'form_class': form_data.get('formClass', ''),
                'buttons': buttons,
                'surrounding_text': form_data.get('surroundingText', ''),
                'absolute_y': form_data.get('absoluteY', 0),
                'dom_order': form_data.get('domOrder', 0),
                'formType': form_data.get('formType', 'standard')
            }
            
            logger.debug(f"フォーム標準化完了: {len(form_fields)}個のフィールド")
            return standardized_form
            
        except Exception as e:
            logger.error(f"フォームデータ標準化エラー: {e}")
            return form_data

    def _validate_form_quality(self, form_data: Dict[str, Any], html_content: str) -> bool:
        """フォームの品質をチェック"""
        try:
            inputs = form_data.get('inputs', [])
            buttons = form_data.get('buttons', [])
            
            # 0. 基本的な要素存在チェック
            if not inputs:
                logger.debug("入力フィールドなしのためフォーム除外")
                return False
            
            # 1. 有効フィールドチェック（緩和版）
            text_input_count = self._count_text_inputs(inputs)
            contact_fields = self._count_contact_fields(inputs)
            
            valid_field_count = max(text_input_count, contact_fields)
            
            if valid_field_count < 1:
                logger.debug(f"有効フィールド不足のためフォーム除外: {valid_field_count}個")
                return False
            
            # 2. 明らかな検索フォームを除外
            if self._is_search_form(inputs):
                logger.debug("検索フォームのため除外")
                return False
            
            # 3. ログインフォームの除外
            if self._is_login_form(inputs):
                logger.debug("ログインフォームのため除外")
                return False

            # 3.2 コメントフォーム除外（ブログ等）
            if self._is_comment_form(form_data):
                logger.debug("コメントフォームと推定のため除外")
                return False

            # 3.5 採用専用フォームの除外（周辺テキスト・ボタン文言で判定）
            if self._is_recruitment_only_form(form_data):
                logger.debug("採用専用フォームと推定のため除外")
                return False

            # 3.6 NGワード（学校など）を含むフォームの除外
            if self._contains_forbidden_form_terms(form_data):
                logger.debug("NGワードを含むためフォーム除外（学校等）")
                return False

            # 4. 送信機能の存在チェック
            has_submit_capability = self._has_submit_capability(inputs, buttons, form_data)
            if not has_submit_capability:
                logger.debug("送信機能なしのためフォーム除外")
                return False
            
            logger.debug(f"有効なフォーム: 有効フィールド{valid_field_count}個")
            return True
            
        except Exception as e:
            logger.error(f"フォーム品質チェックエラー: {e}")
            return False

    def _is_comment_form(self, form_data: Dict[str, Any]) -> bool:
        """ブログ等のコメントフォームかどうかを簡易判定して除外。

        方針:
        - 明示的なコメントシグナル（id/class の境界一致: commentform/comment-form/reply/respond 等、
          もしくは強いテキスト: "Leave a Reply"/"Post Comment"/「コメントを送信」など）があれば、
          問い合わせ語が併記されていても除外する。
        - 汎用語（comment/comments/「コメント」）のみの出現は除外しない（英語圏の"Comments"フィールド対策）。
        """
        try:
            # 1) 属性による判定（強いシグナル）
            form_id = (form_data.get('formId') or form_data.get('form_id') or '').lower()
            form_class = (form_data.get('formClass') or form_data.get('form_class') or '').lower()
            attr_hay = f"{form_id} {form_class}"
            # 単純部分一致ではなく境界を考慮（document 等を誤検出しない）
            if any(p.search(attr_hay) for p in self._comment_attr_patterns):
                return True

            # 2) 周辺テキスト/ボタン文言
            surrounding = (
                form_data.get('surroundingText')
                or form_data.get('surrounding_text')
                or ''
            )
            button_texts = ' '.join((btn.get('text', '') or '') for btn in form_data.get('buttons', []) )
            texts = [surrounding, button_texts]

            # 3) 入力フィールドの placeholder/name/id
            for inp in form_data.get('inputs', []) or []:
                texts.append(inp.get('placeholder') or '')
                texts.append(inp.get('name') or '')
                texts.append(inp.get('id') or '')

            haystack_norm = ' '.join(texts).lower()

            # 問い合わせ語（一般）を抽出（共存時は除外しない方針）
            try:
                general_kws_norm = [str(k).lower() for k in getattr(self, 'general_contact_whitelist_keywords', []) if k]
            except Exception:
                general_kws_norm = ["お問い合わせ", "お問合せ", "問い合わせ", "contact", "inquiry", "ご相談", "連絡"]
            has_general_contact_kw = any(k in haystack_norm for k in general_kws_norm)

            # コメント系に該当する語から generic を除外して強いシグナルのみで判定
            generic_tokens = {"comment", "comments", "コメント"}
            strong_kws = [
                str(k).lower() for k in self.comment_exclusion_keywords
                if str(k).lower() not in generic_tokens
            ]
            if any(k in haystack_norm for k in strong_kws):
                return True
            return False
        except Exception:
            return False

    def _count_text_inputs(self, inputs: List[Dict[str, Any]]) -> int:
        """テキスト入力フィールドの数をカウント"""
        count = 0
        for input_field in inputs:
            tag_name = input_field.get('tagName', '').lower()
            field_type = input_field.get('type', '').lower()
            
            if (tag_name == 'textarea' or 
                (tag_name == 'input' and field_type in ['text', 'email', ''])):
                count += 1
        return count

    def _count_contact_fields(self, inputs: List[Dict[str, Any]]) -> int:
        """コンタクトフォーム向けのフィールド数をカウント"""
        count = 0
        for input_field in inputs:
            tag_name = input_field.get('tagName', '').lower()
            field_type = input_field.get('type', '').lower()
            
            valid_types = ['text', 'email', 'tel', 'url', 'number', 'date', 'time', 'file', '']
            
            if (tag_name in ['textarea', 'select'] or 
                (tag_name == 'input' and field_type in valid_types)):
                count += 1
        
        return count

    def _is_search_form(self, inputs: List[Dict[str, Any]]) -> bool:
        """検索フォームかどうかを判定"""
        for input_field in inputs:
            name = input_field.get('name', '').lower()
            id_attr = input_field.get('id', '').lower()
            placeholder = input_field.get('placeholder', '').lower()
            
            if name in self.search_names or any(keyword in name for keyword in self.search_keywords):
                return True
            if any(keyword in id_attr for keyword in self.search_keywords):
                return True
            if any(keyword in placeholder for keyword in ['search', '検索', 'サーチ']):
                return True
        
        return False

    def _is_login_form(self, inputs: List[Dict[str, Any]]) -> bool:
        """ログインフォームかどうかを判定"""
        has_password = False
        has_username = False
        
        for input_field in inputs:
            field_type = input_field.get('type', '').lower()
            name = input_field.get('name', '').lower()
            id_attr = input_field.get('id', '').lower()
            placeholder = input_field.get('placeholder', '').lower()
            
            if field_type == 'password':
                has_password = True
            
            username_indicators = ['username', 'user', 'email', 'login', 'userid', 'user_id']
            if (any(indicator in name for indicator in username_indicators) or
                any(indicator in id_attr for indicator in username_indicators) or
                any(indicator in placeholder for indicator in username_indicators)):
                has_username = True
        
        return has_password and has_username

    def _has_submit_capability(self, inputs: List[Dict[str, Any]], buttons: List[Dict[str, Any]], form_data: Dict[str, Any]) -> bool:
        """送信機能があるかどうかを判定"""
        # 1. 明示的な送信ボタンをチェック
        for button in buttons:
            button_type = button.get('type', '').lower()
            button_text = button.get('text', '').lower()
            
            if (button_type == 'submit' or 
                any(keyword in button_text for keyword in ['送信', 'submit', '問い合わせ', 'contact', '申し込', '登録', 'send'])):
                return True
        
        # 2. input[type="submit"]をチェック
        for input_field in inputs:
            if input_field.get('type', '').lower() == 'submit':
                return True
        
        # 3. フォームタイプが動的フォーム（モーダルなど）の場合は送信機能ありと仮定
        form_type = form_data.get('formType', '')
        if form_type in ['modal-trigger', 'external-service', 'flexible-form']:
            return True
        
        # 4. 複数フィールドがあれば送信機能ありと仮定（緩和条件）
        if len(inputs) >= 2:
            return True
        
        return False

    def _is_recruitment_only_form(self, form_data: Dict[str, Any]) -> bool:
        """採用専用フォームを簡易判定する。

        周辺テキストやボタン文言に、学歴・大学・出身・経歴などの採用関連語が含まれ、
        かつ一般的な問い合わせを示す語（お問い合わせ/問い合わせ/contact/inquiry等）が含まれない場合に限り除外とする。
        （採用と問い合わせを兼ねるフォームは許容）
        """
        try:
            # 周辺テキスト（ラベル・見出し等を含む）
            surrounding = (
                form_data.get('surroundingText')
                or form_data.get('surrounding_text')
                or ''
            )
            # ボタン文言も参考にする
            button_texts = ' '.join(
                (btn.get('text', '') or '') for btn in form_data.get('buttons', [])
            )
            haystack = f"{surrounding} {button_texts}"

            # 事前サニタイズ（不要な制御文字の除去）
            haystack = self._sanitize_text_content(haystack, max_length=self.surrounding_text_limit)

            # 英語UI等での大文字表記に対応（日本語は影響なし）
            norm_haystack = haystack.lower()
            recruit_kws_norm = [str(k).lower() for k in self.recruitment_exclusion_keywords if k]
            general_kws_norm = [str(k).lower() for k in self.general_contact_whitelist_keywords if k]

            # 採用系キーワード出現判定（OR）
            has_recruit_kw = any(k in norm_haystack for k in recruit_kws_norm)
            if not has_recruit_kw:
                return False

            # 問い合わせ系キーワード出現判定（兼用は許容）
            has_general_contact_kw = any(k in norm_haystack for k in general_kws_norm)
            if has_general_contact_kw:
                return False

            # ログ出力（機微情報はサニタイズ）
            try:
                matched = [k for k in self.recruitment_exclusion_keywords if k in haystack]
                logger.debug(
                    f"採用専用フォーム候補を検出: keywords={sanitize_for_log(str(matched))}"
                )
            except Exception:
                pass

            return True
        except Exception:
            return False

    def _contains_forbidden_form_terms(self, form_data: Dict[str, Any]) -> bool:
        """設定ファイルのNGワード（例: 学校）を含むかを判定。

        周辺テキスト、ボタン文言、入力プレースホルダ/名前/IDを対象に部分一致で検出する。
        """
        try:
            if not self.form_ng_keywords:
                return False

            texts: List[str] = []
            texts.append(form_data.get('surroundingText') or form_data.get('surrounding_text') or '')
            texts.extend([(btn.get('text') or '') for btn in form_data.get('buttons', [])])

            for inp in form_data.get('inputs', []) or []:
                texts.append(inp.get('placeholder') or '')
                texts.append(inp.get('name') or '')
                texts.append(inp.get('id') or '')

            haystack_norm = ' '.join(texts).lower()
            found = any((str(kw).lower() in haystack_norm) for kw in self.form_ng_keywords if kw)
            if found:
                try:
                    # ログは具体語を出さず方針のみ
                    logger.debug("フォームNGワード検出: 設定キーワードに一致")
                except Exception:
                    pass
            return found
        except Exception:
            return False

    def _prioritize_multiple_forms(self, forms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """複数フォーム処理の優先度付け"""
        try:
            logger.debug(f"複数フォーム優先度付け開始: {len(forms)}個のフォーム")
            
            forms_with_priority = []
            for i, form in enumerate(forms):
                absolute_y = form.get('absolute_y', 999999)  # 安全な大きな値
                dom_order = form.get('dom_order', i)
                source = form.get('source', 'unknown')
                
                # ソース別の基本優先度
                source_priority = {
                    'main': 0,
                    'iframe': 100,
                    'shadow': 200
                }.get(source, 300)
                
                # 追加の意味論スコア（問い合わせを優先、資料請求/採用は減点）
                try:
                    BONUS_CONTACT = 2000  # 問い合わせ系の強い優先
                    PENALTY_NON_CONTACT = 1200  # 問い合わせ以外（資料請求/採用等）の減点

                    texts: List[str] = []
                    texts.append(form.get('surroundingText') or form.get('surrounding_text') or '')
                    texts.extend([(btn.get('text') or '') for btn in form.get('buttons', [])])
                    haystack = ' '.join(texts)
                    norm_haystack = haystack.lower()

                    # 正のキーワード（設定ファイル準拠）
                    pos_kws = [str(k).lower() for k in getattr(self, 'general_contact_whitelist_keywords', []) if k]
                    has_contact_kw = any(k in norm_haystack for k in pos_kws)

                    # 負のキーワード（資料請求/見積/採用/エントリー等）
                    neg_kws = [
                        '資料請求', '見積', 'お見積', 'ダウンロード', 'catalog', 'カタログ', 'price', '料金',
                        'エントリー', 'entry', '採用', '求人', 'recruit', 'recruitment', 'career', 'careers', 'job', 'jobs'
                    ]
                    has_non_contact_kw = any(k in norm_haystack for k in neg_kws)

                    semantic_adjust = 0
                    if has_contact_kw:
                        semantic_adjust -= BONUS_CONTACT
                    elif has_non_contact_kw:
                        semantic_adjust += PENALTY_NON_CONTACT
                except Exception:
                    semantic_adjust = 0

                # 最終優先度スコア（小さいほど優先）
                priority_score = absolute_y + source_priority + (dom_order * 0.1) + semantic_adjust
                
                forms_with_priority.append((form, priority_score, absolute_y, dom_order, source))
            
            # 優先度スコア順でソート（小さい方が優先）
            forms_with_priority.sort(key=lambda x: x[1])
            
            if forms_with_priority:
                top_form = forms_with_priority[0]
                form_data, score, y_pos, dom_idx, src = top_form
                logger.info(f"最優先フォーム選択: {src}領域 DOM順={dom_idx} Y座標={y_pos:.1f}")
            
            # 最優先フォームのみを返す
            return [forms_with_priority[0][0]] if forms_with_priority else []
            
        except Exception as e:
            logger.error(f"複数フォーム優先度付けエラー: {e}")
            return [forms[0]] if forms else []
