// Browser checks for observer role setup using Chromium DevTools Protocol.
import {createServer} from 'node:http';
import {readFile, mkdtemp, rm} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';

const root = resolve('frontend');
const server = createServer(async (req, res) => {
  const path = resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
  if (!path.startsWith(root + '/')) {res.writeHead(404); res.end(); return;}
  try {
    res.setHeader('Content-Type', path.endsWith('.css') ? 'text/css' : path.endsWith('.js') ? 'text/javascript' : 'text/html');
    res.end(await readFile(path));
  } catch {res.writeHead(404); res.end();}
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const profile = await mkdtemp(join(tmpdir(), 'observer-setup-'));
const chrome = spawn(process.env.CHROMIUM_PATH || 'chromium', [
  '--headless', '--no-sandbox', '--disable-gpu', '--remote-debugging-port=0',
  '--no-first-run', '--no-default-browser-check', '--user-data-dir=' + profile, 'about:blank',
]);
let socket;
try {
  const endpoint = await new Promise((resolveEndpoint, reject) => {
    const timer = setTimeout(() => reject(new Error('Chromium did not start')), 10000);
    chrome.on('error', reject);
    chrome.stderr.on('data', chunk => {
      const match = String(chunk).match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) {clearTimeout(timer); resolveEndpoint(match[1]);}
    });
  });
  socket = new WebSocket(endpoint);
  await new Promise((done, reject) => {socket.addEventListener('open', done, {once:true}); socket.addEventListener('error', reject, {once:true});});
  let serial = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const reply = JSON.parse(String(event.data));
    if (reply.id && pending.has(reply.id)) {pending.get(reply.id)(reply); pending.delete(reply.id);}
  });
  const send = async (method, params = {}, sessionId) => {
    const id = ++serial;
    const reply = new Promise(done => pending.set(id, done));
    socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    const result = await reply;
    if (result.error) throw new Error(JSON.stringify(result.error));
    return result.result;
  };
  const {targetInfos} = await send('Target.getTargets');
  const {sessionId} = await send('Target.attachToTarget', {targetId: targetInfos.find(info => info.type === 'page').targetId, flatten:true});
  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);
  await send('Page.addScriptToEvaluateOnNewDocument', {source: `
    window.testState = {saved:null, launched:false, reportAvailable:false, resetCount:0};
    window.fetch = async (url, options = {}) => {
      const json = value => new Response(JSON.stringify(value), {status:200, headers:{'Content-Type':'application/json'}});
      if (url === '/api/health') return json({status:'ready'});
      if (url === '/api/roles') return json({roles:[{id:'software-engineer',name:'Software Engineer',description:'Engineering interview'}],modes:{adaptive:{label:'Fully Adaptive',adaptive_turn_budget:null}}});
      if (url === '/api/session/config' && options.method === 'POST') {testState.saved = JSON.parse(options.body); return json({...testState.saved,role_name:testState.saved.role_name || 'Software Engineer'});}
      if (url === '/api/session/config') return json({launched:testState.launched,role_id:null});
      if (url === '/api/session/launch') {testState.launched = true; return json({status:'ready'});}
      if (url === '/api/session/reset') {testState.resetCount++; testState.saved = null; testState.launched = false; testState.reportAvailable = false; return json({role_name:null});}
      if (url === '/api/log') return json({session:{role_name:testState.saved?.role_name || 'Software Engineer',mode:'adaptive'},planner_state:{},skill_graph:{skills:{},edges:[]},report_available:testState.reportAvailable});
      throw new Error('Unexpected request: ' + url);
    };
  `}, sessionId);
  await send('Page.navigate', {url:'http://127.0.0.1:' + server.address().port + '/observer.html'}, sessionId);
  const evaluate = async expression => {
    const result = await send('Runtime.evaluate', {expression, awaitPromise:true, returnByValue:true}, sessionId);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  };
  for (let i = 0; i < 100 && !(await evaluate("document.querySelector('[data-role-id=custom]') !== null")); i++) await new Promise(done => setTimeout(done, 30));
  assert.equal(await evaluate("document.querySelectorAll('.role-option').length"), 2);
  assert.equal(await evaluate("document.body.textContent.includes('29 categories found') || document.body.textContent.includes('Choose a category to begin') || !!document.querySelector('.library-toolbar,.drawer-hint')"), false);
  assert.equal(await evaluate("getComputedStyle(document.querySelector('.welcome'),'::after').content"), 'none');
  const state = await evaluate(`(async () => {
    const custom = document.querySelector('[data-role-id=custom]');
    const preset = document.querySelector('[data-role-id=software-engineer]');
    const name = document.getElementById('customRoleName');
    const field = document.getElementById('customRoleField');
    const launch = document.getElementById('launchBtn');
    custom.click();
    const initially = {visible:!field.hidden, focused:document.activeElement===name, disabled:launch.disabled};
    name.value='   '; name.dispatchEvent(new Event('input'));
    const blankDisabled=launch.disabled;
    name.value='  Cybersecurity Analyst  '; name.dispatchEvent(new Event('input'));
    const named={enabled:!launch.disabled, review:document.getElementById('reviewTitle').textContent};
    name.value='x'.repeat(81); name.dispatchEvent(new Event('input'));
    const longDisabled=launch.disabled;
    preset.click();
    const presetState={hidden:field.hidden, enabled:!launch.disabled, review:document.getElementById('reviewTitle').textContent};
    custom.click(); name.value='  Cybersecurity Analyst  '; name.dispatchEvent(new Event('input'));
    launch.click();
    for(let i=0;i<100 && !testState.launched;i++) await new Promise(done=>setTimeout(done,10));
    await new Promise(done=>setTimeout(done,20));
    const launched={payload:testState.saved, role:document.getElementById('sessionRole').textContent, dashboard:!document.getElementById('dashboard').hidden};
    const reportLink=document.getElementById('viewReportBtn');
    const downloadLink=document.getElementById('downloadReportBtn');
    const reportBefore={view:reportLink.hidden,download:downloadLink.hidden};
    testState.reportAvailable=true; await refresh();
    const reportAfter={visible:!reportLink.hidden,href:reportLink.getAttribute('href'),target:reportLink.target,downloadVisible:!downloadLink.hidden,downloadHref:downloadLink.getAttribute('href')};
    document.getElementById('resetBtn').click();
    for(let i=0;i<100 && (!testState.resetCount || !field.hidden);i++) await new Promise(done=>setTimeout(done,10));
    return {initially,blankDisabled,named,longDisabled,presetState,launched,reportBefore,reportAfter,reset:{cleared:name.value==='',hidden:field.hidden,disabled:launch.disabled,label:launch.textContent.trim(),reportHidden:reportLink.hidden,downloadHidden:downloadLink.hidden}};
  })()`);
  assert.deepEqual(state.initially, {visible:true, focused:true, disabled:true});
  assert.equal(state.blankDisabled, true);
  assert.deepEqual(state.named, {enabled:true, review:'Cybersecurity Analyst interview'});
  assert.equal(state.longDisabled, true);
  assert.deepEqual(state.presetState, {hidden:true, enabled:true, review:'Software Engineer interview'});
  assert.equal(state.launched.payload.role_id, 'custom');
  assert.equal(state.launched.payload.role_name, 'Cybersecurity Analyst');
  assert.equal(state.launched.role, 'Cybersecurity Analyst');
  assert.equal(state.launched.dashboard, true);
  assert.deepEqual(state.reportBefore, {view:true,download:true});
  assert.deepEqual(state.reportAfter, {visible:true,href:'/api/session/report',target:'_blank',downloadVisible:true,downloadHref:'/api/session/report?download=1'});
  assert.deepEqual(state.reset, {cleared:true, hidden:true, disabled:true, label:'Launch interview ↗',reportHidden:true,downloadHidden:true});
  await send('Emulation.setDeviceMetricsOverride', {width:1366,height:900,deviceScaleFactor:1,mobile:false}, sessionId);
  const desktopGraph = await evaluate(`(() => {
    document.getElementById('dashboard').hidden=false;
    document.getElementById('onboarding').hidden=true;
    const wrap=document.querySelector('.graph-wrap');
    const panel=document.querySelector('.graph-panel');
    const makeGraph=count=>({skills:Object.fromEntries(Array.from({length:count},(_,i)=>['skill-'+i,{skill_id:'skill-'+i,name:'Skill '+i,mastery:0.5,confidence:0.5}])),edges:[]});
    drawGraph(makeGraph(3));
    const smallFits=wrap.scrollHeight<=wrap.clientHeight+1 && wrap.scrollWidth<=wrap.clientWidth+1;
    drawGraph(makeGraph(30));
    const grows=document.getElementById('graph').viewBox.baseVal.height>390;
    const scrolls=wrap.scrollHeight>wrap.clientHeight;
    const headingTop=panel.querySelector('.panel-head').getBoundingClientRect().top;
    wrap.scrollTop=wrap.scrollHeight;
    const last=[...document.querySelectorAll('#graph .node-value')].at(-1).getBoundingClientRect();
    const viewport=wrap.getBoundingClientRect();
    return {smallFits,grows,scrolls,lastVisible:last.top>=viewport.top && last.bottom<=viewport.bottom,headingFixed:panel.querySelector('.panel-head').getBoundingClientRect().top===headingTop,insidePanel:viewport.bottom<=panel.getBoundingClientRect().bottom+1};
  })()`);
  assert.deepEqual(desktopGraph,{smallFits:true,grows:true,scrolls:true,lastVisible:true,headingFixed:true,insidePanel:true});
  console.log('PASS desktop skill graph scrolls inside its panel');
  await send('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true}, sessionId);
  const mobileGraph = await evaluate(`(() => {
    const wrap=document.querySelector('.graph-wrap');
    drawGraph({skills:{only:{skill_id:'only',name:'Only skill'}},edges:[]});
    const smallFits=wrap.scrollHeight<=wrap.clientHeight+1 && wrap.scrollWidth<=wrap.clientWidth+1;
    const skills=Object.fromEntries(Array.from({length:30},(_,i)=>['skill-'+i,{skill_id:'skill-'+i,name:'Skill '+i}]));
    drawGraph({skills,edges:[]});
    const scrollsBoth=wrap.scrollHeight>wrap.clientHeight && wrap.scrollWidth>wrap.clientWidth;
    wrap.scrollTop=wrap.scrollHeight;wrap.scrollLeft=wrap.scrollWidth;
    const last=[...document.querySelectorAll('#graph .node-value')].at(-1).getBoundingClientRect();
    const viewport=wrap.getBoundingClientRect();
    return {smallFits,scrollsBoth,lastVisible:last.top>=viewport.top && last.bottom<=viewport.bottom && last.right<=viewport.right,documentFits:document.documentElement.scrollWidth<=window.innerWidth};
  })()`);
  assert.deepEqual(mobileGraph,{smallFits:true,scrollsBoth:true,lastVisible:true,documentFits:true});
  console.log('PASS mobile skill graph scrolls in both directions without page overflow');
  const mobile = await evaluate(`(() => {
    showOnboarding();
    document.querySelector('[data-role-id=custom]').click();
    const field=document.getElementById('customRoleField');
    return {overflow:document.documentElement.scrollWidth>window.innerWidth, visible:!field.hidden, width:Math.round(field.getBoundingClientRect().width)};
  })()`);
  assert.equal(mobile.overflow, false);
  assert.equal(mobile.visible, true);
  assert(mobile.width > 200);
  console.log('PASS observer setup: roles, report link, reset, mobile layout');
} finally {
  socket?.close();
  chrome.kill('SIGTERM');
  await new Promise(done => {
    if (chrome.exitCode !== null || chrome.signalCode !== null) return done();
    const timer = setTimeout(() => chrome.kill('SIGKILL'), 5000);
    chrome.once('exit', () => {clearTimeout(timer); done();});
  });
  server.closeAllConnections();
  await new Promise(done => server.close(done));
  await rm(profile, {recursive:true, force:true, maxRetries:10, retryDelay:100});
}
