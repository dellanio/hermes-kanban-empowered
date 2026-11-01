// kanban-empowered has no web-dashboard page: its dashboard/ folder exists for the backend routes
// (plugin_api.py) used by the Hermes Desktop half. `hermes dashboard` still loads this entry for every
// plugin with a manifest, so it registers the documented slot-only placeholder (tab.hidden).
(function () {
  var P = window.__HERMES_PLUGINS__;
  if (P && P.register) P.register("kanban-empowered", function () { return null; });
})();
