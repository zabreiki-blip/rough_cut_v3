/* eslint-env node */
"use strict";
// Real Chrome + actual CSS/modules and isolated Flask app. Node >=22 required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn) {
  for (let i = 0; i < 150; i++) { try { const value = await fn(); if (value) return value; } catch (_) {} await delay(100); }
  throw Error('Timed out waiting for browser/server');
}
async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'rc-browser-smoke-'));
  const children = [];
  let socket;
  try {
    const python = process.env.BROWSER_TEST_PYTHON || path.join(root, '.venv/bin/python');
    const server = spawn(python, [path.join(__dirname, 'first_user_browser_server.py'), root, path.join(temporary, 'app'), '0'], {stdio: ['ignore','pipe','pipe']});
    children.push(server);
    let serverError = ''; server.stderr.on('data', data => { serverError += data; });
    let serverOutput = ''; server.stdout.on('data', data => { serverOutput += data; });
    const port = await until(() => {const match = serverOutput.match(/\{"port": (\d+)\}/); return match && match[1];});
    const baseUrl = `http://127.0.0.1:${port}`;
    const headers = {Authorization:'Bearer browser-smoke-only', 'Content-Type':'application/json'};
    try { await until(async () => (await fetch(`${baseUrl}/projects`, {headers})).ok); }
    catch(error) { throw Error(`${error.message}: ${serverError}`); }
    const create = await fetch(`${baseUrl}/projects`, {method:'POST',headers,body:JSON.stringify({project:'browser_fixture'})});
    assert.equal(create.ok, true);
    const chrome = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
    const browser = spawn(chrome, ['--headless=new','--no-first-run','--no-default-browser-check','--remote-debugging-port=0',`--user-data-dir=${temporary}/chrome`,'about:blank'], {stdio:'ignore'});
    children.push(browser);
    const portFile = path.join(temporary,'chrome','DevToolsActivePort');
    const debugPort = await until(() => fs.existsSync(portFile) && fs.readFileSync(portFile,'utf8').split('\n')[0]);
    const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
    socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve,reject) => {socket.onopen=resolve;socket.onerror=reject;});
    let sequence = 0; const pending = new Map();
    socket.onmessage = event => { const data=JSON.parse(event.data); if(data.id && pending.has(data.id)) {const {resolve,reject}=pending.get(data.id);pending.delete(data.id);data.error?reject(Error(JSON.stringify(data.error))):resolve(data.result);} };
    const command = (method,params={}) => new Promise((resolve,reject)=> {const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
    const evaluate = async expression => {
      const result=await command('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
      if(result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const artifacts = path.join(root,'artifacts','first-user');
    fs.mkdirSync(artifacts,{recursive:true});
    const captureGuidance = async phase => {
      for (const [width,height] of [[1440,900],[1280,800]]) {
        await command('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
        await evaluate('window.scrollTo(0,0)');
        await delay(150);
        const geometry = await evaluate(`(()=>{const el=document.querySelector('#project_next_action');const r=el.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:innerHeight,width:innerWidth,uncovered:el.contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2))};})()`);
        assert.equal(geometry.top>=0 && geometry.bottom<=height && geometry.left>=0 && geometry.right<=width,true,`next action above fold at ${width}×${height}: ${JSON.stringify(geometry)}`);
        assert.equal(geometry.uncovered,true,'diagnostics must not cover the next action');
        const shot=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
        fs.writeFileSync(path.join(artifacts,`task5-${phase}-${width}x${height}.png`),Buffer.from(shot.data,'base64'));
      }
    };
    await command('Network.enable');
    await command('Network.setExtraHTTPHeaders',{headers:{Authorization:headers.Authorization}});
    await command('Page.navigate',{url:`${baseUrl}/project/browser_fixture`});
    await until(()=>evaluate("!!document.querySelector('#btn_tts_cancel') && !!document.querySelector('#log_box')?.textContent.includes('UI ready')"));
    await until(()=>evaluate("document.querySelector('#readiness_summary_badge').dataset.state !== 'empty'"));
    assert.equal(await evaluate("document.querySelector('#normalization_use_llm').checked"),false,'cloud LLM defaults off');
    await captureGuidance('start');
    // Actual folder → discovery → explicit normalization job → confirmation.
    await until(()=>evaluate("document.querySelector('#normalization_scenario_summary').textContent.includes('ЗАГРУЗИТЬ')"));
    assert.equal(await evaluate("document.querySelector('#normalization_input_path').value"),'','empty project has no fabricated source');
    const scenarioDirectory = path.join(temporary,'app','projects','browser_fixture','ЗАГРУЗИТЬ','Сценарии');
    fs.mkdirSync(scenarioDirectory,{recursive:true});
    const scenarioFile = path.join(scenarioDirectory,'Новый выпуск.txt');
    fs.writeFileSync(scenarioFile,'ЗК: Первый закадровый блок.\n');
    await evaluate("document.querySelector('#btn_refresh_scenarios').click()");
    await until(()=>evaluate("document.querySelector('#normalization_scenario_select').value.includes('Новый выпуск.txt')")).catch(async error=>{throw Error(error.message + ': ' + JSON.stringify(await evaluate("({summary:document.querySelector('#normalization_scenario_summary').textContent, options:document.querySelector('#normalization_scenario_select').innerHTML, log:document.querySelector('#log_box').textContent})")));});
    assert.equal(await evaluate("document.querySelector('#normalization_blocks').children.length"),0,'selection does not normalize');
    await evaluate("document.querySelector('#normalization_use_llm').checked=false; document.querySelector('#btn_normalize').click()");
    await until(()=>evaluate("document.querySelector('#normalization_status').dataset.state === 'draft' && !document.querySelector('#btn_confirm_normalization').disabled"));
    await evaluate("document.querySelector('#btn_confirm_normalization').click()");
    await until(()=>evaluate("document.querySelector('#normalization_status').dataset.state === 'confirmed' || document.querySelector('#normalization_learning_dialog').open"));
    await evaluate("if(document.querySelector('#normalization_learning_dialog').open) document.querySelector('#btn_confirm_with_learning').click()");
    await until(()=>evaluate("document.querySelector('#normalization_status').dataset.state === 'confirmed'"));
    assert.match(await evaluate("document.querySelector('#project_next_text').textContent"),/Сценарий подтверждён/);
    await captureGuidance('confirmed');
    await command('Page.reload');
    await until(()=>evaluate("document.querySelector('#normalization_scenario_select')?.value.includes('Новый выпуск.txt') && document.querySelector('#normalization_status').dataset.state === 'confirmed'"));
    await until(()=>evaluate("(async()=> !!(await import('/static/stage1/state.js')).appState.normalization)()"));
    const preservedRevision = await evaluate("(async()=> (await import('/static/stage1/state.js')).appState.normalization.revision)()");
    fs.unlinkSync(scenarioFile);
    await evaluate("document.querySelector('#btn_refresh_scenarios').click()");
    await until(()=>evaluate("document.querySelector('#normalization_scenario_select').value === '' && document.querySelector('#normalization_scenario_summary').textContent.includes('исчез')"));
    assert.equal(await evaluate("(async()=> (await import('/static/stage1/state.js')).appState.normalization.revision)()"),preservedRevision,'missing file preserves normalized document');
    await evaluate("document.querySelector('[data-tab=voiceover]').click()");
    for(const status of ['idle','queued','running','cancelling','completed','failed','cancelled','interrupted']) {
      const visible = await evaluate(`(async()=>{const {live}=await import('/static/stage1/state.js'); const {renderLive}=await import('/static/stage1/tts.js'); live.status=${JSON.stringify(status)};renderLive();const button=document.querySelector('#btn_tts_cancel');return getComputedStyle(button).display !== 'none' && button.getClientRects().length>0;})()`);
      assert.equal(visible,['queued','running','cancelling'].includes(status),`TTS stop visibility in ${status}`);
    }
    assert.equal(await evaluate("Array.from(document.querySelectorAll('.workspace-panel[hidden],dialog:not([open])')).every(el=>el.getClientRects().length===0)"),true,'hidden tabs/dialogs');
    const lifecycleFile = path.join(temporary, 'app', 'browser-lifecycle.json');
    const stage3Fixture = {
      stage3_status:{processing:{status:'completed'},build:{build_id:'old-browser-build',mode:'complete',is_current:false},assembly:{level:'complete_ready',blockers:[]}},
      stage3_start:{job_id:'browser-stage3',kind:'prepare',status:'queued'},
      stage3_job:{id:'browser-stage3',kind:'prepare',status:'queued'},
    };
    fs.writeFileSync(lifecycleFile,JSON.stringify(stage3Fixture));
    await evaluate(`(async()=>{const {appState}=await import('/static/stage1/state.js');const core=await import('/static/stage1/core.js');core.openTab('assembly');appState.normalization={status:'confirmed',blocks:[{uid:'z1',type:'voiceover'}]};appState.normalizationEligible=true;appState.timelineReady=true;appState.currentTtsJobStatus='idle';core.updateActionAvailability();await (await import('/static/stage1/stage3.js')).refreshStage3Status();document.querySelector('#stage3_prepare_mode').value='complete';document.querySelector('#btn_export_xml').click();})()`);
    await until(()=>evaluate("document.querySelector('#stage3_media_status').textContent === 'в очереди'"));
    assert.match(await evaluate("document.querySelector('#stage3_processing').textContent"),/в очереди/,'Stage3 processing follows queued job');
    assert.match(await evaluate("document.querySelector('#stage3_build').textContent"),/old-browser-build.*устарела/,'old build stays independently stale');
    stage3Fixture.stage3_job.status='running';
    stage3Fixture.stage3_status.processing.status='running';
    fs.writeFileSync(lifecycleFile,JSON.stringify(stage3Fixture));
    await until(()=>evaluate("document.querySelector('#stage3_media_status').textContent === 'сборка…'"));
    assert.match(await evaluate("document.querySelector('#stage3_processing').textContent"),/выполняется/,'Stage3 processing follows running job');
    stage3Fixture.stage3_job={...stage3Fixture.stage3_job,status:'completed',result:{source_mapping:[],mapped_source_ids:[],placeholder_segments:0}};
    stage3Fixture.stage3_status.processing.status='completed';
    stage3Fixture.stage3_status.build={build_id:null};
    fs.writeFileSync(lifecycleFile,JSON.stringify(stage3Fixture));
    await until(()=>evaluate("document.querySelector('#stage3_media_status').textContent === 'экспорт завершён'"));
    await until(()=>evaluate("document.querySelector('#stage3_processing').textContent === 'завершена'"));
    assert.match(await evaluate("document.querySelector('#stage3_media_message').textContent"),/Экспортированный XML/,'legacy export is not a confirmed build');
    assert.equal(await evaluate("document.querySelector('#premiere_import_help').hidden"),false,'XML import instruction persists after status refresh');
    await captureGuidance('xml');
    fs.writeFileSync(lifecycleFile,JSON.stringify({tts_start:true}));
    await evaluate(`(async()=>{const {appState}=await import('/static/stage1/state.js');const core=await import('/static/stage1/core.js');core.openTab('voiceover');appState.currentTtsJobStatus='idle';core.updateActionAvailability();document.querySelector('#btn_tts').click();})()`);
    await until(()=>evaluate("document.querySelector('#log_box').textContent.includes('ERROR: start rejected by guard')"));
    assert.equal(await evaluate("document.querySelector('#live_job_status').textContent"),'failed','rejected TTS click exits queued');
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#btn_tts_cancel')).display"),'none','rejected TTS has no stop');
    fs.rmSync(lifecycleFile);
    const jobFile = path.join(temporary, 'app', 'browser-job.json');
    const job = {id:'browser-job',status:'running',total:2,done:0};
    fs.writeFileSync(jobFile, JSON.stringify(job));
    await command('Page.reload');
    await until(()=>evaluate("document.querySelector('#live_job_status')?.textContent === 'running'"));
    await evaluate("document.querySelector('[data-tab=voiceover]').click()");
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#btn_tts_cancel')).display !== 'none'"),true,'restored active job has stop');
    fs.writeFileSync(jobFile, JSON.stringify({...job,status:'completed',done:2}));
    await until(()=>evaluate("document.querySelector('#live_job_status')?.textContent === 'completed'"));
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#btn_tts_cancel')).display"),'none','polled completion hides stop');
    await command('Page.reload');
    await until(()=>evaluate("document.querySelector('#live_job_status')?.textContent === 'completed'"));
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#btn_tts_cancel')).display"),'none','completed stop stays hidden after reload');
    console.log('Chrome smoke passed: folder picker → normalization → confirmation/reload/disappearance; Stage3 queued → running → completed; rejected TTS click; TTS visibility/polling/reload; hidden tabs/dialogs');
  } finally {
    if(socket) socket.close();
    await Promise.all(children.reverse().map(child=>new Promise(resolve=> { if(child.exitCode!==null) return resolve();child.once('exit',resolve);child.kill('SIGTERM');setTimeout(()=>{child.kill('SIGKILL');resolve();},2000).unref(); })));
    fs.rmSync(temporary,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
