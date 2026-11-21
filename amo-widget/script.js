define(['jquery'], function ($) {
  'use strict';

  return function RATSWidget() {
    var self = this;
    var VERSION = '5.2.0';
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
    var mode = 'forward';
    var busy = false;
    var callId = null;
    var call = null;
    var callState = 'idle';
    var pollInFlight = false;
    var sipUA = null;
    var sipRegistered = false;
    var sipSession = null;
    var sipIncoming = false;
    var expectedSipToken = null;
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
    var boundConnections = new WeakSet();

    var ERROR_MAP = {
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
      destination_not_allowed: 'Номер клиента недоступен для звонка',
      browser_endpoint_not_ready: 'WebRTC сотрудника ещё не настроен',
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
        return next(0);
      });
    }
    function setError(message) { errorMessage = String(message || ''); statusMessage = ''; draw(); }
    function setStatus(message) { statusMessage = String(message || ''); errorMessage = ''; draw(); }
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
      api('/me','GET').then(function (res) {
        if (!alive) return;
        principal = res;
        if (res && res.active_call && res.active_call.call_id && !callId) {
          callId = String(res.active_call.call_id); callState = res.active_call.state; callingStatus(true);
          if (!refreshTimer) refreshTimer = setInterval(refreshCall, 1800); refreshCall();
        }
        if (res && res.operator_id && mode === 'browser') sipConnect().catch(function () {});
        draw();
      }).catch(function (e) {
        if (alive) setError(serverError(e));
      });
    }
    function safeText(el, text) { if (el) el.textContent = String(text == null ? '' : text); }
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
      safeText(element('rats-user'), principal ? principal.operator_name || 'Оператор RATS' : 'Определяется автоматически');
      safeText(element('rats-forward'), principal ? principal.forward_number_masked || 'Не назначен' : '—');
      safeText(element('rats-status'), errorMessage || statusMessage);
      safeText(element('rats-call-state'), ({idle:'Нет разговора', queued:'В очереди', ringing_operator:'Вызываем менеджера', ringing_customer:'Звоним клиенту', connected:'Разговор', ending:'Завершаем', reconciliation_required:'Требуется сверка Asterisk', ended:'Завершён', failed:'Ошибка'})[callState] || callState);
      safeText(element('rats-call-id'), callId ? 'ID: ' + callId.slice(0,8) : '');
      var browser = element('rats-mode-browser'), forward = element('rats-mode-forward');
      if (browser) browser.checked = mode === 'browser';
      if (forward) forward.checked = mode === 'forward';
      var userBlock = element('rats-user-block'); if (userBlock) userBlock.hidden = false;
      var ready = !!principal && !!card && !!card.phone && !busy && !callId && (mode !== 'browser' || sipRegistered);
      var start = element('rats-start'); if (start) start.disabled = !ready;
      var mic = element('rats-microphone'); if (mic) mic.hidden = mode !== 'browser';
      var hangup = element('rats-hangup'); if (hangup) hangup.disabled = !callId;
      var answer = element('rats-answer'); if (answer) { answer.hidden = !sipIncoming; answer.disabled = !sipIncoming; }
      root.classList.toggle('rats-live', !!callId);
      root.classList.toggle('rats-error', !!errorMessage);
    }
    function syncCard(force) {
      if (!alive || pendingCard) return;
      var ctx=context(); if (!ctx.key) { lastCardKey=''; draw(); return; }
      if (ctx.key === lastCardKey && cardCache[ctx.key] && !force) { draw(); return; }
      lastCardKey=ctx.key; pendingCard=true;
      resolveCard(ctx).then(function (card) {
        cardCache[ctx.key]=card;
        if (context().key===ctx.key) setStatus('Контакт готов для звонка');
      }).catch(function () { if (context().key===ctx.key) setError('В карточке нет связанного контакта с телефоном'); })
        .then(function () {pendingCard=false; draw();});
    }
    function randomId() {
      if (!window.crypto || typeof window.crypto.randomUUID!=='function') throw new Error('Требуется HTTPS и crypto.randomUUID');
      return window.crypto.randomUUID();
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
      if (mediaReadySent || !callId || mode !== 'browser' || !localStream || !localStream.active || !mediaConnection) return;
      if (mediaConnection.connectionState !== 'connected') return;
      mediaReadySent = true;
      api('/calls/'+encodeURIComponent(callId)+'/ready','POST',{})
        .catch(function(e){mediaReadySent=false;setError(serverError(e));});
    }
    function sessionAttach(session, isIncoming) {
      sipSession=session; sipIncoming=!!isIncoming; draw();
      function bind(pc) {
        if (!pc || boundConnections.has(pc)) return;
        boundConnections.add(pc);
        mediaConnection = pc;
        pc.addEventListener('track',function(track){
          ensureAudio().srcObject = track.streams && track.streams[0] || new MediaStream([track.track]);
          var playing = ensureAudio().play();
          if (playing && playing.catch) playing.catch(function(){setError('Браузер заблокировал звук. Нажмите «Включить звук»');var audioButton=element('rats-audio');if(audioButton)audioButton.hidden=false;});
        });
        pc.addEventListener('connectionstatechange',signalMediaReady);
        pc.addEventListener('iceconnectionstatechange',signalMediaReady);
        signalMediaReady();
      }
      session.on('peerconnection', function(e){bind(e.peerconnection);});
      bind(session.connection);
      session.on('confirmed',function(){sipIncoming=false;callingStatus(true);setStatus('Браузер подключён. Ожидаем соединения с клиентом');bind(session.connection);});
      function finish(){sipSession=null;sipIncoming=false;mediaConnection=null;callingStatus(!!callId);draw();}
      session.on('ended',finish);session.on('failed',finish);
    }
    function sipConnect() {
      if(sipUA && sipRegistered) return Promise.resolve();
      if(sipUA) return Promise.reject({code:'browser_endpoint_not_ready'});
      if(!principal) return Promise.reject({code:'operator_mapping_missing'});
      setStatus('Подготавливаем браузерную телефонию…');
      return api('/webrtc/config','GET').then(function(cfg){
        webrtc=cfg;
        if(cfg.ready!==true||!/^wss:\/\//.test(cfg.wss_url||'')||!/^sip:[^@]+@[^@]+$/.test(cfg.sip_uri||''))throw{code:'browser_endpoint_not_ready'};
        return loadJsSIP(cfg.library_url||'');
      }).then(function(JsSIP){
        var password=webrtc.sip_password||'';
        if(!password)throw {code:'browser_endpoint_not_ready'};
        var socket=new JsSIP.WebSocketInterface(webrtc.wss_url);
        sipUA=new JsSIP.UA({sockets:[socket],uri:webrtc.sip_uri,password:password,
          authorization_user:webrtc.auth_user||undefined,register:true,session_timers:false});
        webrtc.sip_password=null;
        sipUA.on('registered',function(){sipRegistered=true;setStatus('Браузерный телефон зарегистрирован на Asterisk');});
        sipUA.on('unregistered',function(){sipRegistered=false;setStatus('Браузерный телефон отключён');});
        sipUA.on('disconnected',function(){sipRegistered=false;setError('SIP WebSocket отключился — проверьте подключение Asterisk');});
        sipUA.on('registrationFailed',function(){sipRegistered=false;setError('SIP-регистрация браузера не прошла');});
        sipUA.on('newRTCSession',function(e){
          if(e.originator!=='remote')return;
          if(sipSession){e.session.terminate();return;}
          if(!callId){e.session.terminate();return;}
          var deliveredToken = '';
          try { deliveredToken = String(e.request && e.request.getHeader && e.request.getHeader('X-RATS-Call-Token') || ''); } catch (_error) {}
          if (!expectedSipToken || deliveredToken !== expectedSipToken) {e.session.terminate();return;}
          sessionAttach(e.session,false);
          try {
            e.session.answer({mediaStream:localStream,mediaConstraints:{audio:true,video:false},pcConfig:{iceServers:(webrtc&&webrtc.ice_servers)||[]}});
            setStatus('Телефон подключается. Ожидаем клиента');
          } catch (answerError) {setError('Не удалось подключить микрофон');hangup();}
        });
        sipUA.start();
        setStatus('Подключаем браузерный SIP-телефон…');
      }).catch(function(e){ setError(serverError(e)); throw e; });
    }
    function answer() {
      if(!sipSession||!sipIncoming)return;
      sipIncoming=false;
      try{sipSession.answer({mediaConstraints:{audio:true,video:false},pcConfig:{iceServers:(webrtc&&webrtc.ice_servers)||[]}});}
      catch(e){setError('Нет доступа к микрофону или ошибка WebRTC');}
      draw();
    }
    function startCall(card,overridePhone) {
      if(busy||callId)return;
      var c=card,phone=normalizePhone(overridePhone||(c&&c.phone));
      if(!principal)return setError('Сначала необходимо подтвердить пользователя amoCRM');
      if(!c||!c.crm_contact_id||!phone)return setError('Выберите контакт с действительным номером');
      if(mode==='browser' && !sipRegistered)return setError('Браузерная телефония ещё не зарегистрирована. Выберите телефон менеджера или обратитесь к администратору');
      if(mode==='browser' && (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia))return setError('Для микрофона нужен современный браузер и защищённая страница HTTPS');
      var ctx=context();
      var body={phone:phone,crm_contact_id:c.crm_contact_id,mode:mode,
        amo_account:system().subdomain||'',entity_type:ctx.type||'contacts',entity_id:ctx.id||c.crm_contact_id};
      if(requestBody && JSON.stringify(body)!==JSON.stringify(requestBody))return setError('Повторите предыдущий запрос в исходной карточке, чтобы уточнить его результат');
      if(!requestId){requestId=randomId();requestBody=body;}
      busy=true;setStatus('Запрашиваем звонок через Мультиком…');
      var media=mode==='browser'?navigator.mediaDevices.getUserMedia({audio:true,video:false}):Promise.resolve(null);
      media.then(function(stream){localStream=stream;mediaReadySent=false;return api('/calls','POST',Object.assign({},body,{client_request_id:requestId}));})
        .then(function(result){
          if(!result.call_id)throw{code:'widget_backend_not_ready'};
          callId=String(result.call_id);expectedSipToken=String(result.operator_call_token||'');callState=result.state||'queued';callingStatus(true);
          setStatus(mode==='browser'?'Ожидаем вызов в браузере':'Сначала вызываем телефон менеджера');
          if(refreshTimer)clearInterval(refreshTimer);refreshTimer=setInterval(refreshCall,1800);
          return api('/calls/'+encodeURIComponent(callId)+'/start','POST',{}).then(refreshCall);
        }).catch(function(e){
          if(e.name==='NotAllowedError'||e.name==='NotFoundError'){requestId=null;requestBody=null;setError('Разрешите микрофон в браузере и проверьте выбранное устройство');}
          else setError(serverError(e));
          if(e.status && e.status<500 && !callId){requestId=null;requestBody=null;}
          if(!callId && localStream){localStream.getTracks().forEach(function(t){t.stop();});localStream=null;}
        })
        .then(function(){busy=false;draw();});
    }
    function refreshCall() {
      if(!callId||pollInFlight)return;
      pollInFlight=true;var id=callId;
      api('/calls/'+encodeURIComponent(id),'GET').then(function(result){
        if(callId!==id)return;
        callState=result.state||callState;
        if (callState !== 'ended' && callState !== 'failed') signalMediaReady();
        if(callState==='ended'||callState==='failed'){
          setStatus(callState==='ended'?'Звонок завершён':serverError({code:result.reason})||'Звонок завершился ошибкой');
          callId=null;expectedSipToken=null;requestId=null;requestBody=null;mediaReadySent=false;callingStatus(false);
          if(sipSession)try{sipSession.terminate();}catch(ignore){}
          if(localStream){localStream.getTracks().forEach(function(t){t.stop();});localStream=null;}
          if(refreshTimer)clearInterval(refreshTimer);refreshTimer=null;
        }else { statusMessage=({ringing_operator:'Вызов менеджера',ringing_customer:'Вызываем клиента',connected:'Разговор с клиентом'})[callState]||'Ожидаем ответа';draw(); }
      }).catch(function(e){if(e.status===401){principal=null;setError(serverError(e));}})
        .then(function(){pollInFlight=false;draw();});
    }
    function hangup(){
      if(!callId)return;
      var id=callId;
      api('/calls/'+encodeURIComponent(id)+'/hangup','POST',{})
        .then(function(){setStatus('Запрошено завершение разговора');})
        .catch(function(e){setError(serverError(e));});
      if(sipSession)try{sipSession.terminate();}catch(e){}
    }
    function selectMode(next) {
      if(callId||busy||requestId)return setError('Сначала завершите текущий запрос');
      if(next!=='browser'&&next!=='forward')return;
      mode=next;
      if(next === 'browser' && !sipRegistered && principal) sipConnect().catch(function () {});
      setStatus(next==='browser'?'Разговор через компьютер и WebRTC':'Первый вызов поступит на закреплённый телефон менеджера');
    }
    function routePhoneClick(data) {
      var phone=normalizePhone(data&&data.value!==undefined?data.value:data);if(!phone)return;
      var ctx=context();if(!ctx.key)return setError('Откройте сохранённую карточку');
      var suppliedId=positiveId(data&&(data.contact_id||data.contactId));
      var promise=suppliedId?Promise.resolve({crm_contact_id:suppliedId,phone:phone,key:ctx.key}):resolveCard(ctx,phone);
      promise.then(function(c){startCall(c,phone);}).catch(function(){setError('Номер не принадлежит контакту карточки');});
    }
    function click(e) {
      var r=document.getElementById(ROOT);if(!r||!r.contains(e.target))return;
      var t=e.target.closest&&e.target.closest('button,input');if(!t)return;
      if(t.classList.contains('rats-start')){e.preventDefault();startCall(cardCache[context().key]);}
      if(t.classList.contains('rats-hangup')){e.preventDefault();hangup();}
      if(t.classList.contains('rats-answer')){e.preventDefault();answer();}
      if(t.classList.contains('rats-audio')){e.preventDefault();ensureAudio().play().then(function(){t.hidden=true;}).catch(function(){setError('Не удалось включить звук');});}
      if(t.classList.contains('rats-refresh')){e.preventDefault();delete cardCache[context().key];syncCard(true);}
      if(t.classList.contains('rats-mode-browser'))selectMode('browser');
      if(t.classList.contains('rats-mode-forward'))selectMode('forward');
    }
    function template() {
      return '<section id="'+ROOT+'" class="rats-widget" role="region" aria-label="Телефония RATS">' +
        '<header class="rats-head"><div class="rats-mark">R<span>▲</span>TS</div><div class="rats-head-sub">MULTICOM · ASTERISK</div></header>' +
        '<div class="rats-section"><div class="rats-label">Клиент</div><div class="rats-name">Контакт не выбран</div><div class="rats-phone">—</div></div>' +
        '<div class="rats-section rats-user-block" hidden><div class="rats-label">Менеджер</div><div class="rats-user"></div><div class="rats-label rats-label-small">Телефон переадресации</div><div class="rats-forward">—</div></div>' +
        '<div class="rats-section"><div class="rats-label">Куда подключить менеджера?</div><label class="rats-radio"><input type="radio" name="rats-mode" class="rats-mode-browser" checked> В браузере amoCRM</label>' +
        '<label class="rats-radio"><input type="radio" name="rats-mode" class="rats-mode-forward"> На мой телефон</label>' +
        '<p class="rats-hint rats-microphone">Потребуется микрофон и WebRTC-подключение к Asterisk.</p></div>' +
        '<div class="rats-actions">' +
        '<button class="rats-start rats-primary" type="button" disabled>Позвонить клиенту</button>' +
        '<button class="rats-answer rats-answer" type="button" hidden>Ответить</button>' +
        '<button class="rats-audio rats-primary" type="button" hidden>Включить звук</button>' +
        '<button class="rats-hangup rats-danger" type="button" disabled>Завершить разговор</button>' +
        '<button class="rats-refresh rats-text" type="button">Обновить контакт</button></div>' +
        '<div class="rats-call"><span class="rats-call-state">Нет разговора</span><small class="rats-call-id"></small></div>' +
        '<div class="rats-status" role="status" aria-live="polite"></div><div class="rats-footer">RATS · Remote Automatic Telephone System</div></section>';
    }
    function loadStyles() {
      var s=settings(),path=String(s.path||(self.params&&self.params.path)||'').replace(/\/+$/,'');
      if(!path&&s.widget_code)path='/widgets/'+encodeURIComponent(String(s.widget_code));
      if(!path)return;
      var href=path+'/style.css?v='+VERSION;
      if(document.querySelector('link[data-rats-style="'+href+'"]'))return;
      var link=document.createElement('link');link.rel='stylesheet';link.href=href;link.setAttribute('data-rats-style',href);document.head.appendChild(link);
    }
    self.callbacks={
      render:function(){alive=true;loadStyles();if(areaType(system().area)&&typeof self.render_template==='function'){
        try{self.render_template({caption:{class_name:'rats-widget-caption'},body:'',render:template()});}
        catch(e){console.warn('RATS render error',e);}
      }return true;},
      init:function(){alive=true;
        if(!phoneActionRegistered&&areaType(system().area)&&typeof self.add_action==='function'){
          try{self.add_action('phone',routePhoneClick);phoneActionRegistered=true;}catch(e){console.warn('RATS phone action',e);}
        }
        if(!timer)timer=setInterval(function(){if(document.getElementById(ROOT))syncCard(false);},1700);
        if(!wired){document.addEventListener('click',click,false);wired=true;}
        autoIdentify();
        setTimeout(function(){syncCard(false);draw();},0);
        return true;},
      bind_actions:function(){draw();return true;},
      settings:function(){return true;},onSave:function(){return true;},
      destroy:function(){if(callId||busy||requestId)return true;alive=false;
        if(timer)clearInterval(timer);timer=null;if(refreshTimer)clearInterval(refreshTimer);refreshTimer=null;
        if(wired){document.removeEventListener('click',click,false);wired=false;}
        if(sipSession){try{sipSession.terminate();}catch(e){}sipSession=null;}
        if(sipUA){try{sipUA.stop();}catch(e){}sipUA=null;sipRegistered=false;}
        if(remoteAudio){remoteAudio.srcObject=null;remoteAudio.remove();remoteAudio=null;}
        if(localStream){localStream.getTracks().forEach(function(t){t.stop();});localStream=null;}
        principal=null;callId=null;callingStatus(false);
        phoneActionRegistered=false;cardCache={};lastCardKey='';expectedSipToken=null;return true;}
    };
    return this;
  };
});
