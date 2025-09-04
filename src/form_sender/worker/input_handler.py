"""
フォーム要素への入力処理を担当するハンドラ
"""
import logging
from typing import Dict, Any

from playwright.async_api import Page, ElementHandle, TimeoutError as PlaywrightTimeoutError


class FormInputHandler:
    """ページ上のフォーム要素への具体的な入力操作をカプセル化する"""

    def __init__(self, page: Page, worker_id: int):
        self.page = page
        self.worker_id = worker_id
        self.logger = logging.getLogger(f"{__name__}.w{worker_id}")

    async def fill_rule_based_field(self, field_name: str, field_info: Dict[str, Any], value: str) -> bool:
        """ルールベースで発見されたフィールドに値を入力する

        Returns:
            bool: 入力と検証が成功した場合 True、それ以外は False
        """
        selector = field_info.get('selector')
        # RuleBasedAnalyzer は 'input_type' キーに正規化済みの型を格納する。
        # 古い互換のため 'type' (HTML属性) もフォールバックとして参照する。
        input_type = field_info.get('input_type') or field_info.get('type', 'text')
        if not selector or value is None or not str(value).strip():
            self.logger.warning(f"Skipping field {field_name} due to missing selector or value.")
            return False

        self.logger.info(f"Starting field operation - field: {field_name}, type: {input_type}")
        element = await self.page.query_selector(selector)
        if not element:
            self.logger.warning(f"Rule-based element not found: {selector}")
            return False

        try:
            input_success = await self._fill_element(element, value, input_type, field_name)
            if input_success:
                await self.page.wait_for_timeout(500)  # 短時間待機
                verification_success = await self._verify_field_input(element, field_name, input_type, value)
                if verification_success:
                    self.logger.info(f"Field operation completed successfully - {field_name}")
                    return True
                else:
                    self.logger.warning(f"Field input verification failed - {field_name}")
                    return False
            else:
                self.logger.error(f"Field operation failed - {field_name}")
                return False
        except Exception as e:
            self.logger.error(f"Error in field operation - {field_name}: {e}")
            return False

    async def _fill_element(self, element: ElementHandle, value: str, input_type: str, field_name: str) -> bool:
        """要素の型に応じて入力処理を振り分ける"""
        tag_name = await element.evaluate('el => el.tagName.toLowerCase()')
        type_attr = await element.get_attribute('type')

        if input_type in ["text", "email", "tel", "url", "textarea", "password"]:
            return await self._fill_text_like(element, value, tag_name)
        elif input_type == "select":
            return await self._fill_select(element, value)
        elif input_type == "checkbox":
            return await self._fill_checkbox(element, value)
        elif input_type == "radio":
            return await self._fill_radio(element)
        else:
            self.logger.warning(f"Unknown input type '{input_type}' for field {field_name}, attempting text fill.")
            return await self._fill_text_like(element, value, tag_name)

    async def _fill_text_like(self, element: ElementHandle, value: str, tag_name: str) -> bool:
        """テキスト系要素への入力"""
        await element.fill(str(value))
        return True

    async def _fill_select(self, element: ElementHandle, value: str) -> bool:
        """Select要素の選択"""
        try:
            await element.select_option(value=str(value))
            return True
        except PlaywrightTimeoutError:
            try:
                await element.select_option(label=str(value))
                return True
            except Exception as e:
                self.logger.warning(f"Could not select option '{value}': {e}")
                return False

    async def _fill_checkbox(self, element: ElementHandle, value: str) -> bool:
        """Checkbox要素の選択"""
        should_be_checked = str(value).lower() not in ['false', '0', '', 'no']
        try:
            # 1) 素直にPlaywrightのensure動作
            if should_be_checked:
                await element.check()
            else:
                await element.uncheck()
            # 状態確認
            if (await element.is_checked()) == should_be_checked:
                return True
        except Exception as e:
            self.logger.debug(f"Primary checkbox action failed, trying fallbacks: {e}")

        # 2) for属性のlabelクリックを試す
        try:
            el_id = await element.get_attribute('id')
            if el_id:
                label_selector = f'label[for="{el_id}"]'
                label = await self.page.query_selector(label_selector)
                if label:
                    await label.click()
                    if (await element.is_checked()) == should_be_checked:
                        return True
        except Exception as e:
            self.logger.debug(f"Label(for=) click fallback failed: {e}")

        # 3) 親labelクリック（inputがlabel内にあるケース）
        try:
            parent_is_label = await element.evaluate("el => el.closest('label') !== null")
            if parent_is_label:
                await element.evaluate("el => el.closest('label').click()")
                if (await element.is_checked()) == should_be_checked:
                    return True
        except Exception as e:
            self.logger.debug(f"Closest(label) click fallback failed: {e}")

        # 4) 最終フォールバック: JSでcheckedを書き換え、input/changeイベントを発火
        try:
            await element.evaluate(
                "(el, should) => { el.checked = !!should; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                should_be_checked
            )
            await self.page.wait_for_timeout(100)
            if (await element.is_checked()) == should_be_checked:
                return True
        except Exception as e:
            self.logger.debug(f"JS set checked fallback failed: {e}")

        return False

    async def _fill_radio(self, element: ElementHandle) -> bool:
        """Radioボタンの選択"""
        await element.check() # Radioはcheck()で良い
        return True

    async def _verify_field_input(self, element: ElementHandle, field_name: str, input_type: str, expected_value: str) -> bool:
        """フィールド入力が正しく行われたか検証する"""
        try:
            if input_type in ["checkbox", "radio"]:
                is_checked = await element.is_checked()
                expected_checked = str(expected_value).lower() not in ['false', '0', '', 'no']
                return is_checked == expected_checked

            actual_value = await element.input_value()
            return expected_value in actual_value
        except Exception as e:
            self.logger.warning(f"Input verification error for {field_name}: {e}")
            return False
