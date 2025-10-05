/**
 * サービスアカウントキーを用いたトークン発行・署名ユーティリティ
 * ScriptProperties に SERVICE_ACCOUNT_JSON を保存して利用する。
 */
var ServiceAccountClient = (function() {
  var ACCESS_TOKEN_CACHE = {};
  var ID_TOKEN_CACHE = {};
  var SERVICE_ACCOUNT_INFO = null;

  function getServiceAccountInfo_() {
    if (SERVICE_ACCOUNT_INFO) {
      return SERVICE_ACCOUNT_INFO;
    }
    var json = PropertiesService.getScriptProperties().getProperty('SERVICE_ACCOUNT_JSON');
    if (!json) {
      throw new Error('SERVICE_ACCOUNT_JSON が設定されていません');
    }
    SERVICE_ACCOUNT_INFO = JSON.parse(json);
    if (!SERVICE_ACCOUNT_INFO.private_key || !SERVICE_ACCOUNT_INFO.client_email) {
      throw new Error('SERVICE_ACCOUNT_JSON の形式が不正です');
    }
    SERVICE_ACCOUNT_INFO.private_key = SERVICE_ACCOUNT_INFO.private_key.replace(/\\n/g, '\n');
    return SERVICE_ACCOUNT_INFO;
  }

  function buildAccessTokenJwt_(scopes) {
    var info = getServiceAccountInfo_();
    var header = {
      alg: 'RS256',
      typ: 'JWT'
    };
    var now = Math.floor(new Date().getTime() / 1000);
    var claim = {
      iss: info.client_email,
      scope: scopes.join(' '),
      aud: 'https://oauth2.googleapis.com/token',
      exp: now + 3600,
      iat: now
    };
    var headerEncoded = Utilities.base64EncodeWebSafe(JSON.stringify(header));
    var claimEncoded = Utilities.base64EncodeWebSafe(JSON.stringify(claim));
    var toSign = headerEncoded + '.' + claimEncoded;
    var signatureBytes = Utilities.computeRsaSha256Signature(toSign, info.private_key);
    var signatureEncoded = Utilities.base64EncodeWebSafe(signatureBytes);
    return toSign + '.' + signatureEncoded;
  }

  function fetchAccessToken_(scopes) {
    var jwt = buildAccessTokenJwt_(scopes);
    var response = UrlFetchApp.fetch('https://oauth2.googleapis.com/token', {
      method: 'post',
      contentType: 'application/x-www-form-urlencoded',
      payload: {
        grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        assertion: jwt
      },
      muteHttpExceptions: true
    });
    if (response.getResponseCode() >= 300) {
      throw new Error('アクセストークン取得に失敗: ' + response.getContentText());
    }
    var data = JSON.parse(response.getContentText());
    var expiresAt = Math.floor(new Date().getTime() / 1000) + (Number(data.expires_in) || 3600);
    return {
      token: data.access_token,
      expires: expiresAt
    };
  }

  function getAccessToken(scopes) {
    var key = scopes.slice().sort().join(' ');
    var cached = ACCESS_TOKEN_CACHE[key];
    var now = Math.floor(new Date().getTime() / 1000);
    if (cached && cached.expires - 120 > now) {
      return cached.token;
    }
    var tokenInfo = fetchAccessToken_(scopes);
    ACCESS_TOKEN_CACHE[key] = tokenInfo;
    return tokenInfo.token;
  }

  function buildIdTokenAssertion_(audience) {
    var info = getServiceAccountInfo_();
    var header = {
      alg: 'RS256',
      typ: 'JWT'
    };
    var now = Math.floor(new Date().getTime() / 1000);
    var claim = {
      iss: info.client_email,
      sub: info.client_email,
      aud: 'https://oauth2.googleapis.com/token',
      target_audience: audience,
      exp: now + 3600,
      iat: now
    };
    var headerEncoded = Utilities.base64EncodeWebSafe(JSON.stringify(header));
    var claimEncoded = Utilities.base64EncodeWebSafe(JSON.stringify(claim));
    var toSign = headerEncoded + '.' + claimEncoded;
    var signatureBytes = Utilities.computeRsaSha256Signature(toSign, info.private_key);
    var signatureEncoded = Utilities.base64EncodeWebSafe(signatureBytes);
    return toSign + '.' + signatureEncoded;
  }

  function parseIdTokenExpiry_(token) {
    if (!token) {
      return null;
    }
    var parts = token.split('.');
    if (parts.length < 2) {
      return null;
    }
    try {
      var payload = parts[1];
      var padLength = (4 - (payload.length % 4)) % 4;
      if (padLength > 0) {
        payload += Array(padLength + 1).join('=');
      }
      payload = payload.replace(/-/g, '+').replace(/_/g, '/');
      var decoded = Utilities.newBlob(Utilities.base64Decode(payload)).getDataAsString();
      var json = JSON.parse(decoded);
      if (json && json.exp) {
        return Number(json.exp);
      }
    } catch (error) {
      // ignore parse errors; fallback handled by caller
    }
    return null;
  }

  function fetchIdToken_(audience) {
    var assertion = buildIdTokenAssertion_(audience);
    var response = UrlFetchApp.fetch('https://oauth2.googleapis.com/token', {
      method: 'post',
      contentType: 'application/x-www-form-urlencoded',
      payload: {
        grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        assertion: assertion
      },
      muteHttpExceptions: true
    });
    if (response.getResponseCode() >= 300) {
      throw new Error('ID トークン取得に失敗: ' + response.getContentText());
    }
    var data = JSON.parse(response.getContentText() || '{}');
    if (!data.id_token) {
      throw new Error('ID トークン取得に失敗: response に id_token が含まれていません');
    }
    var expiresAt = parseIdTokenExpiry_(data.id_token);
    if (!expiresAt) {
      var now = Math.floor(new Date().getTime() / 1000);
      expiresAt = now + (Number(data.expires_in) || 3600);
    }
    return {
      token: data.id_token,
      expires: expiresAt
    };
  }

  function getIdToken(audience) {
    if (!audience) {
      throw new Error('ID トークンの audience が指定されていません');
    }
    var key = String(audience);
    var cached = ID_TOKEN_CACHE[key];
    var now = Math.floor(new Date().getTime() / 1000);
    if (cached && cached.expires - 120 > now) {
      return cached.token;
    }
    var tokenInfo = fetchIdToken_(audience);
    ID_TOKEN_CACHE[key] = tokenInfo;
    return tokenInfo.token;
  }

  function signBytes(data) {
    var info = getServiceAccountInfo_();
    var signature = Utilities.computeRsaSha256Signature(data, info.private_key);
    var hex = signature.map(function(b) {
      var h = (b & 0xff).toString(16);
      return h.length === 1 ? '0' + h : h;
    }).join('');
    return hex;
  }

  function getServiceAccountEmail() {
    return getServiceAccountInfo_().client_email;
  }

  return {
    getAccessToken: getAccessToken,
    getIdToken: getIdToken,
    signBytes: signBytes,
    getServiceAccountEmail: getServiceAccountEmail
  };
})();
