/*
 * Minimal CSInterface-compatible bridge.
 *
 * Adapted from the locally installed PR_TOOLBOX panel. The full Adobe
 * CSInterface library is unnecessary here: Rough Cut only needs evalScript
 * and getSystemPath. Keeping this bridge small also avoids coupling the panel
 * to a particular CEP sample release.
 */
(function () {
  "use strict";

  if (window.CSInterface) {
    return;
  }

  var SystemPath = {
    USER_DATA: "userData",
    COMMON_FILES: "commonFiles",
    MY_DOCUMENTS: "myDocuments",
    APPLICATION: "application",
    EXTENSION: "extension",
    HOST_APPLICATION: "hostApplication"
  };

  function CSInterface() {}

  CSInterface.prototype.evalScript = function (script, callback) {
    var done = callback || function () {};
    if (!window.__adobe_cep__ || !window.__adobe_cep__.evalScript) {
      done("EvalScript engine is unavailable.");
      return;
    }
    window.__adobe_cep__.evalScript(script, done);
  };

  CSInterface.prototype.getSystemPath = function (pathType) {
    if (!window.__adobe_cep__ || !window.__adobe_cep__.getSystemPath) {
      return "";
    }
    return window.__adobe_cep__.getSystemPath(pathType);
  };

  window.SystemPath = SystemPath;
  window.CSInterface = CSInterface;
})();
