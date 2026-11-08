(function () {
  'use strict';
  var VERSION = '1.0.0';
  var settings = {};
  function api(path, body) {
    return fetch((settings.ats_url || 'https://axiomats.ru') + path, {
      method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (data) { if (!r.ok) throw new Error(data.error || 'ATS error'); return data; }); });
  }
  function init() { return true; }
  function bind_actions() { return true; }
  function render() { return true; }
  function call(crmContactId, phone) {
    return api('/api/v1/workspace/amo/click-to-call', {crm_contact_id: Number(crmContactId), phone: phone});
  }
  window.ATOS_ATS_WIDGET = {version: VERSION, init: init, bind_actions: bind_actions, render: render, call: call};
  if (typeof self !== 'undefined') {
    self.callbacks = {init: init, bind_actions: bind_actions, render: render, settings: render};
  }
}());
