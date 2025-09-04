import logging
import os
from pathlib import Path
from typing import List, Dict, Any, Tuple
import json

logger = logging.getLogger(__name__)


class PromptGenerator:
    """システムプロンプトとユーザープロンプトの生成ロジックをカプセル化するクラス"""

    def __init__(self):
        """プロンプトテンプレートの初期化（外部MDファイルから読み込み）"""
        self.system_prompt_template = None
        self.user_prompt_template = None
        self._load_prompt_templates()

    def _load_prompt_templates(self):
        """外部MDファイルからプロンプトテンプレートを読み込み"""
        try:
            current_dir = Path(__file__).parent
            system_prompt_path = current_dir / "system_prompt.md"
            user_prompt_path = current_dir / "user_prompt.md"
            
            # システムプロンプトの読み込み
            if system_prompt_path.exists():
                with open(system_prompt_path, 'r', encoding='utf-8') as f:
                    self.system_prompt_template = f.read().strip()
                logger.info(f"システムプロンプトを読み込みました: {system_prompt_path}")
            else:
                logger.error(f"システムプロンプトファイルが見つかりません: {system_prompt_path}")
                self._use_fallback_system_prompt()
            
            # ユーザープロンプトの読み込み
            if user_prompt_path.exists():
                with open(user_prompt_path, 'r', encoding='utf-8') as f:
                    self.user_prompt_template = f.read().strip()
                logger.info(f"ユーザープロンプトを読み込みました: {user_prompt_path}")
            else:
                logger.error(f"ユーザープロンプトファイルが見つかりません: {user_prompt_path}")
                self._use_fallback_user_prompt()
                
        except Exception as e:
            logger.error(f"プロンプトテンプレート読み込みエラー: {e}")
            self._use_fallback_prompts()
    
    def _use_fallback_system_prompt(self):
        """システムプロンプトのフォールバック"""
        logger.warning("フォールバックシステムプロンプトを使用します")
        self.system_prompt_template = """## Task

### **1. Primary Goal**

Your task is to analyze the provided preprocessed HTML source of a contact form and the accompanying client data from the user prompt. Based on this analysis, generate a single, raw JSON object that precisely details the actions required to fill and submit the form.

### **2. Output Format & Structure**

Your final output **must** be a single, raw JSON object with no surrounding text or markdown formatting.

#### **2.1. form_elements Object**

* Contains all input elements. Each key is a logical name, and its value is an object with:  
  * selector: A precise CSS selector.
  * input_type: The input type (text, email, textarea, select, etc.).
  * required: A boolean (true or false).
  * value: The value to be entered.

#### **2.2. submit_button Object**

* Defines the submit button. It must contain:  
  * selector: A precise CSS selector.
  * method: This should always be "click".

### **3. Critical Rules**

1. **Submit Button is Mandatory**: The submit_button object must always be present.  
2. **No Form Found**: If no form is found, return { "form_elements": {} }.
3. **Raw JSON Output**: The entire response must be the JSON object itself, without any extra text or markdown.
"""
    
    def _use_fallback_user_prompt(self):
        """ユーザープロンプトのフォールバック"""
        logger.warning("フォールバックユーザープロンプトを使用します")
        self.user_prompt_template = """### **Preprocessed Page Source**
```
{preprocessed_page_source}
```"""
    
    def _use_fallback_prompts(self):
        """全プロンプトのフォールバック"""
        logger.warning("全フォールバックプロンプトを使用します")
        self._use_fallback_system_prompt()
        self._use_fallback_user_prompt()

    def generate_prompts(self, company_data: Dict[str, Any], form_elements: List[Dict[str, Any]]) -> Tuple[str, str]:
        """システムプロンプトとユーザープロンプトを生成"""
        system_prompt = self._generate_system_prompt()
        user_prompt = self._generate_user_prompt(company_data, form_elements)
        return system_prompt, user_prompt

    def _generate_system_prompt(self) -> str:
        """システムプロンプトの生成"""
        if not self.system_prompt_template:
            logger.error("システムプロンプトテンプレートが読み込まれていません")
            self._use_fallback_system_prompt()
        return self.system_prompt_template

    def _generate_user_prompt(self, company_data: Dict[str, Any], form_elements: List[Dict[str, Any]]) -> str:
        """ユーザープロンプトの生成"""
        if not self.user_prompt_template:
            logger.error("ユーザープロンプトテンプレートが読み込まれていません")
            self._use_fallback_user_prompt()
        
        preprocessed_html = self._generate_preprocessed_html(company_data, form_elements)
        
        # デバッグ: 生成されたHTMLをログ出力
        logger.debug(f"生成されたpreprocessed_html (最初の500文字): {preprocessed_html[:500]}...")
        
        try:
            user_prompt = self.user_prompt_template.format(preprocessed_page_source=preprocessed_html)
            
            # デバッグ: 完成したユーザープロンプトをファイル出力
            self._debug_save_user_prompt(user_prompt, company_data.get('record_id'))
            
            return user_prompt
        except KeyError as e:
            logger.error(f"ユーザープロンプトのプレースホルダー置換エラー: {e}")
            logger.error(f"テンプレート内容: {self.user_prompt_template[:200]}...")
            # フォールバックとして単純な形式を返す
            fallback_prompt = f"### **Preprocessed Page Source**\\n```\\n{preprocessed_html}\\n```"
            self._debug_save_user_prompt(fallback_prompt, company_data.get('record_id'), is_fallback=True)
            return fallback_prompt

    def _generate_preprocessed_html(self, company_data: Dict[str, Any], form_elements: List[Dict[str, Any]]) -> str:
        """シンプルなフォーム出力 - 抽出されたHTMLをそのまま<form>で囲む（複数フォーム対応）"""
        record_id = company_data.get('record_id', '不明')
        
        logger.debug(f"シンプルHTML生成開始: Record ID {record_id}, フォーム要素数: {len(form_elements)}")
        
        if not form_elements or not form_elements[0].get('raw_form_content'):
            logger.warning(f"Record ID {record_id}: フォーム要素が空のため、空のフォームを生成")
            return '<form><!-- No form elements found --></form>'
        
        # 抽出されたフォーム内容（複数フォームの場合は結合済み）をそのまま使用
        raw_content = form_elements[0]['raw_form_content']
        
        # raw_contentに既に複数フォーム内容が結合されている場合は、個別に<form>で囲む
        if '\n' in raw_content and raw_content.count('\n') > 10:  # 複数フォームの可能性
            # 改行で分割された内容を個別のフォームとして処理
            form_sections = [section.strip() for section in raw_content.split('\n') if section.strip()]
            if len(form_sections) > 1:
                # 複数セクションがある場合は、それぞれを<form>で囲む
                result_html = '\n'.join([f'<form>{section}</form>' for section in form_sections])
                logger.debug(f"Record ID {record_id}: 複数フォーム生成完了 ({len(form_sections)}個のフォーム, {len(result_html)}文字)")
                return result_html
        
        # 単一フォームまたは既に結合済みの内容として処理
        result_html = f'<form>{raw_content}</form>'
        logger.debug(f"Record ID {record_id}: シンプルHTML生成完了 ({len(result_html)}文字)")
        
        return result_html
    
    
    def _debug_save_user_prompt(self, user_prompt: str, record_id: Any, is_fallback: bool = False) -> None:
        """デバッグ用：生成されたユーザープロンプトをファイル保存"""
        try:
            # デバッグ出力が有効な場合のみファイル保存
            if logger.getEffectiveLevel() <= logging.DEBUG:
                debug_dir = Path('../artifacts/debug_prompts')
                debug_dir.mkdir(parents=True, exist_ok=True)
                
                fallback_suffix = '_FALLBACK' if is_fallback else ''
                filename = f'user_prompt_record_{record_id}{fallback_suffix}.txt'
                filepath = debug_dir / filename
                
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(f"=== User Prompt for Record ID: {record_id} ===\n")
                    f.write(f"Generated at: {os.environ.get('GITHUB_RUN_ID', 'local')}\n")
                    f.write(f"Is Fallback: {is_fallback}\n")
                    f.write(f"Prompt Length: {len(user_prompt)} characters\n\n")
                    f.write(user_prompt)
                
                logger.debug(f"ユーザープロンプトをデバッグファイルに保存: {filepath}")
        except Exception as e:
            logger.warning(f"デバッグファイル保存エラー (無視可能): {e}")