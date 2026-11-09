/* ATOS ATS click-to-call widget for amoCRM. */
define(['jquery'], function ($) {
  'use strict';

  return function () {
    var self = this;
    var version = '3.0.0';
    var rootId = 'atos-ats-widget-root';
    var mountTimer = null;
    var observer = null;
    var boundDocument = null;
    var cardCache = {};
    var pendingCard = null;
    var text = {
      launcher: 'АТС', title: 'Позвонить через ATOS-ATS', call: 'Позвонить',
      refresh: 'Обновить данные карточки', loading: 'Загрузка карточки…',
      calling: 'Запуск звонка…', queued: 'Звонок поставлен в очередь',
      noCard: 'Откройте карточку контакта, компании или сделки',
      noPhone: 'В карточке не найден номер телефона',
      amoReadError: 'Не удалось прочитать данные карточки amoCRM',
      network: 'Нет связи с ATOS-ATS', generic: 'Не удалось запустить звонок'
    };

    function settings() {
      try { return (typeof self.get_settings === 'function' && self.get_settings()) || {}; }
      catch (e) { return {}; }
    }
    function atsUrl() {
      return String(settings().atsUrl || 'https://axiomats.ru').trim().replace(/\/+$/, '');
    }
    function route() {
      return String(settings().route || '').trim().toLowerCase() === 'multicom_ami' ? 'multicom_ami' : 'megafon_vats';
    }
    function system() {
      try { return (typeof self.system === 'function' && self.system()) || {}; }
      catch (e) { return {}; }
    }
    function systemValue(name) {
      var value = system();
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
      var values = [self.entity_id, self.card_id, systemValue('entity_id'), systemValue('entityId'),
        systemValue('card_id'), systemValue('cardId'), systemValue('lead_id'), systemValue('contact_id'), systemValue('company_id')];
      for (var i = 0; i < values.length; i += 1) {
        var id = positiveId(values[i]);
        if (id) return id;
      }
      return null;
    }
    function cardContext() {
      var area = locationArea();
      var id = entityId();
      var routePath = String(((window.location && window.location.pathname) || '') + ' ' + ((window.location && window.location.hash) || '')).toLowerCase();
      if (!area) {
        if (/leads/.test(routePath)) area = 'leads';
        else if (/contacts/.test(routePath)) area = 'contacts';
        else if (/companies/.test(routePath)) area = 'companies';
      }
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
          var phone = normalizePhone(field.values[j] && (field.values[j].value || field.values[j]));
          if (phone) return phone;
        }
      }
      return '';
    }
    function phoneFromElement(element) {
      if (!element) return '';
      var href = String(element.getAttribute('href') || '').replace(/^tel:/i, '');
      return normalizePhone(element.getAttribute('data-value') || element.getAttribute('data-phone') || href || element.textContent);
    }
    function phoneFromDom() {
      var selectors = ['a[href^="tel:"]', '[data-field-code="PHONE"]', '[data-code="PHONE"]'];
      for (var i = 0; i < selectors.length; i += 1) {
        var nodes = document.querySelectorAll(selectors[i]);
        for (var j = 0; j < nodes.length; j += 1) {
          var phone = phoneFromElement(nodes[j]);
          if (phone) return phone;
        }
      }
      var links = document.querySelectorAll('a');
      var pattern = /(?:\+7|8|7)\s*[\(]?\d{3}[\)]?[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}/;
      for (var k = 0; k < links.length; k += 1) {
        if (pattern.test(links[k].textContent || '')) return normalizePhone(links[k].textContent);
      }
      return '';
    }

    /* amoCRM exposes crm_get in newer widget shells. The jQuery fallback is
     * same-origin and works in older private integrations hosted by amoCRM. */
    function amoGet(path, done, fail) {
      var complete = false;
      var timer = setTimeout(function () { error({ code: 'amo_get_timeout' }); }, 12000);
      function success(data) { if (!complete) { complete = true; clearTimeout(timer); done(data || {}); } }
      function error(xhr) { if (!complete) { complete = true; clearTimeout(timer); fail(xhr || {}); } }
      try {
        if (typeof self.crm_get === 'function') {
          var result = self.crm_get(path, {}, success, error);
          if (result && typeof result.done === 'function') result.done(success).fail(error);
          return;
        }
      } catch (e) { error(e); return; }
      $.ajax({ url: path, method: 'GET', dataType: 'json', timeout: 10000 }).done(success).fail(error);
    }
    function responsibleFromPayload(data) {
      return positiveId(data && (data.responsible_user_id || data.responsible_id));
    }
    function buildCard(area, id, data) {
      var payload = data || {};
      var card = {
        area: area, entity_id: id,
        crm_contact_id: area === 'contacts' ? (positiveId(payload.id) || id) : null,
        phone: phoneFromFields(payload.custom_fields_values) || normalizePhone(payload.phone),
        responsible_user_id: responsibleFromPayload(payload), name: String(payload.name || '')
      };
      var linked = payload._embedded && payload._embedded.contacts;
      if (!card.crm_contact_id && linked && linked.length) card.crm_contact_id = positiveId(linked[0].id || linked[0].contact_id);
      return card;
    }
    function finishPending(card, error) {
      var pending = pendingCard;
      pendingCard = null;
      if (!pending) return;
      var i;
      if (card) for (i = 0; i < pending.done.length; i += 1) pending.done[i](card);
      else for (i = 0; i < pending.fail.length; i += 1) pending.fail[i](error || {});
    }
    function atsCard(context, done, fail) {
      $.ajax({
        url: atsUrl() + '/api/v1/amo/widget/card', method: 'POST', dataType: 'json', timeout: 15000,
        contentType: 'application/json; charset=UTF-8',
        data: JSON.stringify({ entity_type: context.area, entity_id: Number(context.id) })
      }).done(done).fail(fail);
    }
    function fetchCard(done, fail) {
      var context = cardContext();
      if (!context.open) { fail({ code: 'no_card' }); return; }
      var key = context.area + ':' + context.id;
      if (cardCache[key]) { done(cardCache[key]); return; }
      if (pendingCard) { pendingCard.done.push(done); pendingCard.fail.push(fail); return; }
      pendingCard = { done: [done], fail: [fail] };
      amoGet('/api/v4/' + context.area + '/' + context.id + '?with=contacts', function (data) {
        var card = buildCard(context.area, context.id, data);
        var contactId = card.crm_contact_id;
        if (context.area !== 'contacts' && contactId && !card.phone) {
          amoGet('/api/v4/contacts/' + contactId, function (contact) {
            card.phone = phoneFromFields(contact.custom_fields_values) || normalizePhone(contact.phone);
            card.crm_contact_id = positiveId(contact.id) || contactId;
            cardCache[key] = card; finishPending(card);
          }, function () {
            card.phone = phoneFromDom(); cardCache[key] = card; finishPending(card);
          });
        } else {
          card.phone = card.phone || phoneFromDom(); cardCache[key] = card; finishPending(card);
        }
      }, function () {
        /* Older amoCRM shells do not expose crm_get. Ask ATS to use its
         * server-side amo access token before falling back to visible DOM. */
        atsCard(context, function (data) {
          var resolved = { area: context.area, entity_id: context.id,
            crm_contact_id: positiveId(data.crm_contact_id), phone: normalizePhone(data.phone),
            responsible_user_id: positiveId(data.responsible_user_id), name: String(data.name || '') };
          if (resolved.phone && resolved.crm_contact_id) { cardCache[key] = resolved; finishPending(resolved); }
          else finishPending(null, { code: 'amo_read_failed' });
        }, function () {
          var fallback = { area: context.area, entity_id: context.id,
            crm_contact_id: context.area === 'contacts' ? context.id : null, phone: phoneFromDom(),
            responsible_user_id: null, name: '' };
          if (fallback.phone) { cardCache[key] = fallback; finishPending(fallback); }
          else finishPending(null, { code: 'amo_read_failed' });
        });
      });
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
      var input = element.querySelector('.atos-ats-phone');
      var responsible = element.querySelector('.atos-ats-responsible');
      var submit = element.querySelector('.atos-ats-submit');
      if (input) input.value = (card && card.phone) || '';
      if (responsible) responsible.textContent = card && card.responsible_user_id ? 'Ответственный: #' + card.responsible_user_id : 'Ответственный: текущий пользователь';
      var ready = !!(context.open && card && card.phone && card.crm_contact_id);
      if (submit) submit.disabled = !ready || element.getAttribute('data-call-state') === 'busy';
      element.setAttribute('data-card-ready', ready ? 'true' : 'false');
      if (!context.open) setStatus(text.noCard, true);
      else if (!card || !card.phone) setStatus(text.noPhone, true);
      else if (!card.crm_contact_id) setStatus('Контакт amoCRM не связан с карточкой', true);
      else setStatus('', false);
    }
    function syncCard(force) {
      var element = root();
      if (!element) return;
      var context = cardContext();
      var key = context.open ? context.area + ':' + context.id : 'no-card';
      if (!force && element.getAttribute('data-context-key') === key && element.getAttribute('data-card-ready') === 'true') return;
      element.setAttribute('data-context-key', key);
      if (!context.open) { updatePanel(null); return; }
      setStatus(text.loading, false);
      fetchCard(function (card) { updatePanel(card); }, function () { updatePanel(null); setStatus(text.amoReadError, true); });
    }
    function responseError(xhr) {
      if (!xhr || xhr.status === 0) return text.network;
      var body = xhr.responseJSON || {};
      var code = String(body.error || body.detail || '');
      var messages = {
        crm_contact_not_synced: 'Контакт ещё не синхронизирован с ATS',
        amo_user_operator_mapping_missing: 'Для ответственного не настроено соответствие оператору ATS',
        operator_unavailable: 'Оператор сейчас недоступен', operator_selection_required: 'Не удалось выбрать оператора',
        live_calls_disabled_for_tenant: 'Живые звонки отключены для этого контура',
        megafon_not_configured: 'МегаФон ВАТС не настроен', ami_not_configured: 'Multicom/AMI не настроен',
        contact_phone_mismatch: 'Номер карточки не совпадает с номером в ATS'
      };
      return messages[code] || code || text.generic;
    }
    function call(card, button) {
      var element = root();
      if (!card || !card.phone || !card.crm_contact_id) { setStatus(text.noPhone, true); return; }
      var amoUserId = card.responsible_user_id || currentUserId();
      element.setAttribute('data-call-state', 'busy');
      if (button) { button.disabled = true; button.textContent = text.calling; }
      setStatus(text.calling, false);
      $.ajax({
        url: atsUrl() + '/api/v1/amo/widget/click-to-call', method: 'POST', dataType: 'json', timeout: 20000,
        contentType: 'application/json; charset=UTF-8',
        data: JSON.stringify({ phone: card.phone, amo_user_id: amoUserId || null,
          crm_contact_id: Number(card.crm_contact_id), route: route(),
          request_id: 'amo-' + Date.now() + '-' + Math.random().toString(36).slice(2, 12) })
      }).done(function () { if (button) button.textContent = text.queued; setStatus(text.queued, false); })
        .fail(function (xhr) {
          element.setAttribute('data-call-state', 'idle');
          if (button) { button.disabled = false; button.textContent = text.call; }
          setStatus(responseError(xhr), true);
          if (window.console && console.error) console.error('ATOS ATS click-to-call failed', xhr);
        });
    }
    function mount() {
      var element = root();
      if (element || !document.body) return !!element;
      var area = String(systemValue('area') || '').toLowerCase();
      if (/settings/.test(area)) return false;
      element = document.createElement('div'); element.id = rootId; element.className = 'atos-ats-widget-root';
      element.setAttribute('data-atos-ats-version', version);
      element.innerHTML = '<button type="button" class="atos-ats-launcher" aria-expanded="false" title="' + text.title + '"><span class="atos-ats-launcher-icon" aria-hidden="true">☎</span><span>' + text.launcher + '</span></button>' +
        '<section class="atos-ats-panel" hidden aria-label="' + text.title + '"><div class="atos-ats-panel-title">' + text.title + '</div><div class="atos-ats-card-caption">Данные открытой карточки</div><input class="atos-ats-phone" type="tel" readonly aria-readonly="true" autocomplete="off"><div class="atos-ats-responsible">Ответственный: текущий пользователь</div><div class="atos-ats-panel-actions"><button type="button" class="atos-ats-refresh">' + text.refresh + '</button><button type="button" class="atos-ats-submit" disabled>' + text.call + '</button></div><div class="atos-ats-status" role="status" aria-live="polite"></div></section>';
      document.body.appendChild(element); syncCard(true); return true;
    }
    function onClick(event) {
      var target = event.target; if (!target || !target.closest) return;
      var element = root();
      var launcher = target.closest('.atos-ats-launcher');
      if (launcher && element) {
        event.preventDefault(); event.stopPropagation(); var panel = element.querySelector('.atos-ats-panel');
        var open = panel && !panel.hidden; if (panel) panel.hidden = open; launcher.setAttribute('aria-expanded', String(!open)); syncCard(false); return;
      }
      var refresh = target.closest('.atos-ats-refresh');
      if (refresh) { event.preventDefault(); event.stopPropagation(); cardCache = {}; syncCard(true); return; }
      var submit = target.closest('.atos-ats-submit');
      if (submit && element) { event.preventDefault(); event.stopPropagation(); var context = cardContext(); call(cardCache[context.area + ':' + context.id], submit); }
    }
    function bind() {
      if (boundDocument === document) return;
      if (boundDocument) boundDocument.removeEventListener('click', onClick, true);
      document.addEventListener('click', onClick, true); boundDocument = document;
    }
    function startMounting() {
      mount();
      if (!mountTimer) mountTimer = setInterval(function () { if (!root()) mount(); syncCard(false); }, 1200);
      if (!observer && window.MutationObserver && document.body) { observer = new MutationObserver(function () { if (!root()) mount(); }); observer.observe(document.body, { childList: true, subtree: true }); }
    }
    self.callbacks = {
      init: function () { bind(); mount(); return true; }, bind_actions: function () { bind(); return true; },
      render: function () { startMounting(); return true; }, settings: function () { return true; }, onSave: function () { return true; },
      destroy: function () {
        if (mountTimer) clearInterval(mountTimer); mountTimer = null;
        if (observer) observer.disconnect(); observer = null;
        if (boundDocument) boundDocument.removeEventListener('click', onClick, true); boundDocument = null;
        var element = root(); if (element && element.parentNode) element.parentNode.removeChild(element);
        cardCache = {}; pendingCard = null; return true;
      }
    };
  };
});
