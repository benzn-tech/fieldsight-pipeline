/* ==========================================================================
   FieldSight API · Generic media presigner — BACKEND-CONTEXT §4.9
   --------------------------------------------------------------------------
   GET /api/media/presigned-url?key=<urlencoded S3 key>
     → { url:'<https presigned>', expires_in: 900 }

   Allowed prefixes: users/, audio_segments/, transcripts/, reports/, web_video/.
   URLs expire in 15 min.

   Sprint 8.8.4 — presigned URL cache with TTL.
   Each URL is cached with its expiresAt timestamp. If the URL would
   expire within 2 minutes of access, a fresh one is fetched first.
   Use getUrl(key) (or presignedUrl(key)) from callers; both return
   the same { url, expires_in, key } shape.
   ========================================================================== */

(function () {
  'use strict';

  /* Map<key, { url, expiresAt }> */
  var _urlCache = new Map();

  var REFRESH_LEAD_MS = 2 * 60 * 1000; /* re-fetch 2 min before expiry */

  async function _fetchFresh(key) {
    var res;
    if (!window.FS.api.useMocks) {
      res = await window.FS.api.request('/media/presigned-url', { params: { key: key } });
    } else {
      await window.FS.api.delay(40);
      res = {
        url:        window.FS.api.mockPresignedUrl(key),
        expires_in: 900,
        key:        key,
      };
    }
    var expiresAt = Date.now() + (res.expires_in || 900) * 1000;
    _urlCache.set(key, { url: res.url, expiresAt: expiresAt });
    return res;
  }

  /* Main entry point — checks cache before hitting the server. */
  async function presignedUrl(key) {
    var cached = _urlCache.get(key);
    if (cached && (cached.expiresAt - Date.now()) >= REFRESH_LEAD_MS) {
      return { url: cached.url, expires_in: Math.round((cached.expiresAt - Date.now()) / 1000), key: key };
    }
    return _fetchFresh(key);
  }

  /* Alias used by PhotoGrid / VideoPlayer (PLAN Sprint 8.8.4). */
  var getUrl = presignedUrl;

  /* Convenience: build the S3 key for a topic.related_photos filename
     (BACKEND-CONTEXT §5.1 + §7). The folder name is the user's display
     name with spaces replaced by underscores. */
  function photoKey(opts) {
    return 'users/' + window.FS.api.folderName(opts.userDisplayName)
      + '/pictures/' + opts.date + '/' + opts.filename;
  }

  window.FS.api.media = {
    presignedUrl: presignedUrl,
    getUrl:       getUrl,
    photoKey:     photoKey,
  };

})();
