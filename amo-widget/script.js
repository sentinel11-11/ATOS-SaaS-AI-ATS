define(['jquery'], function ($) {
  'use strict';

  return function () {
    var self = this;
    var mountTimer = null;
    var observer = null;
    var rootId = 'atos-ats-widget-root';
    var cardButtonClass = 'atos-ats-card-call';

    var text = {
      call: 'Позвонить через ATOS-ATS',
      launcher: 'АТС',
      panelTitle: 'Звонок через ATOS-ATS',
      phonePlaceholder: 'Телефон контакта',
      callButton: 'Позвонить',
      refreshButton: 'Взять из карточки',
      loading: 'Запуск звонка…',
      queued: 'Звонок поставлен в очередь',
      noPhone: 'Укажите номер телефона',
      networkError: 'Нет связи с ATOS-ATS',
      genericError: 'Не удалось запустить звонок'
    };

    function settings() {
      try {
        return (typeof self.get_settings === 'function' && self.get_settings()) || {};
      } catch (error) {
        return {};
      }
    }

    function atsUrl() {
      var configured = String(settings().atsUrl || '').trim();
      if (!configured) configured = 'https://axiomats.ru';
      return configured.replace(/\/+$/, '');
    }

    function systemValue(name) {
      try {
        var system = typeof self.system === 'function' ? self.system() : null;
        return system && system[name] != null ? system[name] : null;
      } catch (error) {
        return null;
      }
    }

    function amoUserId() {
      var value = Number(systemValue('user_id'));
      return isFinite(value) && value > 0 ? value : null;
    }

    function normalizePhone(value) {
      var raw = String(value || '').trim();
      if (!raw) return '';
      var hasPlus = raw.charAt(0) === '+';
      var digits = raw.replace(/[^0-9]/g, '');
      if (digits.length < 10) return '';
      return (hasPlus ? '+' : '') + digits;
    }

    function phoneFromNode(node) {
      var $node = $(node);
      var href = String($node.attr('href') || '').replace(/^tel:/i, '');
      var value = $node.attr('data-value') || $node.attr('data-phone') || href || $node.text();
      var phone = normalizePhone(value);
      if (phone) return phone;
      return normalizePhone($node.find('a').first().attr('href')) || normalizePhone($node.text());
    }

    function findPhoneNode() {
      var selectors = [
        '[data-field-code="PHONE"] a',
        '[data-field-code="PHONE"]',
        'a[href^="tel:"]'
      ];
      var found = null;
      selectors.some(function (selector) {
        var $nodes = $(selector).filter(':visible');
        if (!$nodes.length) $nodes = $(selector);
        for (var i = 0; i < $nodes.length; i += 1) {
          if (phoneFromNode($nodes.eq(i))) {
            found = $nodes.eq(i);
            return true;
          }
        }
        return false;
      });
      if (found) return found;

      var phonePattern = /(?:\+7|8|7)\s*[\(]?\d{3}[\)]?[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}/;
      var $links = $('a').filter(':visible');
      if (!$links.length) $links = $('a');
      for (var i = 0; i < $links.length; i += 1) {
        if (phonePattern.test($links.eq(i).text())) return $links.eq(i);
      }
      return $();
    }

    function currentPhone() {
      var node = findPhoneNode();
      return node.length ? phoneFromNode(node) : '';
    }

    function visibleRoot() {
      var $root = $('#' + rootId);
      return $root.length ? $root.first() : $();
    }

    function setStatus(message, isError) {
      var $root = visibleRoot();
      var $status = $root.find('.atos-ats-status');
      $status.text(message || '').toggleClass('atos-ats-status-error', !!isError);
    }

    function setPhoneFromCard(force) {
      var $root = visibleRoot();
      var phone = currentPhone();
      var $input = $root.find('.atos-ats-phone');
      if (phone && (force || !$input.val())) $input.val(phone);
      return phone;
    }

    function errorText(xhr) {
      if (!xhr || xhr.status === 0) return text.networkError;
      var response = xhr.responseJSON || {};
      return response.error || text.genericError;
    }

    function startCall(button, phoneInput) {
      var $button = $(button);
      var phone = normalizePhone(phoneInput || '');
      if (!phone) {
        setStatus(text.noPhone, true);
        $button.prop('disabled', false);
        return;
      }
      $button.prop('disabled', true).text(text.loading);
      setStatus(text.loading, false);
      self.callThroughAts(phone)
        .done(function () {
          $button.text(text.queued);
          setStatus(text.queued, false);
        })
        .fail(function (xhr) {
          $button.prop('disabled', false).text(text.callButton);
          setStatus(errorText(xhr), true);
          if (window.console && console.error) console.error('ATOS-ATS click-to-call failed', xhr);
        });
    }

    function mountLauncher() {
      if (!document.body || visibleRoot().length) return visibleRoot().length > 0;
      var html = '' +
        '<div id="' + rootId + '" class="atos-ats-widget-root">' +
          '<button type="button" class="atos-ats-launcher" aria-expanded="false" title="' + text.call + '">' +
            '<span class="atos-ats-launcher-icon" aria-hidden="true">☎</span>' +
            '<span class="atos-ats-launcher-label">' + text.launcher + '</span>' +
          '</button>' +
          '<section class="atos-ats-panel" hidden aria-label="' + text.panelTitle + '">' +
            '<div class="atos-ats-panel-title">' + text.panelTitle + '</div>' +
            '<input class="atos-ats-phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="' + text.phonePlaceholder + '">' +
            '<div class="atos-ats-panel-actions">' +
              '<button type="button" class="atos-ats-refresh">' + text.refreshButton + '</button>' +
              '<button type="button" class="atos-ats-submit">' + text.callButton + '</button>' +
            '</div>' +
            '<div class="atos-ats-status" role="status" aria-live="polite"></div>' +
          '</section>' +
        '</div>';
      $('body').append(html);
      setPhoneFromCard(true);
      return true;
    }

    function mountCardButton() {
      var phoneNode = findPhoneNode();
      if (!phoneNode.length) return false;
      if (phoneNode.closest('[data-atos-ats-card-button="1"]').length) return true;
      if (phoneNode.siblings('.' + cardButtonClass).length) return true;
      phoneNode.after('<button type="button" class="' + cardButtonClass + '" data-atos-ats-card-button="1">' + text.call + '</button>');
      return true;
    }

    self.mountWidget = function () {
      // Remove controls left by an older cached widget version.
      $('.atos-ats-call-widget').remove();
      mountLauncher();
      mountCardButton();
      setPhoneFromCard(false);
      return true;
    };

    self.callbacks = {
      init: function () {
        return true;
      },
      bind_actions: function () {
        $(document)
          .off('click.atosAts', '.atos-ats-launcher')
          .on('click.atosAts', '.atos-ats-launcher', function (event) {
            event.preventDefault();
            event.stopPropagation();
            var $root = visibleRoot();
            var $panel = $root.find('.atos-ats-panel');
            var open = !$panel.prop('hidden');
            $panel.prop('hidden', open);
            $(this).attr('aria-expanded', String(!open));
            if (!open) setPhoneFromCard(false);
          })
          .off('click.atosAts', '.atos-ats-refresh')
          .on('click.atosAts', '.atos-ats-refresh', function (event) {
            event.preventDefault();
            event.stopPropagation();
            setPhoneFromCard(true);
            setStatus('', false);
          })
          .off('click.atosAts', '.atos-ats-submit')
          .on('click.atosAts', '.atos-ats-submit', function (event) {
            event.preventDefault();
            event.stopPropagation();
            var $root = visibleRoot();
            startCall(this, $root.find('.atos-ats-phone').val());
          })
          .off('click.atosAts', '.' + cardButtonClass)
          .on('click.atosAts', '.' + cardButtonClass, function (event) {
            event.preventDefault();
            event.stopPropagation();
            startCall(this, currentPhone());
          });
        return true;
      },
      render: function () {
        self.mountWidget();
        if (!mountTimer) mountTimer = setInterval(self.mountWidget, 1000);
        if (!observer && window.MutationObserver && document.body) {
          observer = new MutationObserver(function () { self.mountWidget(); });
          observer.observe(document.body, { childList: true, subtree: true });
        }
        return true;
      },
      settings: function () {
        return true;
      },
      onSave: function () {
        return true;
      },
      destroy: function () {
        if (mountTimer) clearInterval(mountTimer);
        mountTimer = null;
        if (observer) observer.disconnect();
        observer = null;
        $(document).off('.atosAts');
        $('#' + rootId).remove();
        $('.' + cardButtonClass).remove();
        return true;
      }
    };

    self.callThroughAts = function (phone) {
      var payload = {
        phone: String(phone || ''),
        amo_user_id: amoUserId(),
        crm_contact_id: null,
        request_id: 'amo-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10)
      };
      return $.ajax({
        url: atsUrl() + '/api/v1/amo/widget/click-to-call',
        type: 'POST',
        data: JSON.stringify(payload),
        contentType: 'application/json; charset=UTF-8',
        dataType: 'json',
        headers: { Accept: 'application/json' },
        timeout: 15000
      });
    };
  };
});
