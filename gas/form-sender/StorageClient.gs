/**
 * Cloud Storage への client_config 保存と署名付きURL生成を担当するクライアント
 */
var StorageClient = (function() {
  var serviceAccount = ServiceAccountClient;
  var MAX_UPLOAD_ATTEMPTS = 3;
  var MAX_SIGN_ATTEMPTS = 3;
  var RETRY_BASE_DELAY_MS = 1000;

  function getBucket_() {

    var bucket = PropertiesService.getScriptProperties().getProperty('FORM_SENDER_GCS_BUCKET');
    if (!bucket) {
      throw new Error('FORM_SENDER_GCS_BUCKET が設定されていません');
    }
    return bucket;
  }

  function uploadClientConfig(targetingId, payload, options) {
    options = options || {};
    var bucket = getBucket_();
    var dateJst = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd');
    var runId = options.runId || Utilities.getUuid();
    var objectName = dateJst + '/targeting-' + targetingId + '-' + runId + '.json';
    var url = 'https://storage.googleapis.com/upload/storage/v1/b/' + encodeURIComponent(bucket) + '/o?uploadType=media&name=' + encodeURIComponent(objectName);

    var lastError = null;
    for (var attempt = 1; attempt <= MAX_UPLOAD_ATTEMPTS; attempt++) {
      try {
        var token = serviceAccount.getAccessToken(['https://www.googleapis.com/auth/devstorage.read_write']);
        var response = UrlFetchApp.fetch(url, {
          method: 'post',
          contentType: 'application/json; charset=utf-8',
          payload: JSON.stringify(payload),
          headers: {
            Authorization: 'Bearer ' + token
          },
          muteHttpExceptions: true
        });

        if (response.getResponseCode() < 300) {
          return {
            bucket: bucket,
            objectName: objectName,
            objectUri: 'gs://' + bucket + '/' + objectName
          };
        }

        lastError = new Error('client_config のアップロードに失敗: ' + response.getResponseCode() + ' ' + response.getContentText());
      } catch (error) {
        lastError = error;
      }

      if (attempt < MAX_UPLOAD_ATTEMPTS) {
        Utilities.sleep(RETRY_BASE_DELAY_MS * Math.pow(2, attempt - 1));
      }
    }

    throw new Error('client_config のアップロードに失敗 (retries exceeded): ' + (lastError && lastError.message ? lastError.message : lastError));
  }

  function generateV4SignedUrl(bucket, objectName, expirationSeconds) {
    expirationSeconds = expirationSeconds || 54000; // 15時間
    var now = new Date();
    var isoDate = Utilities.formatDate(now, 'UTC', 'yyyyMMdd');
    var isoTimestamp = Utilities.formatDate(now, 'UTC', 'yyyyMMdd\'T\'HHmmss\'Z\'');
    var credentialScope = isoDate + '/auto/storage/goog4_request';
    var credential = serviceAccount.getServiceAccountEmail() + '/' + credentialScope;

    var canonicalUri = '/' + encodeURIComponent(bucket) + '/' + encodeURI(objectName).replace(/%5B/g, '[').replace(/%5D/g, ']');
    var canonicalHeaders = 'host:storage.googleapis.com\n';
    var signedHeaders = 'host';
    var canonicalQuery = [
      'X-Goog-Algorithm=GOOG4-RSA-SHA256',
      'X-Goog-Credential=' + encodeURIComponent(credential),
      'X-Goog-Date=' + isoTimestamp,
      'X-Goog-Expires=' + expirationSeconds,
      'X-Goog-SignedHeaders=' + signedHeaders
    ].join('&');
    var canonicalRequest = [
      'GET',
      canonicalUri,
      canonicalQuery,
      canonicalHeaders,
      '',
      signedHeaders,
      'UNSIGNED-PAYLOAD'
    ].join('\n');

    var hash = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, canonicalRequest, Utilities.Charset.UTF_8);
    var hashedRequest = hash.map(function(b) {
      var hex = (b & 0xff).toString(16);
      return hex.length === 1 ? '0' + hex : hex;
    }).join('');

    var stringToSign = 'GOOG4-RSA-SHA256\n' + isoTimestamp + '\n' + credentialScope + '\n' + hashedRequest;
    var signature = null;
    var signError = null;
    for (var signAttempt = 1; signAttempt <= MAX_SIGN_ATTEMPTS; signAttempt++) {
      try {
        signature = serviceAccount.signBytes(stringToSign);
        break;
      } catch (err) {
        signError = err;
        if (signAttempt < MAX_SIGN_ATTEMPTS) {
          Utilities.sleep(RETRY_BASE_DELAY_MS * Math.pow(2, signAttempt - 1));
        }
      }
    }
    if (!signature) {
      throw new Error('署名の生成に失敗しました: ' + (signError && signError.message ? signError.message : signError));
    }

    return 'https://storage.googleapis.com/' + bucket + '/' + encodeURI(objectName) + '?' + canonicalQuery + '&X-Goog-Signature=' + signature;
  }

  function deleteObject(bucket, objectName) {
    if (!bucket || !objectName) {
      return;
    }

    var url = 'https://storage.googleapis.com/storage/v1/b/' + encodeURIComponent(bucket) + '/o/' + encodeURIComponent(objectName);
    var token = serviceAccount.getAccessToken(['https://www.googleapis.com/auth/devstorage.read_write']);
    var response = UrlFetchApp.fetch(url, {
      method: 'delete',
      headers: {
        Authorization: 'Bearer ' + token
      },
      muteHttpExceptions: true
    });

    var status = response.getResponseCode();
    if (status >= 300 && status !== 404) {
      throw new Error('client_config の削除に失敗: ' + status + ' ' + response.getContentText());
    }
  }

  return {
    uploadClientConfig: uploadClientConfig,
    generateSignedUrl: generateV4SignedUrl,
    deleteObject: deleteObject
  };
})();
