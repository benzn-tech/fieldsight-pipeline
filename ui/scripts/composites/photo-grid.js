/* ==========================================================================
   FieldSight PhotoGrid — Layer 5 composite
   --------------------------------------------------------------------------
   Thumbnail grid for topic.related_photos (BACKEND-CONTEXT §5.1, §7).

   For each filename we build the S3 key
     users/{folder_name}/pictures/{date}/{filename}
   and resolve a presigned URL via FS.api.media.presignedUrl. URLs expire
   in 15 min (BUG §7) — we re-fetch on mount and don't cache; if the user
   closes and re-opens the modal/tab, this remounts and re-fetches.

   Click a thumb to open a lightbox-style overlay.

   Props:
     photos          string[] — filenames (NOT keys)
     userDisplayName string   — owner's display name (folder = name with
                                spaces → underscores)
     date            'YYYY-MM-DD'

   Exported to:
     window.FieldSight.PhotoGrid
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function PhotoGrid(props) {
    var photos   = props.photos || [];
    var userName = props.userDisplayName;
    var date     = props.date;
    /* Sprint 6.6.3 — variant='carousel' switches the layout to a
       horizontal-scrolling row (default 2 cells visible, swipe/scroll
       for more). Used by /safety + /quality right panels where we
       embed photos inline as supporting context. variant='grid'
       (default) keeps the classic auto-fill grid used by topic-card. */
    var variant = props.variant === 'carousel' ? 'carousel' : 'grid';

    var refLight = React.useState(null);
    var lightbox    = refLight[0];
    var setLightbox = refLight[1];

    if (photos.length === 0) {
      return React.createElement('div', { className: 'fs-photo-grid__empty' },
        'No photos for this topic.');
    }

    return React.createElement(React.Fragment, null,
      React.createElement('div', {
        className: 'fs-photo-grid fs-photo-grid--' + variant,
      },
        photos.map(function (filename, i) {
          var s3Key = window.FS.api.media.photoKey({
            userDisplayName: userName, date: date, filename: filename,
          });
          return React.createElement(PhotoCell, {
            key:      i,
            s3Key:    s3Key,
            filename: filename,
            onOpen:   function (url) {
              setLightbox({ url: url, filename: filename });
            },
          });
        }),
      ),

      lightbox ? React.createElement(PhotoLightbox, {
        url:      lightbox.url,
        filename: lightbox.filename,
        onClose:  function () { setLightbox(null); },
      }) : null,
    );
  }

  /* ---------- single thumb ---------------------------------------------- */

  function PhotoCell(props) {
    var refUrl = React.useState(null);
    var url    = refUrl[0];
    var setUrl = refUrl[1];

    var refErr = React.useState(false);
    var errored    = refErr[0];
    var setErrored = refErr[1];

    React.useEffect(function () {
      var cancelled = false;
      window.FS.api.media.presignedUrl(props.s3Key).then(function (res) {
        if (!cancelled) setUrl(res.url);
      }).catch(function () {
        if (!cancelled) setErrored(true);
      });
      return function () { cancelled = true; };
    }, [props.s3Key]);

    function open() {
      if (url) props.onOpen(url);
    }

    return React.createElement('button', {
      type:      'button',
      className: 'fs-photo-grid__cell',
      onClick:   open,
      'aria-label': 'View ' + props.filename,
    },
      React.createElement('div', { className: 'fs-photo-grid__thumb' },
        url && !errored
          ? React.createElement('img', {
              src:       url,
              alt:       props.filename,
              loading:   'lazy',
              className: 'fs-photo-grid__img',
              onError:   function () { setErrored(true); },
            })
          : React.createElement('div', { className: 'fs-photo-grid__placeholder' },
              errored ? 'Preview unavailable' : 'Loading…'),
      ),
      React.createElement('div', { className: 'fs-photo-grid__caption' },
        props.filename),
    );
  }

  /* ---------- lightbox -------------------------------------------------- */

  function PhotoLightbox(props) {
    React.useEffect(function () {
      function onKey(e) { if (e.key === 'Escape') props.onClose(); }
      document.addEventListener('keydown', onKey);
      return function () { document.removeEventListener('keydown', onKey); };
    }, [props.onClose]);

    return React.createElement('div', {
      className: 'fs-photo-lightbox',
      role:      'dialog',
      'aria-label': props.filename,
      onClick:   props.onClose,
    },
      React.createElement('div', {
        className: 'fs-photo-lightbox__inner',
        onClick:   function (e) { e.stopPropagation(); },
      },
        React.createElement('img', {
          src: props.url, alt: props.filename,
          className: 'fs-photo-lightbox__img',
        }),
        React.createElement('div', { className: 'fs-photo-lightbox__caption' },
          props.filename),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.PhotoGrid = PhotoGrid;
})();
