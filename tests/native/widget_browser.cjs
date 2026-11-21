// Native fixture only: no production account, provider credentials or telephone numbers.
const fs = require('fs');
const https = require('https');
const http = require('http');
const net = require('net');
const path = require('path');
const {chromium} = require('playwright');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const server = https.createServer({key:fs.readFileSync(path.join(cfg.folder,'key.pem')),cert:fs.readFileSync(path.join(cfg.folder,'cert.pem'))},(req,res)=>{
  const upstream = http.request({host:'127.0.0.1',port:cfg.apiPort,path:req.url,method:req.method,headers:req.headers},answer=>{
    res.writeHead(answer.statusCode,answer.headers);answer.pipe(res);
  });
  upstream.on('error',()=>{res.writeHead(502);res.end();});req.pipe(upstream);
});
server.on('upgrade',(req,socket,head)=>{
  const upstream = net.connect(cfg.wsPort,'127.0.0.1',()=>{
    upstream.write(req.method+' /ws HTTP/1.1\r\n'+Object.entries(req.headers).map(([k,v])=>k+': '+v).join('\r\n')+'\r\n\r\n');
    if(head.length)upstream.write(head);socket.pipe(upstream);upstream.pipe(socket);
  });
  upstream.on('error',()=>socket.destroy());socket.on('error',()=>upstream.destroy());socket.on('close',()=>upstream.destroy());
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const port = server.address().port;
  fs.writeFileSync(path.join(cfg.folder,'gateway.json'),JSON.stringify({port}));
  while(!fs.existsSync(path.join(cfg.folder,'token.txt')))await new Promise(r=>setTimeout(r,20));
  const token=fs.readFileSync(path.join(cfg.folder,'token.txt'),'utf8');
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||(fs.existsSync('/usr/bin/chromium')?'/usr/bin/chromium':undefined),headless:true,args:['--no-sandbox','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--ignore-certificate-errors','--disable-features=LocalNetworkAccessChecks','--autoplay-policy=no-user-gesture-required']});
  try{
    const context=await browser.newContext({ignoreHTTPSErrors:true,permissions:['microphone']});
    const page=await context.newPage();
    page.on('console',m=>console.log('browser:',m.type(),m.text()));
    page.on('pageerror',e=>console.log('pageerror:',e.message));
    page.on('response',r=>{if(r.url().includes('/widget/v2/'))console.log('api',r.status(),new URL(r.url()).pathname);});
    await page.route('https://omkr.amocrm.ru/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname.startsWith('/api/v4/'))return route.fulfill({json:url.pathname.includes('/contacts/')?{id:123,name:'Native customer',custom_fields_values:[{field_code:'PHONE',values:[{value:'+79990000101'}]}]}:{id:101,_embedded:{contacts:[{id:123}]}}});
      return route.fulfill({contentType:'text/html',body:'<!doctype html><html><body><main></main></body></html>'});
    });
    await page.goto('https://omkr.amocrm.ru/leads/detail/101');
    await page.evaluate(({port,token})=>{
      function request(options,signed){
        const headers=Object.assign({},options.headers||{});if(signed)headers['X-Auth-Token']=token;
        if(options.contentType)headers['Content-Type']=options.contentType;
        const p=fetch(options.url,{method:options.type||'GET',headers,body:options.data}).then(async response=>{
          const json=await response.json();if(!response.ok)throw{status:response.status,responseJSON:json};return json;
        });
        const deferred={done(fn){p.then(fn,()=>{});return deferred;},fail(fn){p.catch(fn);return deferred;}};return deferred;
      }
      window.define=(_dependencies,factory)=>{window.Widget=factory({ajax:o=>request(o,false)});};
      window.install=()=>{
        const widget=new window.Widget();
        widget.get_settings=()=>({ratsUrl:'https://localhost:'+port});
        widget.system=()=>({area:'lcard',subdomain:'omkr',amouser_id:7654321});
        widget.$authorizedAjax=o=>request(o,true);
        widget.render_template=data=>{document.querySelector('main').innerHTML=data.render;};
        widget.add_action=()=>{};widget.callbacks.render();widget.callbacks.init();window.widget=widget;
      };
      const Original=window.RTCPeerConnection;window.peers=[];
      // Force the real relay path on every runner; production keeps the normal ICE policy.
      window.RTCPeerConnection=function(...args){const pc=new Original({...args[0],iceTransportPolicy:'relay'},args[1]);window.peers.push(pc);pc.addEventListener('connectionstatechange',()=>console.log('PC',pc.connectionState,pc.iceConnectionState));return pc;};
      window.RTCPeerConnection.prototype=Original.prototype;
    },{port,token});
    await page.addScriptTag({content:fs.readFileSync(path.join(cfg.root,'amo-widget/script.js'),'utf8')});
    await page.evaluate(()=>window.install());
    await page.locator('.rats-name').filter({hasText:'Native customer'}).waitFor();
    await page.locator('.rats-mode-browser').click();
    await page.waitForFunction(()=>document.querySelector('.rats-status').textContent.includes('зарегистрирован'),null,{timeout:15000});
    await page.locator('.rats-start').click();
    await page.waitForFunction(()=>document.querySelector('.rats-call-state').textContent==='Разговор',null,{timeout:25000});
    await page.waitForTimeout(1000);
    const result=await page.evaluate(async()=>{
      const rows=[];let relay=false;
      for(const pc of window.peers){
        const report=await pc.getStats();
        for(const stat of report.values()){
          if(['inbound-rtp','outbound-rtp'].includes(stat.type))rows.push({type:stat.type,received:stat.packetsReceived||0,sent:stat.packetsSent||0});
          if(stat.type==='transport'&&stat.selectedCandidatePairId){const pair=report.get(stat.selectedCandidatePairId);const candidate=pair&&report.get(pair.localCandidateId);if(candidate&&candidate.candidateType==='relay')relay=true;}
        }
      }
      return {rows,relay};
    });
    const stats=result.rows;
    console.log('media statistics',JSON.stringify(stats));
    if(!result.relay)throw Error('Selected ICE candidate must use the TURN relay');
    if(!stats.some(s=>s.type==='inbound-rtp'&&s.received>5)||!stats.some(s=>s.type==='outbound-rtp'&&s.sent>5))throw Error('Bidirectional browser SRTP not established');
    await page.locator('.rats-hangup').click();
    await page.waitForFunction(()=>document.querySelector('.rats-call-state').textContent==='Завершён',null,{timeout:10000});
    console.log('PASS: shipped widget + signed API + WSS registration + real WebRTC/SRTP + bridge + hangup');
  }finally{await browser.close();server.close();}
})().then(()=>process.exit(0)).catch(error=>{console.error(error);server.close();process.exit(1);});
