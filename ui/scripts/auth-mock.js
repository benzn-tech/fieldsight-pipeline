/* ==========================================================================
   FieldSight Auth Mock — dev only
   window.AuthMock
   ========================================================================== */

(function () {
  'use strict';

  const listeners = new Set();

  function emit() {
    listeners.forEach(cb => { try { cb(AuthMock.currentUser); } catch (e) { console.error('[AuthMock]', e); } });
  }

  const AuthMock = {
    currentUser: {
      id: 'user_001',
      name: 'Jarley Trainor',
      initials: 'JT',
      role: 'site_manager',
      isAdmin: false,
      avatarColor: '#2E75B6',
      site: 'Ellesmere',
    },

    setRole(roleName) {
      const roles = window.FS && window.FS.ROLES;
      if (roles && !roles[roleName]) {
        console.warn('[AuthMock] Unknown role:', roleName);
        return;
      }
      AuthMock.currentUser = Object.assign({}, AuthMock.currentUser, {
        role: roleName,
        isAdmin: false,
      });
      emit();
    },

    setAdmin(isAdmin) {
      AuthMock.currentUser = Object.assign({}, AuthMock.currentUser, { isAdmin: !!isAdmin });
      emit();
    },

    setUser(user) {
      AuthMock.currentUser = Object.assign({}, user);
      // Derive initials if not provided
      if (!AuthMock.currentUser.initials && AuthMock.currentUser.name) {
        AuthMock.currentUser.initials = AuthMock.currentUser.name
          .split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
      }
      emit();
    },

    onChange(callback) {
      listeners.add(callback);
      return () => listeners.delete(callback);
    },
  };

  // Ensure initials are set on the default user
  AuthMock.currentUser.initials = 'JT';

  window.AuthMock = AuthMock;

})();
