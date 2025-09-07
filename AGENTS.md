This repository is `neurify-goto/fs-runner`.

## **Project Architecture**

### **System Overview**

This project implements an automated form submission system with a **GAS (Google Apps Script) → GitHub Actions** workflow architecture. The system consists of multiple specialized modules that coordinate batch processing tasks.

#### **Core Architecture Flow**
1. **GAS Triggers**: Time-based triggers initiate batch processing
2. **GitHub Repository Dispatch**: GAS sends workflow trigger events to GitHub Actions
3. **GitHub Actions Execution**: Python modules in `src/` directory execute the actual processing
4. **Result Reporting**: Processing results are stored back to Supabase database

#### **GAS Modules** (Located in `gas/` directory)
- **fetch-detail**: Company detail information retrieval system
- **form-finder**: Contact form discovery system
- **form-analyzer**: Form analysis and instruction generation system  
- **form-sender**: Automated form submission control system
- **stats**: Statistics collection and spreadsheet update system

Each GAS module follows the same pattern:
- Time-based trigger functions for scheduled execution
- Batch data retrieval from Supabase
- GitHub Actions workflow triggering via repository dispatch
- Error handling and status management

#### **GitHub Actions Integration**
- GAS modules trigger GitHub Actions workflows using repository dispatch events
- Python processing modules are located in `src/` directory
- Workflows handle the heavy computational tasks that exceed GAS limitations
- Results are persisted back to the database upon completion

## **Guidelines**

### **Communication**

* Communicate with users in **Japanese**.

### **Git Management**

* When creating a PR, always explain the reasoning behind changes, not just what was changed.
* When creating a PR, provide the Pull Request URL to the user.
* Include relevant issue numbers when applicable.
* Maintain consistency with existing commit message style in the repository.
* Before pushing new commits to the branch of an open Pull Request, post a comment on the PR summarizing the additional changes **between commit and push** to make the review easier. **DO NOT ADD A COMMENT AFTER PUSH.**
* **PR Review Handling:** When receiving user review feedback on a Pull Request, do not blindly accept all suggestions. Critically evaluate each proposed improvement individually, determining whether it should be implemented based on technical merit and alignment with project standards. Only implement changes that are genuinely necessary and beneficial.
* **Pull Request descriptions must report all changed files.** To ensure traceability, the description must list all created, modified, and deleted files using the following format:
  ```
  ## 今回の開発で変更されたファイル一覧  
  ### 新規作成:  
  - path/to/new_file.py - 理由: [簡潔な説明]  
  ### 編集:  
  - path/to/modified_file.py - 変更内容: [簡潔な説明]  
  ### 削除:  
  - path/to/deleted_file.txt - 理由: [簡潔な説明]
  ```

### **Development**

* **Prioritize simplicity and clarity.** Avoid over-engineering. Always seek the most straightforward solution to a problem.  
* **Actively manage technical debt.** Regularly refactor code to improve its structure, readability, and maintainability.  
* **Proactively remove unused code.** Actively delete methods and files that are clearly judged to be no longer necessary.  
* **Don't Repeat Yourself (DRY).** Avoid creating multiple methods with the same role. Consolidate them into a single, unified method.  
* **Manage constants properly.** Avoid hard-coding values.  
  * Secure credentials, such as API keys, **must** be stored in a `.env` file. Do not use system environment variables (e.g., via export).
  * All other constants (non-confidential ones) should be placed in JSON files within the `config/` directory instead of environment variables.
  * **Configuration File Management:** When editing local configuration files in `config/*.json`.
* **Verify your work.** Always test and verify that your code works as expected before considering the task complete.  
* **Investigate bugs thoroughly.** When a bug is reported, conduct a comprehensive investigation by analyzing code, logs, and configurations to identify the root cause before implementing a fix. If more information is needed, clearly specify what is required.
* **Security for Internal Systems:** As this is an internally operated system, it is not necessary to be overly sensitive to threats like SQL injection.
* **Timestamps:** When recording time in the database, always use Japan Standard Time (JST).
* **Never code secret keys on tracked files**

#### **Python Module Organization**

* **GitHub Actions Entry Points**: Python files directly executed by GitHub Actions workflows must be placed in the `src/` directory.
* **Modularization**: To prevent file bloat, appropriately modularize code into smaller, focused modules.
* **Module Structure**: Non-entry point modules should be organized in appropriate subdirectories within `src/` based on their functionality.
* **Test Organization**: All test files must be placed in the `tests/` directory.

#### **Local Testing Guidelines**

* **Environment Variables**: Use `.env` files for environment variables during local testing.
* **Test Data**: For local submission testing, use actual client data from `tests/info.json` (company's own content) instead of creating test input content.
* **Playwright Configuration**: When performing local submission tests, use Playwright in GUI mode (not headless) so users can observe the operation.

### **Coding Style**

* **Adhere to PEP 8:** All Python code should follow the PEP 8 style guide.  
* **Use Code Formatters/Linters:** Employ tools like Black for automatic formatting and Flake8 for linting to maintain code quality and consistency.

### **Dependency Management**

* **Package Management:** Strictly manage project dependencies using a requirements.txt file or a pyproject.toml (with a tool like Poetry).  
* **Adding New Dependencies:** Before adding a new library, verify its license and assess any potential security risks.

### **Error Handling and Logging**

* **Logging Policy:** Adhere to a clear logging policy. Use appropriate log levels (e.g., INFO, WARNING, ERROR) and ensure sensitive information (like personal data) is never logged.

#### **Security and Privacy in Logging**
* **Sensitive Data Protection:** Never log sensitive information including:
  * Company names and URLs (企業名・URL)
  * Client configuration data (クライアント設定データ)
  * Personal information (個人情報)
  * Environment variable values (環境変数の値)
  * Database record IDs in production environments
* **GitHub Actions Environment:** Use enhanced security logging in CI/CD environments:
  * Mask company names as `***COMPANY_REDACTED***`
  * Mask URLs as `***URL_REDACTED***`
  * Limit statistical information that could identify specific companies
  * Reduce record_id logging to essential error cases only
* **Test Environment:** Ensure test files do not expose real company data in logs
* **LogSanitizer Usage:** Utilize the existing LogSanitizer class to automatically mask sensitive information in logs

#### GAS Projects (Private Environments)
- GAS projects for this system are operated in private, internal environments. Therefore, masking of company names and URLs is NOT required in GAS logs. Keep masking and redaction strictly enforced in CI/CD (GitHub Actions) logs.

### **File Organization Standards**

#### **Documentation Management**
* **Documentation Placement:** All documentation files (*.md) must be placed in the `docs/` directory.
* **Documentation Structure:** Organize documentation by feature or module to maintain clarity and findability.
* **Documentation Naming:** Use descriptive names that clearly indicate the content and purpose of each document.
* **Documentation Creation Policy:** NEVER proactively create documentation files (*.md) or README files unless explicitly requested by the user. Always prefer editing existing files over creating new ones.

#### **SQL Scripts Management**
* **SQL File Placement:** All SQL scripts, including table definitions, migrations, and stored procedures, must be placed in the `scripts/` directory.
* **Table Schema Reference:** Table schema definitions are maintained in `scripts/table_schema/` directory. Always reference existing schemas before implementing database-related functionality.
* **Schema Consistency:** Ensure all database operations and queries are consistent with the existing table schemas defined in `scripts/table_schema/`. Verify column names, data types, and constraints before implementation.
* **SQL Organization:** Organize SQL files by functionality (e.g., `migrations/`, `functions/`, `indexes/`) when the scripts directory grows large.

### **Task Execution Policy**

* **Continuous execution:** You must execute assigned tasks end-to-end without pausing for interim check-ins or status updates. Continue until the task is fully completed.
* **Exceptions — major issues only:** Pausing or stopping is allowed only when a blocking or high‑risk issue occurs, such as safety/security concerns, legal or policy constraints, missing required access/approvals, risk of data loss or corruption, or unrecoverable environmental failures.
* **If an exception occurs:** Surface a concise problem report that includes what was attempted, the exact blocker, and concrete next steps or a minimal, clearly scoped request for input/approval.
