/* ==========================================================================
   FieldSight Fixtures · Sites & Users
   --------------------------------------------------------------------------
   Mirrors BACKEND-CONTEXT §6 (config/user_mapping.json) shape — three sites
   with a few users each. Roles match the mapping convention (worker /
   site_manager / pm); admin & gm exist only in Cognito profiles per the
   doc, so they're not represented here.

   Sprint 9 (Track A.0) extensions — fields that exist UI-side only,
   awaiting a backend mirror (see PLAN §6 "Backend wiring for Sprint 9
   schema"):
     • SUBCONTRACTORS — directory of sub-orgs operating on each site
     • users[].subcontractor_id — FK into SUBCONTRACTORS for who_raised
       resolution on the /insights dashboard
     • users[].managed_sites — populated on PM users so Track B can
       site-scope the /team page (PM sees only their managed sites)

   Exported to window.FieldSight.fixtures.sites = {
     sites, users, subcontractors }
   ========================================================================== */

(function () {
  'use strict';

  var SITES = [
    {
      site_id:    'sb1108-ellesmere',
      name:       'SB1108 Ellesmere College',
      location:   'Christchurch',
      region:     'south-island',          /* Sprint 9 C — strategic grouping */
      client:     'Ministry of Education',
      project_value_nzd: 12_400_000,        /* used by /executive KPI rollup */
      planned_completion: '2026-09-30',
      user_count: 4,
    },
    {
      site_id:    'mpi',
      name:       'MPI',
      location:   'Auckland',
      region:     'north-island',
      client:     'Ministry for Primary Industries',
      project_value_nzd: 8_900_000,
      planned_completion: '2026-08-15',
      user_count: 2,
    },
    {
      site_id:    'sb1131-northbrook-wanaka',
      name:       'SB1131 - Northbrook Wanaka',
      location:   'Wanaka',
      region:     'south-island',
      client:     'Northbrook Group',
      project_value_nzd: 6_700_000,
      planned_completion: '2026-11-20',
      user_count: 2,
    },
  ];

  /* Sprint 9 C — region directory (lookup for /regional + /executive). */
  var REGIONS = [
    { id: 'south-island', name: 'South Island', country: 'NZ' },
    { id: 'north-island', name: 'North Island', country: 'NZ' },
  ];

  /* Sprint 9 A.0 — subcontractor directory.
     Each subcontractor operates on one or more sites. The
     /insights dashboard groups safety/quality issues by
     subcontractor_id to surface "who's triggering the most issues
     this week". Names chosen to mirror typical NZ construction
     trade splits. */
  var SUBCONTRACTORS = [
    { id: 'sub-mainline-civil',     name: 'Mainline Civil',
      trade: 'Civil works',         sites: ['sb1108-ellesmere'] },
    { id: 'sub-apex-scaffolding',   name: 'Apex Scaffolding',
      trade: 'Scaffolding',         sites: ['sb1108-ellesmere','sb1131-northbrook-wanaka'] },
    { id: 'sub-hartco-electrical',  name: 'Hartco Electrical',
      trade: 'Electrical',          sites: ['sb1108-ellesmere','mpi'] },
    { id: 'sub-pacific-concrete',   name: 'Pacific Concrete',
      trade: 'Concrete',            sites: ['mpi','sb1131-northbrook-wanaka'] },
    { id: 'sub-southern-steel',     name: 'Southern Steel Fixers',
      trade: 'Reinforcement',       sites: ['mpi'] },
    { id: 'sub-coastline-roofing',  name: 'Coastline Roofing',
      trade: 'Roofing',             sites: ['sb1108-ellesmere','sb1131-northbrook-wanaka'] },
    { id: 'sub-fieldsight-internal',name: 'FieldSight (internal)',
      trade: 'Site management',     sites: ['sb1108-ellesmere','mpi','sb1131-northbrook-wanaka'] },
  ];

  /* device_id is the primary key (BACKEND-CONTEXT §6).
     folder_name = name.replace(' ', '_').
     Sprint 9 A.0 — added subcontractor_id (org affiliation).
     Sprint 9 B — added managed_sites on PM users. */
  var USERS = [
    { device_id: 'Benl1', name: 'Jarley Trainor',  folder_name: 'Jarley_Trainor',
      role: 'site_manager', primary_site: 'sb1108-ellesmere',
      sites: ['sb1108-ellesmere'],
      subcontractor_id: 'sub-fieldsight-internal' },
    { device_id: 'Benl2', name: 'David Barillaro', folder_name: 'David_Barillaro',
      role: 'worker',       primary_site: 'sb1108-ellesmere',
      sites: ['sb1108-ellesmere'],
      subcontractor_id: 'sub-mainline-civil' },
    { device_id: 'Benl3', name: 'Ben Lin',         folder_name: 'Ben_Lin',
      role: 'worker',       primary_site: 'sb1108-ellesmere',
      sites: ['sb1108-ellesmere'],
      subcontractor_id: 'sub-apex-scaffolding' },
    { device_id: 'Benl4', name: 'Sarah Chen',      folder_name: 'Sarah_Chen',
      role: 'worker',       primary_site: 'sb1108-ellesmere',
      sites: ['sb1108-ellesmere'],
      subcontractor_id: 'sub-hartco-electrical' },
    { device_id: 'Benl5', name: 'James Lamb',      folder_name: 'James_Lamb',
      role: 'pm',           primary_site: 'mpi',
      sites: ['mpi','sb1131-northbrook-wanaka'],
      managed_sites: ['mpi','sb1131-northbrook-wanaka'],
      subcontractor_id: 'sub-fieldsight-internal' },
    { device_id: 'Benl6', name: 'MPI1',            folder_name: 'MPI1',
      role: 'worker',       primary_site: 'mpi',
      sites: ['mpi'],
      subcontractor_id: 'sub-pacific-concrete' },
    { device_id: 'Benl7', name: 'Priya Sharma',    folder_name: 'Priya_Sharma',
      role: 'worker',       primary_site: 'sb1131-northbrook-wanaka',
      sites: ['sb1131-northbrook-wanaka'],
      subcontractor_id: 'sub-coastline-roofing' },
    { device_id: 'Benl8', name: 'Mike OBrien',     folder_name: 'Mike_OBrien',
      role: 'worker',       primary_site: 'sb1131-northbrook-wanaka',
      sites: ['sb1131-northbrook-wanaka'],
      subcontractor_id: 'sub-apex-scaffolding' },
  ];

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.fixtures) window.FieldSight.fixtures = {};
  window.FieldSight.fixtures.sites = {
    sites:          SITES,
    users:          USERS,
    subcontractors: SUBCONTRACTORS,
    regions:        REGIONS,
  };

})();
