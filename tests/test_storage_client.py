
import json
from pathlib import Path

import pytest

module_name = "gas.form-sender.StorageClient"


def _eval_module(monkeypatch, *, prelude="", body="", service_email="svc@example.iam.gserviceaccount.com"):
    module_path = Path(module_name.replace('.', '/')).with_suffix('.gs')
    source = module_path.read_text(encoding='utf-8')

    script = f"""var PropertiesService = {{
  getScriptProperties: function() {{
    return {{
      getProperty: function() {{ return 'bucket-name'; }},
    }};
  }}
}};

var Utilities = {{
  formatDate: function(date, tz, fmt) {{
    if (fmt === 'yyyyMMdd') return '20251003';
    if (fmt === "yyyyMMdd'T'HHmmss'Z'") return '20251003T010203Z';
    return '';
  }},
  computeDigest: function() {{ return [0]; }},
  DigestAlgorithm: {{ SHA_256: 'SHA_256' }},
  Charset: {{ UTF_8: 'UTF-8' }},
  getUuid: function() {{ return 'uuid-1234'; }},
  sleep: function(ms) {{
    (globalThis.__sleepLog || (globalThis.__sleepLog = [])).push(ms);
  }}
}};
{prelude}
var accessTokenCalls = 0;
var signBytesCalls = 0;
var ServiceAccountClient = {{
  getServiceAccountEmail: function() {{ return '{service_email}'; }},
  getAccessToken: function() {{ accessTokenCalls++; return 'access-token'; }},
  signBytes: function(payload) {{ signBytesCalls++; return 'abcdef'; }}
}};
{source}
(function() {{
  try {{
    var result = (function() {{
      {body}
    }})();
    if (typeof result === 'undefined') {{
      result = null;
    }}
    console.log(JSON.stringify(result));
  }} catch (err) {{
    console.log(JSON.stringify({{ error: err.message, stack: err.stack }}));
  }}
}})();
"""

    from subprocess import run, PIPE
    return run(["node", "-e", script], stdout=PIPE, stderr=PIPE, text=True)


def _parse_result(proc):
    assert proc.stdout, f"expected stdout from Node script, stderr={proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_signed_url_includes_bucket(monkeypatch):
    proc = _eval_module(
        monkeypatch,
        body="return { signedUrl: StorageClient.generateSignedUrl('bucket-name', 'path/to cfg.json', 3600) };",
    )
    assert proc.returncode == 0
    data = _parse_result(proc)
    assert data["signedUrl"].startswith("https://storage.googleapis.com/bucket-name/path/to%20cfg.json")


def test_upload_retries_until_success(monkeypatch):
    prelude = """var responses = [
  { code: 500, text: 'error-1' },
  { code: 502, text: 'error-2' },
  { code: 200, text: 'ok' }
];
var attempt = 0;
var UrlFetchApp = {
  fetch: function(url, options) {
    var response = responses.shift();
    if (!response) {
      response = { code: 200, text: 'ok' };
    }
    attempt++;
    return {
      getResponseCode: function() { return response.code; },
      getContentText: function() { return response.text; }
    };
  }
};
"""
    body = """return (function() {
  try {
    var result = StorageClient.uploadClientConfig(123, { foo: 'bar' });
    return {
      status: 'ok',
      bucket: result.bucket,
      objectUri: result.objectUri,
      attempts: attempt,
      sleeps: globalThis.__sleepLog || []
    };
  } catch (err) {
    return { status: 'error', message: err.message, attempts: attempt, sleeps: globalThis.__sleepLog || [] };
  }
})();
"""
    proc = _eval_module(monkeypatch, prelude=prelude, body=body)
    data = _parse_result(proc)
    assert data["status"] == "ok"
    assert data["bucket"] == "bucket-name"
    assert data["attempts"] == 3
    assert data["sleeps"] == [1000, 2000]


def test_upload_retries_fail_after_max(monkeypatch):
    prelude = """var responses = [
  { code: 500, text: 'error-1' },
  { code: 502, text: 'error-2' },
  { code: 503, text: 'error-3' }
];
var attempt = 0;
var UrlFetchApp = {
  fetch: function(url, options) {
    var response = responses.shift();
    if (!response) {
      response = { code: 503, text: 'error-final' };
    }
    attempt++;
    return {
      getResponseCode: function() { return response.code; },
      getContentText: function() { return response.text; }
    };
  }
};
"""
    body = """return (function() {
  try {
    StorageClient.uploadClientConfig(123, { foo: 'bar' });
    return { status: 'ok', attempts: attempt, sleeps: globalThis.__sleepLog || [] };
  } catch (err) {
    return { status: 'error', message: err.message, attempts: attempt, sleeps: globalThis.__sleepLog || [] };
  }
})();
"""
    proc = _eval_module(monkeypatch, prelude=prelude, body=body)
    data = _parse_result(proc)
    assert data["status"] == "error"
    assert "client_config のアップロードに失敗" in data["message"]
    assert data["attempts"] == 3
    assert data["sleeps"] == [1000, 2000]
