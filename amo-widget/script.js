define(['jquery'], function ($) {
  'use strict';

  return function RATSWidget() {
    if(window.RATSPhoneWidget)return window.RATSPhoneWidget;
    var self = this;
    window.RATSPhoneWidget=self;
    var VERSION = '5.4.0';
    var ROOT = 'rats-widget-root';
    var alive = false;
    var wired = false;
    var phoneActionRegistered = false;
    var timer = null;
    var refreshTimer = null;
    var lastCardKey = '';
    var cardCache = {};
    var pendingCard = false;
    var principal = null;
    var mode = 'browser';
    var manager = null;
    var managerKey = '';
    var managerPending = false;
    var managerSynced = 0;
    var agentId = null;
    var agentLease = false;
    var connectingPromise = null;
    var connectionEpoch = 0;
    var heartbeatInFlight = false;
    var agentPolling = false;
    var agentTickCount = 0;
    var agentStatus = '';
    var receiveCall = null;
    var busy = false;
    var callId = null;
    var hangupRequested = false;
    var call = null;
    var callState = 'idle';
    var pollInFlight = false;
    var sipUA = null;
    var sipRegistered = false;
    var sipSession = null;
    var sipIncoming = false;
    var webrtc = null;
    var loadedJsSIP = false;
    var remoteAudio = null;
    var errorMessage = '';
    var statusMessage = 'Выберите режим и нажмите «Позвонить клиенту»';
    var localStream = null;
    var mediaReadySent = false;
    var mediaConnection = null;
    var requestId = null;
    var requestBody = null;
    var requestSelf = false;
    var requestDispatched = false;
    var requestAmbiguous = false;
    var boundConnections = new WeakSet();

    var ERROR_MAP = {
      responsible_mapping_missing: 'Ответственный из карточки ещё не связан с оператором ATOS',
      responsible_user_missing: 'Назначьте ответственного в карточке amoCRM',
      responsible_changed: 'Ответственный изменился. Обновите карточку и повторите звонок',
      responsible_browser_offline: 'Ответственный должен открыть amoCRM и подключить браузерный телефон',
      widget_upgrade_required: 'Обновите виджет и перезагрузите страницы amoCRM',
      browser_devices_exhausted: 'Все 8 браузерных подключений этого аккаунта заняты. Закройте неиспользуемые сессии или обратитесь к администратору',
      agent_session_lost: 'Подключение браузерного телефона обновляется',
      browser_call_mismatch: 'Вызов не относится к этому браузеру',
      invalid_rats_url: 'Укажите публичный HTTPS-адрес ATOS в настройках виджета',
      amocrm_signed_request_unavailable: 'SDK amoCRM не предоставил подписанный запрос. Обновите страницу и интеграцию',
      amo_widget_origin_rejected: 'Аккаунт amoCRM отличается от настроенного в ATOS',
      crm_tenant_not_bound: 'Свяжите amoCRM с рабочей организацией ATOS на сервере',
      callback_disabled: 'На сервере ещё не включён сценарий соединения менеджера с клиентом',
      contact_phone_mismatch: 'Телефон контакта изменился. Обновите карточку и синхронизацию ATOS',
      contact_not_in_card: 'Выбранный контакт не связан с открытой карточкой',
      crm_read_failed: 'Не удалось проверить контакт в amoCRM. Проверьте токен API в ATOS',
      operator_destination_not_allowed: 'Телефон менеджера не добавлен в тестовые направления VPS',
      callback_parties_must_differ: 'Телефоны менеджера и клиента должны отличаться',
      browser_turn_not_configured: 'На сервере не завершена настройка TURN для браузерной связи',
      call_request_expired: 'Истекло ожидание запуска. Обновите состояние звонка',
      widget_auth_not_configured: 'В ATOS заполните ID аккаунта, ID интеграции и секретный ключ amoCRM',
      amo_widget_token_invalid: 'Подпись amoCRM не прошла проверку. Проверьте ID и секрет этой интеграции в ATOS',
      authentication_required: 'amoCRM не передала X-Auth-Token. Обновите интеграцию и повторите вход',
      widget_queue_timeout: 'Истекло ожидание свободной линии',
      reconciliation_required: 'Состояние звонка сверяется с Asterisk. Повторный вызов пока недоступен',
      connection_failed: 'Сервер ATOS недоступен или запрос блокируется CORS. Проверьте адрес сервера и его обновление',
      unauthorized: 'Не удалось проверить пользователя amoCRM — обратитесь к администратору',
      unauthenticated: 'Интеграция amoCRM и RATS не настроена администратором',
      operator_mapping_missing: 'Сотрудник amoCRM не сопоставлен с оператором RATS',
      operator_unavailable: 'Оператор недоступен или уже занят',
      operator_selection_required: 'Неоднозначная привязка оператора',
      caller_pool_exhausted: 'Нет доступных подтверждённых А-номеров',
      caller_number_unavailable: 'Выбранный А-номер временно недоступен',
      channel_limit: 'Достигнут лимит каналов транка',
      no_consent: 'Контакт не разрешён для исходящих звонков',
      contact_blacklisted: 'Номер находится в чёрном списке ATOS. Согласие amoCRM не снимает запрет чёрного списка',
      destination_not_allowed: 'Номер клиента недоступен для звонка',
      browser_endpoint_not_ready: 'WebRTC сотрудника ещё не настроен',
      browser_not_registered: 'Браузерный телефон ответственного не зарегистрирован на Asterisk. Откройте его amoCRM и дождитесь подключения телефона',
      browser_registration_check_failed: 'Не удалось проверить регистрацию браузерного телефона. Обратитесь к администратору ATOS',
      forward_number_not_set: 'Не привязан телефон сотрудника для переадресации',
      widget_backend_not_ready: 'Сервер RATS ещё не поддерживает новый протокол виджета',
      media_not_configured: 'Разговорный мост Asterisk ещё не настроен',
      ami_not_configured: 'Не настроена связь RATS → Asterisk AMI',
      crm_contact_not_synced: 'Контакт не найден в RATS',
      telephony_route_mismatch: 'Основной маршрут должен быть Мультиком / Asterisk',
      forbidden: 'Операция запрещена для этого оператора',
      busy: 'Линия занята',
      no_answer: 'Абонент не ответил'
    };

    function settings() { try { return self.get_settings() || {}; } catch (e) { return {}; } }
    function system() { try { return self.system() || {}; } catch (e) { return {}; } }
    function positiveId(x) { var n = Number(x); return isFinite(n) && n > 0 && Math.floor(n) === n ? n : null; }
    function amoUser() {
      var s = system();
      var app = typeof APP !== 'undefined' ? APP : null;
      return positiveId(s.amouser_id || s.amo_user_id || s.user_id) ||
        positiveId(app && typeof app.constant === 'function' ? app.constant('user_id') : null);
    }
    function baseUrl() {
      var raw = String(settings().ratsUrl || settings().atsUrl || '').trim().replace(/\/+$/, '');
      try { var u = new URL(raw); if (u.protocol === 'https:' && !u.username && !u.password && !u.search && !u.hash) return u.origin + u.pathname.replace(/\/+$/, ''); }
      catch (e) {}
      return '';
    }
    function normalizePhone(value) {
      var digits = String(value || '').replace(/\D/g, '');
      if (digits.length === 10 && digits.charAt(0) === '9') digits = '7' + digits;
      if (digits.length === 11 && digits.charAt(0) === '8') digits = '7' + digits.slice(1);
      return digits.length >= 11 && digits.length <= 15 ? '+' + digits : '';
    }
    function areaType(value) {
      var v = String(value || '').replace(/-\d+$/, '').toLowerCase();
      if (/^(lcard|leads|lead)$/.test(v)) return 'leads';
      if (/^(ccard|contacts|contact)$/.test(v)) return 'contacts';
      if (/^(comcard|companies|company)$/.test(v)) return 'companies';
      return '';
    }
    function context() {
      var s = system(); var app = typeof APP !== 'undefined' ? APP : null;
      var type = areaType(s.area), id = null;
      var match = String(window.location.pathname).match(/\/(leads|contacts|companies)\/detail\/(\d+)/i);
      if (match) { type = match[1].toLowerCase(); id = positiveId(match[2]); }
      if (!id && app && app.data && app.data.current_card) {
        var card = app.data.current_card;
        id = positiveId(card.id);
        if (!id && card.model && typeof card.model.get === 'function') id = positiveId(card.model.get('id'));
      }
      if (!id) id = positiveId(s.entity_id || s.card_id || s.lead_id || s.contact_id || s.company_id);
      return {type: type, id: id, key: type && id ? type + ':' + id : ''};
    }
    function phoneFields(record) {
      var out = [], fields = record && record.custom_fields_values || [];
      fields.forEach(function (f) {
        if (String(f.field_code || f.code || '').toUpperCase() !== 'PHONE') return;
        (f.values || []).forEach(function (v) {
          var p = normalizePhone(v && v.value);
          if (p && out.indexOf(p) < 0) out.push(p);
        });
      });
      return out;
    }
    function amoGet(url) {
      return new Promise(function (resolve, reject) {
        $.ajax({url: url, type: 'GET', dataType: 'json', timeout: 12000})
          .done(resolve).fail(function () { reject(new Error('amo_read_failed')); });
      });
    }
    function resolveCard(ctx, chosen) {
      var url = '/api/v4/' + ctx.type + '/' + ctx.id + (ctx.type === 'contacts' ? '' : '?with=contacts');
      return amoGet(url).then(function (raw) {
        var linked = ctx.type === 'contacts' ? [{id:ctx.id, record:raw}] : (raw._embedded && raw._embedded.contacts || []);
        function next(i) {
          if (i >= linked.length) return Promise.reject(new Error('no_contact_phone'));
          var contactId = positiveId(linked[i].id || linked[i].contact_id);
          if (!contactId) return next(i+1);
          var record = linked[i].record ? Promise.resolve(linked[i].record) : amoGet('/api/v4/contacts/' + contactId);
          return record.then(function (data) {
            var phones = phoneFields(data);
            var p = chosen ? phones.filter(function (x) { return x === chosen; })[0] : phones[0];
            return p ? { key: ctx.key, crm_contact_id: contactId, phone: p,
              name: data.name || raw.name || '', responsible_user_id: positiveId(raw.responsible_user_id)} : next(i+1);
          }).catch(function () { return next(i+1); });
        }
        return next(0).catch(function(original){
          if(ctx.type==='contacts')throw original;
          function more(page){
            return amoGet('/api/v4/'+ctx.type+'/'+ctx.id+'/links?limit=250&page='+page).then(function(response){
              var rows=response._embedded&&response._embedded.links||[],start=linked.length;
              rows.forEach(function(row){
                var id=positiveId(row.to_entity_id);
                if(row.to_entity_type==='contacts'&&id&&!linked.some(function(contact){return positiveId(contact.id)===id;}))linked.push({id:id});
              });
              return next(start).catch(function(error){
                if(page<20&&(rows.length>=250||response._links&&response._links.next))return more(page+1);
                throw error;
              });
            });
          }
          return more(1);
        });
      });
    }
    function setError(message) { errorMessage = String(message || ''); statusMessage = ''; draw(); }
    function setStatus(message) { statusMessage = String(message || ''); draw(); }
    function serverError(error) {
      var code = error && (error.code || error.error || error.message) || '';
      return ERROR_MAP[code] || (error && error.status === 404 ? ERROR_MAP.widget_backend_not_ready : code) || 'Ошибка подключения к RATS';
    }
    function api(path, method, data) {
      var url = baseUrl();
      if (!url) return Promise.reject({code: 'invalid_rats_url'});
      if (typeof self.$authorizedAjax !== 'function')
        return Promise.reject({code: 'amocrm_signed_request_unavailable'});
      return new Promise(function (resolve, reject) {
        var options = {
          url: url + '/api/v1/amo/widget/v2' + path,
          type: method || 'GET', dataType: 'json', timeout: 15000,
          crossDomain: true, xhrFields: {withCredentials: false},
          headers: {'Accept': 'application/json'}
        };
        if (data !== undefined) {
          options.contentType = 'application/json';
          options.data = JSON.stringify(data);
          options.processData = false;
        }
        try {
          self.$authorizedAjax(options).done(resolve).fail(function (xhr) {
            var payload = xhr && xhr.responseJSON || {};
            reject({status: xhr && xhr.status, code: payload.error || payload.code ||
              (xhr && xhr.status === 404 ? 'widget_backend_not_ready' : 'connection_failed')});
          });
        } catch (e) { reject({code:'amocrm_signed_request_unavailable'}); }
      });
    }
    function autoIdentify() {
      // Fully automatic; the current amoCRM user's signed token accompanies this request.
      try{if(!agentId)agentId=randomId();}catch(e){setError(serverError(e));return;}
      api('/me?session_id='+encodeURIComponent(agentId),'GET').then(function (res) {
        if (!alive) return;
        principal = res;
        var active=res&&(res.initiated_call||res.active_call||res.forward_call);
        if (active && active.call_id && !callId) {
          call=active;callId = String(active.call_id); callState = active.state; callingStatus(true);
          if (!refreshTimer) refreshTimer = setInterval(refreshCall, 1800); refreshCall();
        }
        if (res && res.browser_available) sipConnect().catch(function () {});
        draw();
      }).catch(function (e) {
        if (alive) setError(serverError(e));
      });
    }
    function safeText(el, text) { var value=String(text == null ? '' : text);if(el&&el.textContent!==value)el.textContent=value; }
    function element(cls) { var root = document.getElementById(ROOT); return root && root.querySelector('.' + cls); }
    function callingStatus(active) {
      try { if (typeof APP !== 'undefined' && APP.sdk && typeof APP.sdk.setCallingStatus === 'function') APP.sdk.setCallingStatus(!!active); }
      catch (e) {}
    }
    function draw() {
      var root = document.getElementById(ROOT); if (!root) return;
      var ctx = context(), card = cardCache[ctx.key];
      safeText(element('rats-phone'), card && card.phone || '—');
      safeText(element('rats-name'), card && card.name || 'Контакт не выбран');
      var owner = managerKey === ctx.key ? manager : null;
      safeText(element('rats-user'), owner ? owner.operator_name : 'Определяется по карточке');
      safeText(element('rats-forward'), owner ? owner.forward_number_masked || 'Не назначен' : '—');
      safeText(element('rats-manager-status'), owner ? (owner.reason ? serverError({code:owner.reason}) :
        owner.status==='busy'&&!owner.browser_can_call ? 'Ответственный занят разговором' : owner.status==='break' ? 'Ответственный на перерыве' :
        mode==='browser' ? (owner.browser_online?'Браузер ответственного подключён':'Ответственный должен открыть amoCRM и подключить браузерный телефон') :
        owner.forward_available?'Первый вызов поступит на телефон ответственного':'Телефон ответственного не назначен') : '');
      var receiving=!callId&&receiveCall,shownState=receiving?receiveCall.state:callState;
      safeText(element('rats-status'), errorMessage || (receiving?agentStatus:statusMessage));
      safeText(element('rats-call-state'), ({idle:'Нет разговора', queued:'В очереди', ringing_operator:'Вызываем менеджера', ringing_customer:'Звоним клиенту', connected:'Разговор', ending:'Завершаем', reconciliation_required:'Требуется сверка Asterisk', ended:'Завершён', failed:'Ошибка'})[shownState] || shownState);
      var shownId=callId||(receiveCall&&receiveCall.call_id);
      var activeContext=call&&call.context;
      var otherClient=shownId&&activeContext&&(!card||Number(card.crm_contact_id)!==Number(activeContext.contact_id)||normalizePhone(card.phone)!==normalizePhone(activeContext.phone));
      var activeClient=element('rats-active-client');
      if(activeClient){activeClient.hidden=!otherClient;safeText(activeClient,otherClient?'Текущий звонок: '+(activeContext.contact_name||'Клиент')+' '+(activeContext.phone||''):'');}
      safeText(element('rats-call-id'), shownId ? 'ID: ' + shownId.slice(0,8) : '');
      var browser = element('rats-mode-browser'), forward = element('rats-mode-forward');
      if (browser) browser.checked = mode === 'browser';
      if (forward) forward.checked = mode === 'forward';
      var userBlock = element('rats-user-block'); if (userBlock) userBlock.hidden = false;
      var ready = !!principal && !!owner && !owner.reason && !!card && !!card.phone && !busy && !callId && !receiveCall &&
        (mode === 'browser' ? (owner.browser_can_call===undefined?owner.status==='free'&&owner.browser_online:owner.browser_can_call) : owner.status==='free'&&owner.forward_available);
      var start = element('rats-start'); if (start) start.disabled = !ready;
      var mic = element('rats-microphone'); if (mic) mic.hidden = mode !== 'browser';
      var hangup = element('rats-hangup'); if (hangup) hangup.disabled = !shownId;
      var retry=element('rats-retry');if(retry)retry.hidden=!requestId||!!shownId||busy;
      root.classList.toggle('rats-live', !!shownId);
      root.classList.toggle('rats-error', !!errorMessage);
      drawAgent();
    }
    function syncCard(force) {
      if (!alive || pendingCard) return;
      var ctx=context(); if (!ctx.key) { lastCardKey=''; draw(); return; }
      if (ctx.key === lastCardKey && cardCache[ctx.key] && !force) { syncManager(ctx); draw(); return; }
      lastCardKey=ctx.key; pendingCard=true;
      resolveCard(ctx).then(function (card) {
        cardCache[ctx.key]=card;
        if (context().key===ctx.key) {if(force)errorMessage='';setStatus('Контакт готов для звонка');}
      }).catch(function () { if (context().key===ctx.key) setError('В карточке нет связанного контакта с телефоном'); })
        .then(function () {pendingCard=false;syncManager(ctx);draw();});
    }
    function syncManager(ctx) {
      if (!ctx.key || managerPending || (managerKey===ctx.key && Date.now()-managerSynced<5000)) return;
      managerPending=true;managerSynced=Date.now();
      api('/context','POST',{entity_type:ctx.type,entity_id:ctx.id,session_id:agentId}).then(function(res){
        if(context().key !== ctx.key)return;
        manager=res.responsible;managerKey=ctx.key;draw();
      }).catch(function(e){if(context().key===ctx.key){manager=null;managerKey='';setError(serverError(e));}})
        .finally(function(){managerPending=false;});
    }
    function randomId() {
      if(window.crypto&&typeof window.crypto.randomUUID==='function')return window.crypto.randomUUID();
      if(!window.crypto||!window.crypto.getRandomValues)throw new Error('Для звонков требуется безопасное HTTPS-подключение');
      var bytes=window.crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
      var hex=Array.prototype.map.call(bytes,function(b){return ('0'+b.toString(16)).slice(-2);}).join('');
      return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);
    }
    function loadJsSIP(url) {
      if(window.RATSJsSIP) return Promise.resolve(window.RATSJsSIP);
      if(!url) return Promise.reject(new Error('Отсутствует локальная библиотека JsSIP'));
      var parsed = new URL(url,baseUrl());
      if(parsed.origin !== new URL(baseUrl()).origin || parsed.protocol!=='https:')return Promise.reject(new Error('JsSIP должен загружаться только с HTTPS-сервера RATS'));
      if(loadedJsSIP) return Promise.reject(new Error('JsSIP ещё загружается'));
      loadedJsSIP=true;
      return new Promise(function(resolve,reject){
        var script=document.createElement('script');script.src=parsed.href;script.async=true;
        script.onload=function(){loadedJsSIP=false;window.RATSJsSIP?resolve(window.RATSJsSIP):reject(new Error('JsSIP не загружен'));};
        script.onerror=function(){loadedJsSIP=false;reject(new Error('Не удалось загрузить JsSIP с сервера RATS'));};
        document.head.appendChild(script);
      });
    }
    function ensureAudio() {
      if (!remoteAudio) { remoteAudio=document.createElement('audio');remoteAudio.autoplay=true;remoteAudio.setAttribute('playsinline','');remoteAudio.style.display='none';document.body.appendChild(remoteAudio); }
      return remoteAudio;
    }
    function signalMediaReady() {
      if (mediaReadySent || !receiveCall || !localStream || !localStream.active || !mediaConnection) return;
      if (mediaConnection.connectionState !== 'connected') return;
      mediaReadySent = true;
      api('/calls/'+encodeURIComponent(receiveCall.call_id)+'/ready','POST',{session_id:agentId,token:receiveCall.operator_call_token})
        .catch(function(e){mediaReadySent=false;setError(serverError(e));});
    }
    function sessionAttach(session, isIncoming) {
      sipSession=session; sipIncoming=!!isIncoming; draw();
      var authoritativeConnection=null;
      function bind(pc,fromEvent) {
        if(!pc)return;
        if(fromEvent)authoritativeConnection=pc;
        if(fromEvent||!authoritativeConnection)mediaConnection=pc;
        if(boundConnections.has(pc))return;
        boundConnections.add(pc);
        pc.addEventListener('track',function(track){
          if(sipSession!==session||pc!==mediaConnection)return;
          ensureAudio().srcObject = track.streams && track.streams[0] || new MediaStream([track.track]);
          var playing = ensureAudio().play();
          if (playing && playing.catch) playing.catch(function(){agentStatus='Нажмите «Включить звук»';drawAgent();document.querySelector('.rats-agent-audio').hidden=false;});
        });
        pc.addEventListener('connectionstatechange',signalMediaReady);
        pc.addEventListener('iceconnectionstatechange',signalMediaReady);
        signalMediaReady();
      }
      session.on('peerconnection', function(e){bind(e.peerconnection,true);});
      bind(session.connection);
      session.on('confirmed',function(){sipIncoming=false;callingStatus(true);agentStatus='Браузер подключён. Ожидаем клиента';drawAgent();bind(session.connection);});
      function finish(){if(sipSession!==session)return;sipSession=null;sipIncoming=false;mediaConnection=null;mediaReadySent=false;
        receiveCall=null;agentStatus='Браузерный телефон готов';
        if(localStream){localStream.getTracks().forEach(function(t){t.stop();});localStream=null;}
        callingStatus(!!callId);drawAgent();draw();}
      session.on('ended',finish);session.on('failed',finish);
    }
    function sipConnect() {
      if(sipUA && sipRegistered) return Promise.resolve();
      if(connectingPromise)return connectingPromise;
      if(!principal) return Promise.reject({code:'operator_mapping_missing'});
      var epoch=connectionEpoch;
      function current(){if(!alive||epoch!==connectionEpoch)throw{code:'agent_session_lost'};}
      agentStatus='Подготавливаем браузерный телефон';drawAgent();
      connectingPromise=Promise.resolve().then(function(){
        current();
        if(!agentId)agentId=randomId();
        if(sipUA){sipUA.stop();sipUA=null;}
        return api('/agent/lease','POST',{session_id:agentId});
      }).then(function(){
        current();
        agentLease=true;
        return api('/webrtc/config?session_id='+encodeURIComponent(agentId),'GET');
      }).then(function(cfg){
        current();
        webrtc=cfg;
        if(cfg.ready!==true||!/^wss:\/\//.test(cfg.wss_url||'')||!/^sip:[^@]+@[^@]+$/.test(cfg.sip_uri||''))throw{code:'browser_endpoint_not_ready'};
        return loadJsSIP(cfg.library_url||'');
      }).then(function(JsSIP){
        current();
        var password=webrtc.sip_password||'';
        if(!password)throw {code:'browser_endpoint_not_ready'};
        var socket=new JsSIP.WebSocketInterface(webrtc.wss_url);
        sipUA=new JsSIP.UA({sockets:[socket],uri:webrtc.sip_uri,password:password,
          authorization_user:webrtc.auth_user||undefined,register:true,session_timers:false});
        webrtc.sip_password=null;
        var ua=sipUA,registerResolve,registerReject;
        var registration=new Promise(function(resolve,reject){registerResolve=resolve;registerReject=reject;});
        var registrationTimer=setTimeout(function(){registerReject({code:'browser_not_registered'});},20000);
        sipUA.on('registered',function(){if(sipUA!==ua)return;sipRegistered=true;agentStatus='Браузерный телефон зарегистрирован на Asterisk';heartbeat();drawAgent();clearTimeout(registrationTimer);registerResolve();});
        sipUA.on('unregistered',function(){if(sipUA!==ua)return;sipRegistered=false;agentStatus='Браузерный телефон отключён';heartbeat();drawAgent();});
        sipUA.on('disconnected',function(){if(sipUA!==ua)return;sipRegistered=false;agentStatus='Нет подключения к Asterisk';heartbeat();drawAgent();});
        sipUA.on('registrationFailed',function(){if(sipUA!==ua)return;sipRegistered=false;agentStatus='Ошибка регистрации браузерного телефона';heartbeat();drawAgent();clearTimeout(registrationTimer);registerReject({code:'browser_not_registered'});});
        sipUA.on('newRTCSession',function(e){
          if(e.originator!=='remote')return;
          if(!alive||sipUA!==ua){e.session.terminate();return;}
          if(sipSession){e.session.terminate();return;}
          var deliveredToken = '';
          try { deliveredToken = String(e.request && e.request.getHeader && e.request.getHeader('X-RATS-Call-Token') || ''); } catch (_error) {}
          if (!/^[a-f0-9]{48}$/.test(deliveredToken)) {e.session.terminate();return;}
          // Only the participant's signed identity and leased page can accept this nonce.
          sipSession=e.session;
          function abandoned(){if(sipSession===e.session&&!receiveCall){sipSession=null;sipIncoming=false;drawAgent();}}
          e.session.on('ended',abandoned);e.session.on('failed',abandoned);
          api('/agent/invite','POST',{session_id:agentId,token:deliveredToken}).then(function(res){
            if(sipSession!==e.session)return;
            var autoAnswer=!!localStream&&res.call_id===callId;
            receiveCall=res;call=res;mediaReadySent=false;mode='browser';
            if(!callId){callId=res.call_id;callState=res.state;errorMessage='';statusMessage='Входящий звонок';
              if(!refreshTimer)refreshTimer=setInterval(refreshCall,1800);}
            sessionAttach(e.session,true);
            agentStatus='Входящий звонок — '+(res.context.contact_name||'Клиент');drawAgent();
            if(autoAnswer)answer();
          }).catch(function(){if(sipSession===e.session)sipSession=null;try{e.session.terminate();}catch(ignore){}});
        });
        try{sipUA.start();}catch(startError){registerReject(startError);}
        if(!sipRegistered){agentStatus='Подключаем браузерный SIP-телефон';drawAgent();}
        return registration.finally(function(){clearTimeout(registrationTimer);});
      }).catch(function(e){if(epoch!==connectionEpoch)throw e;agentLease=false;sipRegistered=false;
        if(sipUA&&!sipSession){var failed=sipUA;sipUA=null;try{failed.stop();}catch(ignore){}}
        agentStatus=serverError(e);drawAgent();throw e;})
        .finally(function(){if(epoch===connectionEpoch)connectingPromise=null;});
      return connectingPromise;
    }
    function answer() {
      if(!sipSession||!sipIncoming)return;
      var session=sipSession;sipIncoming=false;drawAgent();
      var media=localStream?Promise.resolve(localStream):navigator.mediaDevices.getUserMedia({audio:true,video:false});
      media.then(function(stream){
        if(sipSession!==session){stream.getTracks().forEach(function(t){t.stop();});return;}
        localStream=stream;
        session.answer({mediaStream:stream,mediaConstraints:{audio:true,video:false},pcConfig:{iceServers:(webrtc&&webrtc.ice_servers)||[]}});
      }).catch(function(){agentStatus='Разрешите доступ к микрофону';sipIncoming=true;drawAgent();});
    }
    function heartbeat(){
      if(!alive||!agentLease||heartbeatInFlight)return;
      var epoch=connectionEpoch,sessionId=agentId;
      heartbeatInFlight=true;
      api('/agent/heartbeat','POST',{session_id:agentId,registered:sipRegistered}).then(function(res){
        if(epoch!==connectionEpoch||sessionId!==agentId)return;
        if(res.status)principal.status=res.status;drawAgent();
      }).catch(function(e){
        if(epoch!==connectionEpoch||sessionId!==agentId)return;
        if(e.code==='agent_session_lost'){
          agentLease=false;sipRegistered=false;
          if(sipUA){try{sipUA.stop();}catch(ignore){}sipUA=null;}
          if(!callId&&!receiveCall&&!requestId)agentId=null;
        }
        agentStatus=serverError(e);drawAgent();
      }).finally(function(){if(epoch===connectionEpoch)heartbeatInFlight=false;});
    }
    function drawAgent(){
      var panel=document.getElementById('rats-browser-agent');
      var host=element('rats-agent-host');
      if(!panel){panel=document.createElement('section');panel.id='rats-browser-agent';panel.className='rats-agent';
        panel.innerHTML='<div class="rats-label">Браузерный телефон</div><div class="rats-agent-status" role="status"></div><div class="rats-agent-client"></div>'+
          '<button class="rats-agent-answer rats-answer" hidden>Ответить</button><button class="rats-agent-hangup rats-danger" hidden>Завершить</button>'+
          '<button class="rats-agent-audio rats-text" hidden>Включить звук</button>'+
          '<button class="rats-agent-availability rats-text">Перерыв</button>';
      }
      var parent=host||document.body;
      if(panel.parentNode!==parent)parent.appendChild(panel);
      panel.classList.toggle('rats-agent-fallback',!host);
      var sameCall=!!receiveCall&&receiveCall.call_id===callId;
      safeText(panel.querySelector('.rats-agent-status'),host&&receiveCall&&!sipIncoming?'Браузер подключён':agentStatus);
      safeText(panel.querySelector('.rats-agent-client'),receiveCall?((receiveCall.context.contact_name||'Клиент')+' '+(receiveCall.context.phone||'')):'');
      var card=cardCache[context().key];
      var sameContact=receiveCall&&card&&Number(card.crm_contact_id)===Number(receiveCall.context.contact_id)
        &&normalizePhone(card.phone)===normalizePhone(receiveCall.context.phone);
      panel.querySelector('.rats-agent-client').hidden=!!host&&sameCall&&!!sameContact;
      panel.querySelector('.rats-agent-answer').hidden=!sipIncoming;
      panel.querySelector('.rats-agent-hangup').hidden=!receiveCall||!!host&&(!callId||sameCall);
      var available=panel.querySelector('.rats-agent-availability');
      available.hidden=!agentLease||!!receiveCall;
      available.disabled=!!principal&&principal.status==='busy';
      safeText(available,principal&&principal.status==='break'?'Готов к звонкам':'Перерыв');
      panel.hidden=!agentStatus||!host&&!receiveCall;
      if(host)host.hidden=panel.hidden;
    }
    function agentTick(){
      if(!principal || !principal.browser_available)return;
      if(!agentLease){if(++agentTickCount%4===0)sipConnect().catch(function(){});return;}
      if(++agentTickCount%2===0)heartbeat();
      signalMediaReady();
      if(!receiveCall||agentPolling)return;
      agentPolling=true;var id=receiveCall.call_id;
      api('/calls/'+encodeURIComponent(id),'GET').then(function(res){
        if(!receiveCall||receiveCall.call_id!==id)return;
        receiveCall.state=res.cancel_requested&&res.state==='reconciliation_required'?'ending':res.state;
        if(res.cancel_requested)mediaReadySent=true;
        agentStatus=({connected:'Разговор с клиентом',ringing_customer:'Вызываем клиента',ringing_operator:'Вызов менеджера'})[res.state]||agentStatus;
        if(res.state==='ended'||res.state==='failed'){
          if(sipSession)try{sipSession.terminate();}catch(ignore){}
        }
        drawAgent();draw();
      }).finally(function(){agentPolling=false;}).catch(function(){});
    }
    function startCall(card,overridePhone,replay) {
      if(busy||callId||sipSession)return;
      var selected=element('rats-mode-forward');
      if(selected)mode=selected.checked?'forward':'browser';
      var c=card,phone=normalizePhone(overridePhone||(c&&c.phone));
      if(!principal)return setError('Сначала необходимо подтвердить пользователя amoCRM');
      if(!replay&&(!c||!c.crm_contact_id||!phone))return setError('Выберите контакт с действительным номером');
      if(!replay&&(!manager||managerKey!==context().key))return setError('Подождите определения ответственного');
      var ctx=context();
      var body={phone:phone,crm_contact_id:c.crm_contact_id,mode:mode,
        amo_account:system().subdomain||'',entity_type:ctx.type||'contacts',entity_id:ctx.id||c.crm_contact_id,protocol_version:3,session_id:agentId};
      if(replay){if(!requestBody)return;body=requestBody;mode=body.mode;}
      if(requestBody && JSON.stringify(body)!==JSON.stringify(requestBody))return setError('Повторите предыдущий запрос в исходной карточке, чтобы уточнить его результат');
      try{if(!requestId){requestId=randomId();requestBody=body;requestSelf=manager.operator_id===principal.operator_id;requestDispatched=false;requestAmbiguous=false;}}
      catch(e){return setError(serverError(e));}
      busy=true;errorMessage='';setStatus('Запрашиваем звонок через Мультиком…');
      var isSelf=requestSelf;
      var media=Promise.resolve().then(function(){return mode==='browser'&&isSelf?navigator.mediaDevices.getUserMedia({audio:true,video:false}):null;});
      media.then(function(stream){localStream=stream;mediaReadySent=false;requestDispatched=true;return api('/calls','POST',Object.assign({},body,{client_request_id:requestId}));})
        .then(function(result){
          if(!result.call_id)throw{code:'widget_backend_not_ready'};
          call=result;callId=String(result.call_id);callState=result.state||'queued';callingStatus(true);
          if(!result.is_participant&&localStream){localStream.getTracks().forEach(function(t){t.stop();});localStream=null;}
          setStatus(mode==='browser'?'Ожидаем вызов в браузере':'Сначала вызываем телефон менеджера');
          if(refreshTimer)clearInterval(refreshTimer);refreshTimer=setInterval(refreshCall,1800);
          return api('/calls/'+encodeURIComponent(callId)+'/start','POST',{}).then(refreshCall);
        }).catch(function(e){
          if(e.name==='NotAllowedError'||e.name==='NotFoundError'){if(!requestDispatched){requestId=null;requestBody=null;}setError('Разрешите микрофон в браузере и проверьте выбранное устройство');}
          else setError(serverError(e));
          if(!callId&&requestDispatched&&(!e.status||e.status>=500))requestAmbiguous=true;
          if(e.status && e.status<500 && !callId&&!requestAmbiguous){requestId=null;requestBody=null;}
          if(!callId && localStream){localStream.getTracks().forEach(function(t){t.stop();});localStream=null;}
        })
        .then(function(){busy=false;draw();});
    }
    function refreshCall() {
      if(!callId||pollInFlight)return;
      pollInFlight=true;var id=callId;
      api('/calls/'+encodeURIComponent(id),'GET').then(function(result){
        if(callId!==id)return;
        call=result;
        callState=result.state||callState;
        if((result.cancel_requested||hangupRequested) && callState==='reconciliation_required')callState='ending';
        if (!result.cancel_requested && !hangupRequested && callState !== 'ended' && callState !== 'failed') signalMediaReady();
        if(callState==='ended'||callState==='failed'){
          setStatus(callState==='ended'?'Звонок завершён':serverError({code:result.reason})||'Звонок завершился ошибкой');
          callId=null;call=null;hangupRequested=false;requestId=null;requestBody=null;requestDispatched=false;requestAmbiguous=false;mediaReadySent=false;callingStatus(!!sipSession);
          if(sipSession&&receiveCall&&receiveCall.call_id===id)try{sipSession.terminate();}catch(ignore){}
          if(localStream&&!receiveCall){localStream.getTracks().forEach(function(t){t.stop();});localStream=null;}
          if(refreshTimer)clearInterval(refreshTimer);refreshTimer=null;
          managerSynced=0;syncManager(context());
        }else { statusMessage=({ringing_operator:'Вызов менеджера',ringing_customer:'Вызываем клиента',connected:'Разговор с клиентом',ending:'Проверяем завершение звонка в Asterisk'})[callState]||'Ожидаем ответа';draw(); }
      }).catch(function(e){if(e.status===401){principal=null;setError(serverError(e));}})
        .then(function(){pollInFlight=false;draw();});
    }
    function hangup(){
      var id=callId||(receiveCall&&receiveCall.call_id);if(!id)return;
      api('/calls/'+encodeURIComponent(id)+'/hangup','POST',{})
        .then(function(){hangupRequested=true;callState='ending';if(receiveCall&&receiveCall.call_id===id)receiveCall.state='ending';setStatus('Проверяем завершение звонка в Asterisk');})
        .catch(function(e){setError(serverError(e));});
      if(sipSession&&receiveCall&&receiveCall.call_id===id)try{sipSession.terminate();}catch(e){}
    }
    function selectMode(next) {
      if(callId||receiveCall||busy||requestId)return setError('Сначала завершите текущий запрос');
      if(next!=='browser'&&next!=='forward')return;
      mode=next;
      if(next === 'browser' && !sipRegistered && principal) sipConnect().catch(function () {});
      setStatus(next==='browser'?'Разговор через компьютер и WebRTC':'Первый вызов поступит на закреплённый телефон менеджера');
    }
    function routePhoneClick(data) {
      var phone=normalizePhone(data&&data.value!==undefined?data.value:data);if(!phone)return;
      var ctx=context();if(!ctx.key)return setError('Откройте сохранённую карточку');
      resolveCard(ctx,phone).then(function(c){startCall(c,phone);}).catch(function(){setError('Номер не принадлежит контакту карточки');});
    }
    function click(e) {
      var agent=document.getElementById('rats-browser-agent');
      if(agent&&agent.contains(e.target)){
        var action=e.target.closest('button');if(!action)return;
        if(action.classList.contains('rats-agent-answer'))answer();
        if(action.classList.contains('rats-agent-hangup')&&receiveCall){
          api('/calls/'+encodeURIComponent(receiveCall.call_id)+'/hangup','POST',{}).catch(function(e){agentStatus=serverError(e);drawAgent();});
          if(sipSession)try{sipSession.terminate();}catch(ignore){}
        }
        if(action.classList.contains('rats-agent-audio'))ensureAudio().play().then(function(){action.hidden=true;}).catch(function(){agentStatus='Не удалось включить звук. Проверьте разрешение браузера и устройство вывода';drawAgent();});
        if(action.classList.contains('rats-agent-availability')){
          api('/agent/availability','POST',{status:principal.status==='break'?'free':'break'}).then(function(res){
            principal.status=res.status;drawAgent();syncManager(context());
          }).catch(function(e){agentStatus=serverError(e);drawAgent();});
        }
        return;
      }
      var r=document.getElementById(ROOT);if(!r||!r.contains(e.target))return;
      var t=e.target.closest&&e.target.closest('button,input');if(!t)return;
      if(t.classList.contains('rats-start')){e.preventDefault();startCall(cardCache[context().key]);}
      if(t.classList.contains('rats-retry')&&requestBody){e.preventDefault();startCall({crm_contact_id:requestBody.crm_contact_id,phone:requestBody.phone},null,true);}
      if(t.classList.contains('rats-hangup')){e.preventDefault();hangup();}
      if(t.classList.contains('rats-answer')){e.preventDefault();answer();}
      if(t.classList.contains('rats-audio')){e.preventDefault();ensureAudio().play().then(function(){t.hidden=true;}).catch(function(){setError('Не удалось включить звук');});}
      if(t.classList.contains('rats-refresh')){e.preventDefault();delete cardCache[context().key];syncCard(true);}
    }
    function changeMode(e){
      var root=document.getElementById(ROOT);
      if(!root||!root.contains(e.target))return;
      if(e.target.classList.contains('rats-mode-browser'))selectMode('browser');
      if(e.target.classList.contains('rats-mode-forward'))selectMode('forward');
    }
    function template() {
      return '<section id="'+ROOT+'" class="rats-widget" role="region" aria-label="Телефония RATS">' +
        '<header class="rats-head"><div class="rats-mark">R<span>▲</span>TS</div><div class="rats-head-sub">MULTICOM · ASTERISK</div></header>' +
        '<div class="rats-section"><div class="rats-label">Клиент</div><div class="rats-name">Контакт не выбран</div><div class="rats-phone">—</div></div>' +
        '<div class="rats-section rats-user-block" hidden><div class="rats-label">Ответственный менеджер</div><div class="rats-user"></div><div class="rats-label rats-label-small">Телефон переадресации</div><div class="rats-forward">—</div><p class="rats-hint rats-manager-status"></p></div>' +
        '<div class="rats-section"><div class="rats-label">Куда подключить менеджера?</div><label class="rats-radio"><input type="radio" name="rats-mode" class="rats-mode-browser" checked> В браузере amoCRM</label>' +
        '<label class="rats-radio"><input type="radio" name="rats-mode" class="rats-mode-forward"> На телефон ответственного</label>' +
        '<p class="rats-hint rats-microphone">Первый вызов поступит в браузер ответственного из карточки.</p></div>' +
        '<div class="rats-section rats-agent-host"></div>' +
        '<div class="rats-actions">' +
        '<button class="rats-start rats-primary" type="button" disabled>Позвонить клиенту</button>' +
        '<button class="rats-hangup rats-danger" type="button" disabled>Завершить разговор</button>' +
        '<button class="rats-retry rats-text" type="button" hidden>Продолжить предыдущий звонок</button>' +
        '<button class="rats-refresh rats-text" type="button">Обновить контакт</button></div>' +
        '<div class="rats-call"><span class="rats-call-state">Нет разговора</span><small class="rats-call-id"></small></div>' +
        '<p class="rats-active-client rats-hint" hidden></p>' +
        '<div class="rats-status" role="status" aria-live="polite"></div><div class="rats-footer">RATS '+VERSION+' · Remote Automatic Telephone System</div></section>';
    }
    function loadStyles() {
      var s=settings(),path=String(s.path||(self.params&&self.params.path)||'').replace(/\/+$/,'');
      if(!path&&s.widget_code)path='/widgets/'+encodeURIComponent(String(s.widget_code));
      if(!path)return;
      var href=path+'/style.css?v='+VERSION;
      if(document.querySelector('link[data-rats-style="'+href+'"]'))return;
      Array.prototype.forEach.call(document.querySelectorAll('link[data-rats-style]'),function(old){old.remove();});
      var link=document.createElement('link');link.rel='stylesheet';link.href=href;link.setAttribute('data-rats-style',href);document.head.appendChild(link);
    }
    self.callbacks={
      render:function(){alive=true;loadStyles();if(areaType(system().area)&&typeof self.render_template==='function'){
        try{self.render_template({caption:{class_name:'rats-widget-caption'},body:'',render:template()});}
        catch(e){console.warn('RATS render error',e);}
      }return true;},
      init:function(){alive=true;
        if(!phoneActionRegistered&&typeof self.add_action==='function'){
          try{self.add_action('phone',routePhoneClick);phoneActionRegistered=true;}catch(e){console.warn('RATS phone action',e);}
        }
        if(!timer)timer=setInterval(function(){if(document.getElementById(ROOT))syncCard(false);agentTick();},1700);
        if(!wired){document.addEventListener('click',click,false);document.addEventListener('change',changeMode,false);wired=true;}
        autoIdentify();
        setTimeout(function(){syncCard(false);draw();},0);
        return true;},
      bind_actions:function(){draw();return true;},
      settings:function(){return true;},onSave:function(){return true;},
      // amoCRM destroys card views during navigation. The receiving phone belongs
      // to the signed-in manager's page and continues through those transitions.
      destroy:function(){lastCardKey='';managerKey='';cardCache={};return true;}
    };
    window.addEventListener('pagehide',function(){
      alive=false;connectionEpoch++;connectingPromise=null;heartbeatInFlight=false;
      if(timer)clearInterval(timer);
      if(refreshTimer)clearInterval(refreshTimer);
      timer=null;refreshTimer=null;
      if(receiveCall&&receiveCall.operator_call_token){
        // A narrowly scoped participant capability; closing an initiator's page
        // must never disconnect a conversation taking place in someone else's browser.
        var payload=JSON.stringify({call_id:receiveCall.call_id,session_id:agentId,token:receiveCall.operator_call_token});
        var url=baseUrl()+'/api/v1/amo/widget/v2/agent/disconnect';
        try{if(!navigator.sendBeacon||!navigator.sendBeacon(url,new Blob([payload],{type:'text/plain'})))
          fetch(url,{method:'POST',body:payload,keepalive:true,credentials:'omit',headers:{'Content-Type':'text/plain'}}).catch(function(){});
        }catch(ignore){}
      }
      if(sipUA){var leaving=sipUA;sipUA=null;try{leaving.stop();}catch(ignore){}}
      sipRegistered=false;agentLease=false;
      if(localStream)localStream.getTracks().forEach(function(t){t.stop();});
    });
    window.addEventListener('pageshow',function(event){
      if(!event.persisted)return;
      alive=true;sipUA=null;sipSession=null;receiveCall=null;localStream=null;
      sipRegistered=false;agentLease=false;mediaReadySent=false;mediaConnection=null;
      if(!timer)timer=setInterval(function(){if(document.getElementById(ROOT))syncCard(false);agentTick();},1700);
      if(callId&&!refreshTimer)refreshTimer=setInterval(refreshCall,1800);
      autoIdentify();
    });
    return this;
  };
});
