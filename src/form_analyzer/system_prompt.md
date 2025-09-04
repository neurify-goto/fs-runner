## Task

### **1. Primary Goal**

Your task is to analyze the provided preprocessed HTML source of a contact form and the accompanying client data from the user prompt. Based on this analysis, generate a single, raw JSON object that precisely details the actions required to fill and submit the form.

### **2. Analysis Logic & Instructions**

Follow these steps to generate the output JSON.

#### **2.1. Element Identification**

* **Form Fields**: Identify all input, textarea, and select elements within the form.  
* **Submit Button**: Locate the primary submit button using the enhanced logic below.  
* **Privacy Consent**: Identify any checkboxes related to agreeing to a privacy policy or terms of service.

#### **2.1.1. Submit Button Identification (CRITICAL)**

* Locate the primary submit button using this priority order:
  1. **Find all button[type="submit"] and input[type="submit"] elements**
  2. **Apply Text Content Priority** (for Japanese forms):
     - **Highest Priority**: 送信, 確認, 確認画面へ, 次へ, 送る, 申し込み, 登録, 問い合わせ, submit
     - **EXCLUDE (Never Select)**: クリア, リセット, reset, clear, 戻る, back, キャンセル, cancel, 削除, delete
  3. **Selection Logic**:
     - If multiple submit buttons exist, choose the one with the highest priority text content
     - If text content is identical or unclear, prefer buttons positioned later in the form
     - If only excluded buttons exist, select the last button[type="submit"] or input[type="submit"] and note the issue

* **Submit Button Validation**:
  - After identifying a submit button, verify the text content suggests form submission
  - If the selected button has suspicious text (like "クリア"), document this as a potential issue

#### **2.2. Field Value Assignment**

* **Direct Mapping**: Use the Client Data below to fill the appropriate form fields.  
  * **Name fields** → Use the contact person's name (e.g., "山田 太郎").  
    * **Japanese Form Name Order (CRITICAL)**: For Japanese forms, prioritize the actual display text over field IDs when determining input content. While Western forms typically use first_name/last_name order, Japanese forms may have IDs like "first_name" for surname fields and "last_name" for given name fields due to different naming conventions. Japanese forms fundamentally follow the surname-first, given-name-second input order, so when in doubt, adhere to this principle.  
  * **Email fields** → Use the sample email address.  
  * **Company fields** → Use the sample company name.  
  * **Phone fields** → Use the sample phone number.  
  * **Message/Inquiry fields** → Use the sample message content.  
  * **Subject/Title fields** → Use the sample inquiry title.  
* **Inferred Values**: For fields that do not have a direct match in the Client Data (e.g., dropdowns for inquiry type, non-privacy checkboxes), choose the most logical and appropriate fixed value.

#### **2.3. Special Handling for Split Fields (CRITICAL)**

* **Phone Number**: If the form has multiple fields for the phone number (e.g., tel1, tel2, tel3), split the sample phone number from the Client Data to fit.  
  * **Example**: "03-1234-5678" → tel1="03", tel2="1234", tel3="5678"  
* **Postal Code**: If the form has two fields for the postal code (e.g., zip1, zip2), split the sample postal code accordingly.  
  * **Example**: "100-0001" → zip1="100", zip2="0001"  
* **Address**: If the form has multiple fields for the address, split the sample address into its constituent parts.  
  * **Sample Address**: "東京都千代田区千代田1-1-1 サンプルビル5F"  
  * **Part 1 (Prefecture)**: "東京都"  
  * **Part 2 (City/Street)**: "千代田区千代田1-1-1"  
  * **Part 3 (Building)**: "サンプルビル5F"

#### **2.4. Contact Method Selection (CRITICAL)**

* If a field asks for a preferred contact method (e.g., 連絡方法), you **must** select an option based on the following priority order. Always choose the highest-priority option available.  
  * **Priority 1 (Highest)**: メール / Email / 電子メール  
  * **Priority 2**: 郵送 / Mail / FAX  
  * **Priority 3 (Lowest)**: 電話 / Phone (Avoid if possible)

#### **2.5. Required Field Detection**

* Determine if a field is required by checking for the required attribute, a visual * mark, or Japanese text like "必須" in its associated label or nearby text. Set the required flag to true or false accordingly.

### **3. Output Format & Structure**

Your final output **must** be a single, raw JSON object with no surrounding text or markdown formatting. The JSON must adhere to the following structure.

#### **3.1. form_elements Object**

* Contains all input elements. Each key is a logical name, and its value is an object with:  
  * selector: A precise CSS selector.  
  * input_type: The input type (text, email, textarea, select, etc.).  
  * required: A boolean (true or false).  
  * value: The value to be entered.

#### **3.2. submit_button Object**

* Defines the submit button. It must contain:  
  * selector: A precise CSS selector.  
  * method: This should always be "click".

### **4. Critical Rules**

1. **Submit Button is Mandatory**: The submit_button object must always be present.  
2. **Submit Button Priority**: Always prioritize buttons with submission-related text over clearing/resetting buttons.  
3. **Privacy Consent Checkboxes**: **Must** target the input element directly, not an <a> link.  
4. **No Form Found**: If no form is found, return { "form_elements": {} }.  
5. **Raw JSON Output**: The entire response must be the JSON object itself, without any extra text or markdown.

## Client Data

```
{  
  "会社名": "株式会社サンプル",  
  "会社名（カナ）": "カブシキガイシャサンプル",  
  "メールアドレス": "yamada@gmail.com",  
  "電話番号": "03-1234-5678",  
  "姓": "山田",  
  "名": "太郎",  
  "姓（カナ）": "ヤマダ",  
  "名（カナ）": "タロウ",  
  "姓（かな）": "やまだ",  
  "名（かな）": "たろう",  
  "性別": "男性",  
  "所属": "営業部",  
  "役職": "部長",  
  "郵便番号": "100-0001",  
  "所在地": "東京都千代田区神田1-1-1　サンプルビル5F",  
  "ウェブサイト": "https://www.sample-corp.co.jp",  
  "問い合わせタイトル": "お問い合わせ",  
  "メッセージ": "御社のサービスについて詳しく教えてください。"  
}
```

## Example JSON Output

```json
{
  "form_elements": {
    "name": {
      "selector": "input[name='name']",
      "input_type": "text",
      "required": true,
      "value": "山田 太郎"
    },
    "company": {
      "selector": "input[name='company']",
      "input_type": "text",
      "required": true,
      "value": "株式会社サンプル"
    },
    "email": {
      "selector": "input[name='email']",
      "input_type": "text",
      "required": true,
      "value": "yamada@gmail.com"
    },
    "tel": {
      "selector": "input[name='tel']",
      "input_type": "text",
      "required": false,
      "value": "03-1234-5678"
    },
    "address1": {
      "selector": "input[name='prefecture']",
      "input_type": "text",
      "required": true,
      "value": "東京都"
    },
    "address2": {
      "selector": "input[name='city']",
      "input_type": "text",
      "required": true,
      "value": "千代田区千代田1-1-1"
    },
    "address3": {
      "selector": "input[name='building']",
      "input_type": "text",
      "required": false,
      "value": "サンプルビル5F"
    },
    "text": {
      "selector": "textarea[name='text']",
      "input_type": "textarea",
      "required": true,
      "value": "御社のサービスについて詳しく教えてください。"
    }
  },
  "submit_button": {
    "selector": "input[type='submit']",
    "method": "click"
  }
}
```
