This repository is `neurify-goto/fs-runner`.

## **Project Architecture**

### **System Overview**

This project orchestrates automated form operations through a hybrid **GAS (Google Apps Script) → Cloud Tasks → Cloud Run Dispatcher → Cloud Run Jobs / GCP Batch** architecture, while GitHub Actions remains available for selected batch workloads. Specialized modules collaborate to collect company data, discover contact forms, analyze instructions, and execute submissions at scale.

#### **Core Architecture Flow**
1. **GAS Triggers**: Time-based triggers and spreadsheet actions inside `gas/` modules start processing and prepare Supabase queues.
2. **Task Enqueue**: Serverless-ready paths package payloads and enqueue Cloud Tasks targeting the Cloud Run dispatcher; legacy or auxiliary paths fall back to GitHub repository dispatch events.
3. **Cloud Run Dispatcher**: The FastAPI service in `src/dispatcher` validates client configurations, refreshes signed URLs via Cloud Storage, records execution metadata to Supabase, and selects Cloud Run Jobs or GCP Batch for execution.
4. **Execution Backends**: `bin/form_sender_job_entry.py` boots the Python runner container on Cloud Run Jobs (default) or GCP Batch (high-parallel or long-running workloads), invoking submodules within `src/form_sender`.
5. **Processing Modules**: Python workers under `src/` coordinate browser automation, data extraction, and Supabase updates for each workload (form sending, form discovery, detail enrichment, analytics).
6. **Result Reporting**: Status and metrics are written back to Supabase and surfaced to GAS/spreadsheets for operators.

#### **GAS Modules** (`gas/`)
- **fetch-detail**: Retrieves company enrichment data and dispatches GitHub Actions jobs with retry support.
- **form-finder**: Crawls and identifies contact forms, queueing GitHub Actions workers per batch.
- **form-analyzer**: Generates submission instructions via LLM pipelines and saves outputs to Supabase.
- **form-sender**: Controls end-to-end submissions; prefers the Cloud Tasks → dispatcher route with GitHub Actions fallback/manual execution.
- **field-mapping-improvement**: Schedules the field mapping improvement workflow via repository dispatch.
- **stats**: Aggregates Supabase statistics and synchronizes management spreadsheets.

Each module provides:
- Time-based trigger functions for scheduled execution.
- Supabase integrations to pull queued records and update statuses.
- Dispatch clients (Cloud Tasks or GitHub repository dispatch) with resilient retry and logging.
- Error handling utilities shared across GAS scripts.

#### **Python Components** (`src/`)
- **dispatcher/**: Cloud Run API mediating between Cloud Tasks and compute backends, handling verification, signed URLs, Supabase execution records, and Cloud Run/GCP Batch launches.
- **form_sender/**: Runner implementation split into subpackages (`orchestrator`, `worker`, `browser`, `template`, `communication`, `database`, `security`, `validation`) powering automated submissions.
- **form_finder/**: Crawling/orchestration modules for discovering forms and coordinating worker processes.
- **form_analyzer/**: LLM-assisted analysis pipeline with prompt assets and Supabase writers.
- **fetch_detail/**: Browser automation and data formatting utilities for enrichment jobs.
- **shared/**: Supabase abstractions reusable by multiple workloads.
- **utils/** & **config/**: Cross-cutting helpers for feature flags, environment loading, logging, and configuration management.

`bin/form_sender_job_entry.py` serves as the container entry point for Cloud Run Jobs and GCP Batch executions.

#### **Automation & Infrastructure**
- **.github/workflows/**: Workflow definitions for each workload (form sender, finder, analyzer, fetch detail, field mapping improvement) and deployment tasks such as `deploy-gcp-batch.yml`.
- **cloudbuild/** & **cloudbuild.yaml**: Google Cloud Build pipelines for building runner and dispatcher container images.
- **Dockerfile** / **Dockerfile.dispatcher**: Container recipes for the Cloud Run Job runner and dispatcher.
- **infrastructure/gcp/**: IaC assets for Cloud Batch environments and dispatcher deployment support.
- **scripts/**: SQL migrations, functions, and manual operations aligned with Supabase schemas.
- **config/**: JSON-based runtime configuration managed from GAS/Python modules.
- **tests/** & **test_results/**: Automated test suites, fixtures, and stored artifacts for debugging.

### **Repository Layout Overview**

- `gas/` – GAS projects grouped by workload (fetch-detail, form-finder, form-analyzer, form-sender, field-mapping-improvement, stats).
- `src/` – Python source tree including dispatcher, workload runners, shared libraries, and utilities.
- `bin/` – Executable entrypoints (e.g., `form_sender_job_entry.py`) used by Cloud Run Jobs / Batch.
- `.github/workflows/` – GitHub Actions definitions for batch processing and deployment.
- `config/` – JSON configuration files for non-secret constants.
- `docs/` – Design documents and operational guides per feature.
- `cloudbuild/` & `cloudbuild.yaml` – Cloud Build specifications for container images.
- `Dockerfile` / `Dockerfile.dispatcher` – Container build definitions.
- `infrastructure/` – Infrastructure-as-code templates (currently GCP-focused).
- `scripts/` – Database schema references and SQL utilities.
- `tests/` & `test_results/` – Automated tests, fixtures, and captured execution results.
- `artifacts/` – Generated support assets (e.g., PR templates or helper markdown).

## **Guidelines**

### **Communication**

* Communicate with users in **Japanese**.

### **Git Management**

* For new PR creation requests: When instructed to create a new Pull Request, do not ask the user to approve the contents one by one. Use your best judgment to compose the most appropriate title, description, and scope, and create the PR proactively.
* For incidental local changes: If there are local modifications beyond what you personally implemented, review their contents and include them in the PR unless they are clearly inappropriate, incorrect, or risky.
* When creating a PR, always explain the reasoning behind changes, not just what was changed.
* When creating a PR, provide the Pull Request URL to the user.
* Include relevant issue numbers when applicable.
* Maintain consistency with existing commit message style in the repository.
* **PR pre-push comment (scope clarified):** Pre-push comments are NOT required when creating a new PR. Create the PR as usual.
* **Only for updates to an existing PR (strict order):** When updating an already open PR by pushing additional commits, follow this sequence — **implement → commit → comment → push**. After committing locally and before pushing, post a single PR comment summarizing the changes introduced by the new commit(s). **Do not post the comment after pushing.**
* **Comment style (past tense only):** For the additional-push scenario above, write the comment in past tense and factually describe what you implemented (e.g., "Replaced X with Y", "Fixed A by updating B"). Avoid future-tense or intention statements such as "about to push", "will push", or "going to".
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
* **Code File Editing:** When modifying code files, always use the Edit tool. Do not use Python scripts or programmatic approaches for code editing.

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
* **Sensitive Data Protection (CI/CD 環境):** GitHub Actions 等のCI/CDログでは、以下の機微情報を記録しない（または必ずマスキングする）:
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

#### GAS Projects (Private Environments) — マスキング対象外の明記
- 本システムの GAS プロジェクトは私的な内部環境で運用されます。したがって、GAS の実行ログは「企業名・URLのマスキング対象外」です（機能検証に必要な範囲で、企業名やURL等をそのまま出力可能）。
- ただし、CI/CD（GitHub Actions）ログでは従来通りマスキングと秘匿を厳格に適用します（上記「Sensitive Data Protection (CI/CD環境)」が優先）。
- どちらの環境でも、必要最小限のログにとどめ、個人情報や資格情報は記録しません。

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

* **User messaging timing:** Send any user-facing messages only after all instructed tasks have been fully completed. Avoid interim check-ins or status updates unless a blocking, high‑risk issue requires immediate clarification or approval.
* **Continuous execution:** You must execute assigned tasks end-to-end without pausing for interim check-ins or status updates. Continue until the task is fully completed.
* **Exceptions — major issues only:** Pausing or stopping is allowed only when a blocking or high‑risk issue occurs, such as safety/security concerns, legal or policy constraints, missing required access/approvals, risk of data loss or corruption, or unrecoverable environmental failures.
* **If an exception occurs:** Surface a concise problem report that includes what was attempted, the exact blocker, and concrete next steps or a minimal, clearly scoped request for input/approval.
