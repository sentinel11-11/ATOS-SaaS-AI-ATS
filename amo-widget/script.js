/* ATOS ATS card click-to-call widget for amoCRM. */
define(['jquery'], function ($) {
  'use strict';

  return function () {
    var self = this;
    var version = '4.0.0';
    var rootId = 'atos-ats-widget-root';
    var timer = null;
    var boundDocument = null;
    var cardCache = {};
    var pendingCard = null;

    var text = {
      brand: 'ATOS-ATS',
      title: 'Звонок через ATS',
      phone: 'Телефон',
      responsible: 'Ответственный',
      currentUser: 'текущий пользователь',
      call: 'Позвонить через ATS',
      loading: 'Получаем данные карточки…',
      calling: 'Запускаем звонок…',
      queued: 'Звонок поставлен в очередь',
      refresh: 'Обновить карточку',
      noCard: 'Виджет доступен внутри карточки',
      noPhone: 'Телефон в карточке не найден',
      readError: 'Не удалось прочитать карточку amoCRM',
      network: 'Нет связи с ATOS-ATS',
      generic: 'Не удалось запустить звонок'
    };

    function getSettings() {
      try {
        return (typeof self.get_settings === 'function' && self.get_settings()) || {};
      } catch (error) {
        return {};
      }
    }

    function atsUrl() {
      return String(getSettings().atsUrl || 'https://axiomats.ru').trim().replace(/\/+$/, '');
    }

    function route() {
      return String(getSettings().route || '').trim().toLowerCase() === 'multicom_ami'
        ? 'multicom_ami'
        : 'megafon_vats';
    }

    function getSystem() {
      try {
        return (typeof self.system === 'function' && self.system()) || {};
      } catch (error) {
        return {};
      }
    }

    function systemValue(name) {
      var value = getSystem();
      return value && value[name] != null ? value[name] : null;
    }

    function positiveId(value) {
      var number = Number(value);
      return isFinite(number) && number > 0 ? number : null;
    }

    function currentUserId() {
      return positiveId(systemValue('amouser_id')) || positiveId(systemValue('amo_user_id')) ||
        positiveId(systemValue('user_id')) || positiveId(systemValue('userId')) ||
        positiveId(self.user_id) || positiveId(self.current_user_id);
    }

    function locationArea() {
      var area = String(systemValue('area') || '').toLowerCase();
      if (/^lcard/.test(area) || /lead/.test(area)) return 'leads';
      if (/^ccard/.test(area) || /contact/.test(area)) return 'contacts';
      if (/^comcard/.test(area) || /compan/.test(area)) return 'companies';
      return '';
    }

    function entityId() {
      var params = self.params || {};
      var values = [
        self.entity_id, self.card_id, self.id,
        systemValue('entity_id'), systemValue('entityId'), systemValue('card_id'), systemValue('cardId'),
        systemValue('lead_id'), systemValue('contact_id'), systemValue('company_id'),
        params.entity_id, params.entityId, params.card_id, params.id
      ];
      for (var i = 0; i < values.length; i += 1) {
        var id = positiveId(values[i]);
        if (id) return id;
      }
      return null;
    }

    function cardContext() {
      var area = locationArea();
      var id = entityId();
      return { area: area, id: id, open: !!area && !!id };
    }

    function normalizePhone(value) {
      var raw = String(value == null ? '' : value).trim();
      if (!raw) return '';
      var digits = raw.replace(/[^0-9]/g, '');
      if (digits.length < 10) return '';
      if (digits.length === 11 && digits.charAt(0) === '8') digits = '7' + digits.slice(1);
      if (digits.length === 10 && digits.charAt(0) === '9') digits = '7' + digits;
      return '+' + digits;
    }

    function phoneFromFields(fields) {
      var values = fields || [];
      for (var i = 0; i < values.length; i += 1) {
        var field = values[i] || {};
        var code = String(field.field_code || field.code || '').toUpperCase();
        var name = String(field.field_name || field.name || '').toLowerCase();
        if (code !== 'PHONE' && name.indexOf('телефон') < 0 && name.indexOf('phone') < 0) continue;
        for (var j = 0; j < (field.values || []).length; j += 1) {
          var item = field.values[j];
          var phone = normalizePhone(item && (item.value || item));
          if (phone) return phone;
        }
      }
      return normalizePhone('');
    }

    function phoneFromDom() {
      var nodes = document.querySelectorAll('a[href^="tel:"], [data-field-code="PHONE"], [data-code="PHONE"]');
      for (var i = 0; i < nodes.length; i += 1) {
        var href = String(nodes[i].getAttribute('href') || '').replace(/^tel:/i, '');
        var value = nodes[i].getAttribute('data-value') || nodes[i].getAttribute('data-phone') || href || nodes[i].textContent;
        var phone = normalizePhone(value);
        if (phone) return phone;
      }
      return '';
    }

    function amoGet(path, done, fail) {
      var complete = false;
      var timerId = setTimeout(function () { finishError({ code: 'amo_get_timeout' }); }, 12000);
      function finish(data) {
        if (complete) return;
        complete = true;
        clearTimeout(timerId);
        done(data || {});
      }
      function finishError(error) {
        if (complete) return;
        complete = true;
        clearTimeout(timerId);
        fail(error || {});
      }
      try {
        if (typeof self.crm_get === 'function') {
          var result = self.crm_get(path, {}, finish, finishError);
          if (result && typeof result.done === 'function') result.done(finish).fail(finishError);
          return;
        }
      } catch (error) {
        finishError(error);
        return;
      }
      $.ajax({ url: path, method: 'GET', dataType: 'json', timeout: 10000 }).done(finish).fail(finishError);
    }

    function cardFromPayload(area, id, data) {
      var payload = data || {};
      var linked = payload._embedded && payload._embedded.contacts;
      var contactId = area === 'contacts' ? (positiveId(payload.id) || id) : null;
      if (!contactId && linked && linked.length) contactId = positiveId(linked[0].id || linked[0].contact_id);
      return {
        area: area,
        entity_id: id,
        crm_contact_id: contactId,
        phone: phoneFromFields(payload.custom_fields_values) || normalizePhone(payload.phone),
        responsible_user_id: positiveId(payload.responsible_user_id || payload.responsible_id),
        name: String(payload.name || '')
      };
    }

    function finishPending(card, error) {
      var pending = pendingCard;
      pendingCard = null;
      if (!pending) return;
      var i;
      if (card) {
        for (i = 0; i < pending.done.length; i += 1) pending.done[i](card);
      } else {
        for (i = 0; i < pending.fail.length; i += 1) pending.fail[i](error || {});
      }
    }

    function resolveViaAts(context, done, fail) {
      $.ajax({
        url: atsUrl() + '/api/v1/amo/widget/card',
        method: 'POST',
        dataType: 'json',
        timeout: 15000,
        contentType: 'application/json; charset=UTF-8',
        data: JSON.stringify({ entity_type: context.area, entity_id: Number(context.id) })
      }).done(done).fail(fail);
    }

    function fetchCard(done, fail) {
      var context = cardContext();
      if (!context.open) {
        fail({ code: 'no_card' });
        return;
      }
      var key = context.area + ':' + context.id;
      if (cardCache[key]) {
        done(cardCache[key]);
        return;
      }
      if (pendingCard) {
        pendingCard.done.push(done);
        pendingCard.fail.push(fail);
        return;
      }
      pendingCard = { done: [done], fail: [fail] };
      function useFallback() {
        resolveViaAts(context, function (data) {
          var resolved = {
            area: context.area,
            entity_id: context.id,
            crm_contact_id: positiveId(data.crm_contact_id),
            phone: normalizePhone(data.phone),
            responsible_user_id: positiveId(data.responsible_user_id),
            name: String(data.name || '')
          };
          if (resolved.crm_contact_id && resolved.phone) {
            cardCache[key] = resolved;
            finishPending(resolved);
          } else {
            finishPending(null, { code: 'amo_card_data_missing' });
          }
        }, function () {
          var fallback = {
            area: context.area,
            entity_id: context.id,
            crm_contact_id: context.area === 'contacts' ? context.id : null,
            phone: phoneFromDom(),
            responsible_user_id: null,
            name: ''
          };
          if (fallback.crm_contact_id && fallback.phone) {
            cardCache[key] = fallback;
            finishPending(fallback);
          } else {
            finishPending(null, { code: 'amo_read_failed' });
          }
        });
      }
      amoGet('/api/v4/' + context.area + '/' + context.id + '?with=contacts', function (data) {
        var card = cardFromPayload(context.area, context.id, data);
        if (context.area !== 'contacts' && card.crm_contact_id && !card.phone) {
          amoGet('/api/v4/contacts/' + card.crm_contact_id, function (contact) {
            card.phone = phoneFromFields(contact.custom_fields_values) || normalizePhone(contact.phone);
            card.crm_contact_id = positiveId(contact.id) || card.crm_contact_id;
            if (card.phone) {
              cardCache[key] = card;
              finishPending(card);
            } else {
              useFallback();
            }
          }, useFallback);
        } else if (card.crm_contact_id && card.phone) {
          cardCache[key] = card;
          finishPending(card);
        } else {
          useFallback();
        }
      }, useFallback);
    }

    function root() { return document.getElementById(rootId); }

    function setStatus(message, isError) {
      var element = root();
      var status = element && element.querySelector('.atos-ats-status');
      if (!status) return;
      status.textContent = message || '';
      status.className = 'atos-ats-status' + (isError ? ' atos-ats-status-error' : '');
    }

    function updatePanel(card) {
      var element = root();
      if (!element) return;
      var context = cardContext();
      var phone = element.querySelector('.atos-ats-phone-value');
      var responsible = element.querySelector('.atos-ats-responsible-value');
      var submit = element.querySelector('.atos-ats-submit');
      if (phone) phone.textContent = card && card.phone ? card.phone : '—';
      if (responsible) responsible.textContent = card && card.responsible_user_id
        ? '#' + card.responsible_user_id
        : text.currentUser;
      var ready = !!(context.open && card && card.phone && card.crm_contact_id);
      element.setAttribute('data-card-ready', ready ? 'true' : 'false');
      if (submit) submit.disabled = !ready || element.getAttribute('data-call-state') === 'busy';
      if (!context.open) setStatus(text.noCard, true);
      else if (!card || !card.phone) setStatus(text.noPhone, true);
      else if (!card.crm_contact_id) setStatus('Контакт не синхронизирован с ATS', true);
      else setStatus('', false);
    }

    function syncCard(force) {
      var element = root();
      if (!element) return;
      var context = cardContext();
      var key = context.open ? context.area + ':' + context.id : 'no-card';
      if (!force && element.getAttribute('data-context-key') === key && element.getAttribute('data-card-ready') === 'true') return;
      element.setAttribute('data-context-key', key);
      if (!context.open) {
        updatePanel(null);
        return;
      }
      setStatus(text.loading, false);
      fetchCard(function (card) { updatePanel(card); }, function () {
        updatePanel(null);
        setStatus(text.readError, true);
      });
    }

    function responseError(xhr) {
      if (!xhr || xhr.status === 0) return text.network;
      var body = xhr.responseJSON || {};
      var code = String(body.error || body.detail || '');
      var messages = {
        crm_contact_not_synced: 'Контакт ещё не синхронизирован с ATS',
        amo_user_operator_mapping_missing: 'Для ответственного не настроено соответствие оператору ATS',
        operator_unavailable: 'Оператор сейчас недоступен',
        operator_selection_required: 'Не удалось выбрать оператора',
        live_calls_disabled_for_tenant: 'Живые звонки отключены для этого контура',
        megafon_not_configured: 'МегаФон ВАТС не настроен',
        ami_not_configured: 'Multicom/AMI не настроен',
        contact_phone_mismatch: 'Номер карточки не совпадает с номером в ATS'
      };
      return messages[code] || code || text.generic;
    }

    function call(card, button) {
      var element = root();
      if (!card || !card.phone || !card.crm_contact_id) {
        setStatus(text.noPhone, true);
        return;
      }
      var amoUserId = card.responsible_user_id || currentUserId();
      element.setAttribute('data-call-state', 'busy');
      if (button) {
        button.disabled = true;
        button.textContent = text.calling;
      }
      setStatus(text.calling, false);
      $.ajax({
        url: atsUrl() + '/api/v1/amo/widget/click-to-call',
        method: 'POST',
        dataType: 'json',
        timeout: 20000,
        contentType: 'application/json; charset=UTF-8',
        data: JSON.stringify({
          phone: card.phone,
          amo_user_id: amoUserId || null,
          crm_contact_id: Number(card.crm_contact_id),
          route: route(),
          request_id: 'amo-' + Date.now() + '-' + Math.random().toString(36).slice(2, 12)
        })
      }).done(function () {
        if (button) button.textContent = text.queued;
        setStatus(text.queued, false);
      }).fail(function (xhr) {
        element.setAttribute('data-call-state', 'idle');
        if (button) {
          button.disabled = false;
          button.textContent = text.call;
        }
        setStatus(responseError(xhr), true);
        if (window.console && console.error) console.error('ATOS ATS click-to-call failed', xhr);
      });
    }

    function mount() {
      var element = root();
      if (element || !document.body) return !!element;
      var area = String(systemValue('area') || '').toLowerCase();
      if (/settings/.test(area)) return false;
      element = document.createElement('div');
      element.id = rootId;
      element.className = 'atos-ats-widget-root';
      element.setAttribute('data-atos-ats-version', version);
      element.innerHTML =
        '<div class="atos-ats-card-header"><div><div class="atos-ats-brand">' + text.brand + '</div><div class="atos-ats-title">' + text.title + '</div></div><span class="atos-ats-route">' + route() + '</span></div>' +
        '<div class="atos-ats-card-body"><div class="atos-ats-field"><span>' + text.phone + '</span><strong class="atos-ats-phone-value">—</strong></div><div class="atos-ats-field"><span>' + text.responsible + '</span><strong class="atos-ats-responsible-value">' + text.currentUser + '</strong></div><div class="atos-ats-actions"><button type="button" class="atos-ats-submit" disabled>' + text.call + '</button><button type="button" class="atos-ats-refresh" title="' + text.refresh + '" aria-label="' + text.refresh + '">↻</button></div><div class="atos-ats-status" role="status" aria-live="polite"></div></div>';
      document.body.appendChild(element);
      syncCard(true);
      return true;
    }

    function onClick(event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var element = root();
      var refresh = target.closest('.atos-ats-refresh');
      if (refresh) {
        event.preventDefault();
        event.stopPropagation();
        cardCache = {};
        syncCard(true);
        return;
      }
      var submit = target.closest('.atos-ats-submit');
      if (submit && element) {
        event.preventDefault();
        event.stopPropagation();
        var context = cardContext();
        call(cardCache[context.area + ':' + context.id], submit);
      }
    }

    function bind() {
      if (boundDocument === document) return;
      if (boundDocument) boundDocument.removeEventListener('click', onClick, true);
      document.addEventListener('click', onClick, true);
      boundDocument = document;
    }

    function start() {
      mount();
      if (!timer) timer = setInterval(function () {
        if (!root()) mount();
        syncCard(false);
      }, 1500);
    }

    self.callbacks = {
      init: function () { bind(); mount(); return true; },
      bind_actions: function () { bind(); return true; },
      render: function () { start(); return true; },
      settings: function () { return true; },
      onSave: function () { return true; },
      destroy: function () {
        if (timer) clearInterval(timer);
        timer = null;
        if (boundDocument) boundDocument.removeEventListener('click', onClick, true);
        boundDocument = null;
        var element = root();
        if (element && element.parentNode) element.parentNode.removeChild(element);
        cardCache = {};
        pendingCard = null;
        return true;
      }
    };
  };
});
