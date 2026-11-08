define(['jquery'], function ($) {
  'use strict';

  return function () {
    var self = this;
    var atsUrl = 'https://axiomats.ru';

    this.callbacks = {
      init: function () { return true; },
      render: function () {
        var area = self.system && self.system().area;
        if (area && !/card|lcard|ccard|comcard/i.test(String(area))) return true;
        var phoneNode = $('[data-field-code="PHONE"]').first();
        if (!phoneNode.length) phoneNode = $('a[href^="tel:"]').first();
        if (!phoneNode.length) {
          phoneNode = $('a').filter(function () { return /(?:\+7|8|7)\s*[\(]?\d{3}[\)]?[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}/.test($(this).text()); }).first();
        }
        if (phoneNode.length && !$('.atos-ats-call-widget').length) {
          phoneNode.after('<button type="button" class="atos-ats-call-widget">Позвонить через ATOS-ATS</button>');
        }
        return true;
      },
      bind_actions: function () {
        $(document).off('click.atosAts', '.atos-ats-call-widget').on('click.atosAts', '.atos-ats-call-widget', function () {
            var button = $(this);
            var entityId = self.system && self.system().entity_id;
            var phoneNode = $('[data-field-code="PHONE"] a, [data-field-code="PHONE"], a[href^="tel:"]');
            var phone = phoneNode.first().text().replace(/[^+0-9]/g, '') || String(phoneNode.first().attr('href') || '').replace(/^tel:/, '').replace(/[^+0-9]/g, '');
            if (!phone || phone.length < 11) {
              var bodyText = $('body').text();
              var match = bodyText.match(/(?:\+7|8|7)\s*[\(]?\d{3}[\)]?[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}/);
              if (match) phone = match[0].replace(/[^+0-9]/g, '');
            }
            if (!entityId || !phone) {
              button.text('Не найден телефон').prop('disabled', true);
              return;
            }
            button.prop('disabled', true).text('Запуск звонка…');
          self.callThroughAts(null, phone, self.system && self.system().user_id)
              .done(function () { button.text('Звонок поставлен в очередь'); })
              .fail(function (xhr) { button.prop('disabled', false).text((xhr.responseJSON && xhr.responseJSON.error) || 'Ошибка запуска'); });
        });
        return true;
      },
      settings: function () { return true; },
      onSave: function () { return true; },
      destroy: function () { $(document).off('click.atosAts', '.atos-ats-call-widget'); $('.atos-ats-call-widget').remove(); return true; }
    };

    this.callThroughAts = function (crmContactId, phone, amoUserId) {
      return $.ajax({
        url: atsUrl + '/api/v1/amo/widget/click-to-call',
        method: 'POST',
        contentType: 'application/json',
        xhrFields: { withCredentials: true },
        data: JSON.stringify({
          crm_contact_id: crmContactId ? Number(crmContactId) : null,
          phone: String(phone || ''),
          amo_user_id: amoUserId ? Number(amoUserId) : null
        })
      });
    };
  };
});
