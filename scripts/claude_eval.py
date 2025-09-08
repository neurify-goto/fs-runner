import argparse
import subprocess
import sys


PROMPT_TEMPLATE = """
### Task
The files in the following two paths contain the page source for a company contact form and the JSON results of field mapping for auto-filling the form. Compare these files to determine whether the mapping is correct for the form content.

### Criteria for Verification
- Are all required fields filled in?
- Is there inappropriate content mapped? For example, a name entered in the company name field.

### What to Output
1. Is the mapping correct?
2. If there are inappropriate content,
- Which element in the page source is problematic (please provide a broad overview of the relevant page elements, including relevant parts)
- What input should be entered for that element?
- How is it actually mapped (or not mapped)?
- Why is it inappropriate (if it is not clear from the information above)?

### File Paths
Mapping Result: {mapping_path}
Page Source (preprocessed): {page_path}
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Claude Code evaluation with given paths.")
    parser.add_argument("mapping_json", help="Path to analysis_result_*.json")
    parser.add_argument("page_source", help="Path to page_source_*.html")
    parser.add_argument("--model", default="sonnet", help="Claude model alias or name")
    args = parser.parse_args()

    prompt = PROMPT_TEMPLATE.format(mapping_path=args.mapping_json, page_path=args.page_source)

    try:
        proc = subprocess.run(
            ["/usr/local/bin/claude", "-p", prompt, "--model", args.model],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print("ERROR: claude CLI not found at /usr/local/bin/claude", file=sys.stderr)
        return 127

    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())

