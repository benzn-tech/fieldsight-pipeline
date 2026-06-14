/* ==========================================================================
   FieldSight API · Sites & Users — BACKEND-CONTEXT §4.2
   --------------------------------------------------------------------------
   GET /api/sites                       → { sites, role, display_name }
   GET /api/site-users?site=<site_id>   → { users, site }
   GET /api/users                       → { users }
   ========================================================================== */

(function () {
  'use strict';

  function fixtures() {
    return (window.FieldSight && window.FieldSight.fixtures) || {};
  }

  async function getSites() {
    if (!window.FS.api.useMocks) return window.FS.api.request('/sites');
    await window.FS.api.delay();
    var f  = fixtures().sites || { sites: [], users: [] };
    var u  = (window.AuthMock && window.AuthMock.currentUser) || {};
    return {
      sites:        f.sites,
      role:         u.role || 'site_manager',
      display_name: u.name || 'Jarley Trainor',
    };
  }

  async function getSiteUsers(site) {
    if (!window.FS.api.useMocks) return window.FS.api.request('/site-users', { params: { site: site } });
    await window.FS.api.delay();
    var f = fixtures().sites || { users: [] };
    var users = f.users.filter(function (u) {
      return (u.sites || []).indexOf(site) !== -1;
    });
    return { users: users, site: site };
  }

  async function getUsers() {
    if (!window.FS.api.useMocks) return window.FS.api.request('/users');
    await window.FS.api.delay();
    var f = fixtures().sites || { users: [] };
    return { users: f.users };
  }

  window.FS.api.sites = {
    getSites:     getSites,
    getSiteUsers: getSiteUsers,
    getUsers:     getUsers,
  };

})();
