define(['jquery'], function ($) {
  'use strict';

  return function () {
    var self = this;
    var atsUrl = 'https://axiomats.ru';

    this.callbacks = {
      init: function () { return true; },
      render: function () {
        var phoneNode = $('[data-field-code="PHONE"]').first();
        if (phoneNode.length && !$('.atos-ats-call-widget').length) {
          phoneNode.after('<button type="button" class="atos-ats-call-widget">Позвонить через ATOS-ATS</button>');
        }
        return true;
      },
      bind_actions: function () {
        $(document).off('click.atosAts', '.atos-ats-call-widget').on('click.atosAts', '.atos-ats-call-widget', function () {
            var button = $(this);
            var entityId = self.system && self.system().entity_id;
            var phoneNode = $('[data-field-code="PHONE"] a, [data-field-code="PHONE"]');
            var phone = phoneNode.first().text().replace(/[^+0-9]/g, '');
            if (!entityId || !phone) {
              button.text('Не найден телефон').prop('disabled', true);
              return;
            }
            button.prop('disabled', true).text('Запуск звонка…');
            self.callThroughAts(entityId, phone, self.system && self.system().user_id)
              .done(function () { button.text('Звонок поставлен в очередь'); })
              .fail(function (xhr) { button.prop('disabled', false).text((xhr.responseJSON && xhr.responseJSON.error) || 'Ошибка запуска'); });
        });
        return true;
      },
      settings: function () { return true; },
      onSave: function () { return true; },
      destroy: function () { return true; }
    };

    this.callThroughAts = function (crmContactId, phone, amoUserId) {
      return $.ajax({
        url: atsUrl + '/api/v1/amo/widget/click-to-call',
        method: 'POST',
        contentType: 'application/json',
        xhrFields: { withCredentials: true },
        data: JSON.stringify({
          crm_contact_id: Number(crmContactId),
          phone: String(phone || ''),
          amo_user_id: amoUserId ? Number(amoUserId) : null
        })
      });
    };
  };
});
