define(['jquery'], function ($) {
  'use strict';

  return function () {
    var self = this;
    var rootId = 'atos-ats-widget-root';
    var cardButtonClass = 'atos-ats-card-call';
    var mountTimer = null;
    var observer = null;
    var boundDocument = null;
    var version = '2.0.0';

    var text = {
      call: 'Позвонить через ATOS-ATS',
      launcher: 'АТС',
      panelTitle: 'Звонок через ATOS-ATS',
      phonePlaceholder: 'Телефон из открытой карточки',
      callButton: 'Позвонить',
      refreshButton: 'Обновить из карточки',
      loading: 'Запуск звонка…',
      queued: 'Звонок поставлен в очередь',
      noCard: 'Откройте карточку контакта, компании или сделки',
      noPhone: 'В открытой карточке нет телефона',
      networkError: 'Нет связи с ATOS-ATS',
      genericError: 'Не удалось запустить звонок'
    };

    function getSettings() {
      try {
        return (typeof self.get_settings === 'function' && self.get_settings()) || {};
      } catch (error) {
        return {};
      }
    }

    function getAtsUrl() {
      var configured = String(getSettings().atsUrl || '').trim();
      if (!configured) configured = 'https://axiomats.ru';
      return configured.replace(/\/+$/, '');
    }

    function getSystemValue(name) {
      try {
        var system = typeof self.system === 'function' ? self.system() : null;
        return system && system[name] != null ? system[name] : null;
      } catch (error) {
        return null;
      }
    }

    function getAmoUserId() {
      var value = Number(getSystemValue('user_id'));
      return isFinite(value) && value > 0 ? value : null;
    }

    function getCardContext() {
      var area = String(getSystemValue('area') || '');
      var path = String(window.location && window.location.pathname || '');
      var isCard = /lcard|ccard|comcard|card/i.test(area) ||
        /\/(leads|contacts|companies)\/detail(?:\/|$)/i.test(path);
      var entityId = Number(getSystemValue('entity_id'));
      var contactId = /ccard/i.test(area) && isFinite(entityId) && entityId > 0 ? entityId : null;
      return {
        open: isCard,
        contactId: contactId
      };
    }

    function inSettings() {
      var area = String(getSystemValue('area') || '');
      var path = String(window.location && window.location.pathname || '');
      return /settings|advanced_settings/i.test(area + ' ' + path);
    }

    function normalizePhone(value) {
      var raw = String(value || '').trim();
      if (!raw) return '';
      var plus = raw.charAt(0) === '+';
      var digits = raw.replace(/[^0-9]/g, '');
      if (digits.length < 10) return '';
      return (plus ? '+' : '') + digits;
    }

    function phoneFromElement(element) {
      if (!element) return '';
      var href = String(element.getAttribute('href') || '').replace(/^tel:/i, '');
      var value = element.getAttribute('data-value') ||
        element.getAttribute('data-phone') ||
        href ||
        element.textContent ||
        '';
      var phone = normalizePhone(value);
      if (phone) return phone;
      var child = element.querySelector && element.querySelector('a[href^="tel:"]');
      return child ? normalizePhone(child.getAttribute('href')) : '';
    }

    function findPhoneElement() {
      if (!getCardContext().open) return null;
      var selectors = [
        '[data-field-code="PHONE"] a',
        '[data-field-code="PHONE"]',
        'a[href^="tel:"]'
      ];
      for (var s = 0; s < selectors.length; s += 1) {
        var nodes = document.querySelectorAll(selectors[s]);
        for (var i = 0; i < nodes.length; i += 1) {
          if (phoneFromElement(nodes[i])) return nodes[i];
        }
      }
      var links = document.querySelectorAll('a');
      var pattern = /(?:\\+7|8|7)\\s*[\\(]?\\d{3}[\\)]?[-\\s]?\\d{3}[-\\s]?\\d{2}[-\\s]?\\d{2}/;
      for (var j = 0; j < links.length; j += 1) {
        if (pattern.test(links[j].textContent || '')) return links[j];
      }
      return null;
    }

    function getCurrentPhone() {
      var element = findPhoneElement();
      return element ? phoneFromElement(element) : '';
    }

    function getRoot() {
      return document.getElementById(rootId);
    }

    function setStatus(message, isError) {
      var root = getRoot();
      if (!root) return;
      var status = root.querySelector('.atos-ats-status');
      if (!status) return;
      status.textContent = message || '';
      status.className = 'atos-ats-status' + (isError ? ' atos-ats-status-error' : '');
    }

    function syncPhone(force) {
      var root = getRoot();
      if (!root) return '';
      var context = getCardContext();
      var phone = context.open ? getCurrentPhone() : '';
      var input = root.querySelector('.atos-ats-phone');
      var submit = root.querySelector('.atos-ats-submit');
      var key = context.open ? String(window.location.href) + '|' + phone : 'no-card';
      var changed = root.getAttribute('data-context-key') !== key;
      root.setAttribute('data-context-key', key);
      root.setAttribute('data-card-open', context.open ? 'true' : 'false');

      if (input) {
        input.value = phone;
        input.placeholder = context.open ? text.phonePlaceholder : text.noCard;
      }
      if (submit) submit.disabled = !context.open || !phone || root.getAttribute('data-call-state') === 'busy';

      if (changed && root.getAttribute('data-call-state') !== 'busy') {
        if (!context.open) setStatus(text.noCard, true);
        else if (!phone) setStatus(text.noPhone, true);
        else setStatus('', false);
      }
      return phone;
    }

    function responseError(xhr) {
      if (!xhr || xhr.status === 0) return text.networkError;
      var body = xhr.responseJSON || {};
      return body.error || text.genericError;
    }

    function startCall(button, phoneValue) {
      var root = getRoot();
      var context = getCardContext();
      var phone = normalizePhone(phoneValue);
      if (!context.open) {
        setStatus(text.noCard, true);
        if (button) button.disabled = false;
        return;
      }
      if (!phone) {
        setStatus(text.noPhone, true);
        if (button) button.disabled = false;
        return;
      }
      if (root) root.setAttribute('data-call-state', 'busy');
      if (button) {
        button.disabled = true;
        button.textContent = text.loading;
      }
      setStatus(text.loading, false);
      self.callThroughAts(phone, context.contactId)
        .done(function () {
          if (button) button.textContent = text.queued;
          setStatus(text.queued, false);
        })
        .fail(function (xhr) {
          if (root) root.setAttribute('data-call-state', 'idle');
          if (button) {
            button.disabled = false;
            button.textContent = text.callButton;
          }
          setStatus(responseError(xhr), true);
          if (window.console && console.error) {
            console.error('ATOS-ATS click-to-call failed', xhr);
          }
        });
    }

    function mountLauncher() {
      if (inSettings() || !document.body || getRoot()) return !!getRoot();
      var root = document.createElement('div');
      root.id = rootId;
      root.setAttribute('data-atos-ats-version', version);
      root.className = 'atos-ats-widget-root';
      root.innerHTML =
        '<button type="button" class="atos-ats-launcher" aria-expanded="false" title="' + text.call + '">' +
          '<span class="atos-ats-launcher-icon" aria-hidden="true">☎</span>' +
          '<span class="atos-ats-launcher-label">' + text.launcher + '</span>' +
        '</button>' +
        '<section class="atos-ats-panel" hidden aria-label="' + text.panelTitle + '">' +
          '<div class="atos-ats-panel-title">' + text.panelTitle + '</div>' +
          '<input class="atos-ats-phone" type="tel" readonly aria-readonly="true" autocomplete="off" placeholder="' + text.noCard + '">' +
          '<div class="atos-ats-panel-actions">' +
            '<button type="button" class="atos-ats-refresh">' + text.refreshButton + '</button>' +
            '<button type="button" class="atos-ats-submit" disabled>' + text.callButton + '</button>' +
          '</div>' +
          '<div class="atos-ats-status" role="status" aria-live="polite"></div>' +
        '</section>';
      document.body.appendChild(root);
      syncPhone(true);
      return true;
    }

    function mountCardButton() {
      if (inSettings() || !getCardContext().open) return false;
      var phoneElement = findPhoneElement();
      if (!phoneElement || phoneElement.getAttribute('data-atos-ats-phone-button') === '1') return false;
      if (phoneElement.parentNode && phoneElement.parentNode.querySelector('.' + cardButtonClass)) return true;
      var button = document.createElement('button');
      button.type = 'button';
      button.className = cardButtonClass;
      button.setAttribute('data-atos-ats-phone-button', '1');
      button.textContent = text.call;
      phoneElement.parentNode.insertBefore(button, phoneElement.nextSibling);
      return true;
    }

    function removeLegacyControls() {
      var old = document.querySelectorAll('.atos-ats-call-widget');
      for (var i = 0; i < old.length; i += 1) old[i].parentNode.removeChild(old[i]);
    }

    self.mountWidget = function () {
      removeLegacyControls();
      mountLauncher();
      mountCardButton();
      syncPhone(false);
      return true;
    };

    function handleClick(event) {
      var target = event.target;
      if (!target || !target.closest) return;

      var launcher = target.closest('.atos-ats-launcher');
      if (launcher) {
        event.preventDefault();
        event.stopPropagation();
        var root = getRoot();
        if (!root) return;
        var panel = root.querySelector('.atos-ats-panel');
        var open = panel && !panel.hidden;
        if (panel) panel.hidden = open;
        launcher.setAttribute('aria-expanded', String(!open));
        syncPhone(false);
        return;
      }

      var refresh = target.closest('.atos-ats-refresh');
      if (refresh) {
        event.preventDefault();
        event.stopPropagation();
        syncPhone(true);
        return;
      }

      var submit = target.closest('.atos-ats-submit');
      if (submit) {
        event.preventDefault();
        event.stopPropagation();
        var root = getRoot();
        startCall(submit, root && root.querySelector('.atos-ats-phone').value);
        return;
      }

      var cardButton = target.closest('.' + cardButtonClass);
      if (cardButton) {
        event.preventDefault();
        event.stopPropagation();
        startCall(cardButton, getCurrentPhone());
      }
    }

    function bindActions() {
      if (boundDocument === document) return;
      if (boundDocument) boundDocument.removeEventListener('click', handleClick, true);
      document.addEventListener('click', handleClick, true);
      boundDocument = document;
    }

    function startMounting() {
      self.mountWidget();
      if (!mountTimer) mountTimer = setInterval(self.mountWidget, 1000);
      if (!observer && window.MutationObserver && document.body) {
        observer = new MutationObserver(function () { self.mountWidget(); });
        observer.observe(document.body, { childList: true, subtree: true });
      }
    }

    self.callbacks = {
      init: function () {
        bindActions();
        self.mountWidget();
        setTimeout(self.mountWidget, 0);
        return true;
      },
      bind_actions: function () {
        bindActions();
        return true;
      },
      render: function () {
        startMounting();
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
        if (boundDocument) boundDocument.removeEventListener('click', handleClick, true);
        boundDocument = null;
        var root = getRoot();
        if (root && root.parentNode) root.parentNode.removeChild(root);
        var old = document.querySelectorAll('.' + cardButtonClass);
        for (var i = 0; i < old.length; i += 1) old[i].parentNode.removeChild(old[i]);
        return true;
      }
    };

    self.callThroughAts = function (phone, contactId) {
      var payload = {
        phone: String(phone || ''),
        amo_user_id: getAmoUserId(),
        crm_contact_id: contactId ? Number(contactId) : null,
        request_id: 'amo-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10)
      };
      return $.ajax({
        url: getAtsUrl() + '/api/v1/amo/widget/click-to-call',
        method: 'POST',
        contentType: 'application/json; charset=UTF-8',
        dataType: 'json',
        data: JSON.stringify(payload),
        timeout: 15000
      });
    };
  };
});
