define(['jquery'], function ($) {
  'use strict';

  return function () {
    var self = this;
    var atsUrl = 'https://axiomats.ru';

    this.callbacks = {
      init: function () { return true; },
      render: function () { return true; },
      bind_actions: function () { return true; },
      settings: function () { return true; },
      onSave: function () { return true; },
      destroy: function () { return true; }
    };

    this.callThroughAts = function (crmContactId, phone) {
      return $.ajax({
        url: atsUrl + '/api/v1/workspace/amo/click-to-call',
        method: 'POST',
        contentType: 'application/json',
        xhrFields: { withCredentials: true },
        data: JSON.stringify({
          crm_contact_id: Number(crmContactId),
          phone: String(phone || '')
        })
      });
    };
  };
});
