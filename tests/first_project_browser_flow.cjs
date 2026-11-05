/* eslint-env node */
"use strict";
// Real offline application pipeline, using only bundled synthetic demo media.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');

module.exports = async function firstProjectFlow({root, temporary, baseUrl, headers, command, evaluate, keepPilot}) {
  const started = Date.now();
  const timings = {};
  let lastStep = started;
  const mark = name => {const now = Date.now(); timings[name] = (now-lastStep)/1000; lastStep=now; console.log(`Demo ${name}: ${timings[name]}s`);};
  const until = async (expression, timeout=20000) => {
    const end=Date.now()+timeout;
    while(Date.now()<end) {
      if(await evaluate(expression)) return;
      if(expression.includes('job_status')) {
        const failed=await evaluate("['rv_job_status','sync_job_status'].map(id=>document.getElementById(id)).find(el=>el?.dataset.state==='failed')?.textContent");
        if(failed) throw Error(`Demo job failed: ${failed}`);
      }
      await new Promise(resolve=>setTimeout(resolve,250));
    }
    throw Error(`Demo timeout: ${expression}\n`+await evaluate("document.querySelector('#log_box')?.textContent"));
  };
  const click = async selector => {
    await until("(async()=> !(await import('/static/stage1/core.js')).isBusy())()");
    await until(`!!document.querySelector(${JSON.stringify(selector)}) && !document.querySelector(${JSON.stringify(selector)}).disabled`);
    assert.equal(await evaluate(`!!document.querySelector(${JSON.stringify(selector)}) && !document.querySelector(${JSON.stringify(selector)}).disabled`),true,`enabled ${selector}`);
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  };
  const request = async route => {
    const response=await fetch(baseUrl+route,{headers});
    assert.equal(response.ok,true,`${route}: ${await response.clone().text()}`);
    const body=await response.json();
    return body.result || body;
  };
  const app=path.join(temporary,'app');
  await command('Page.enable');
  fs.cpSync(path.join(root,'examples/first-project'),path.join(app,'examples/first-project'),{recursive:true});
  fs.mkdirSync(path.join(app,'scripts'),{recursive:true});
  fs.copyFileSync(path.join(root,'scripts/create-demo'),path.join(app,'scripts/create-demo'));
  const created=spawnSync('bash',[path.join(app,'scripts/create-demo')],{encoding:'utf8'});
  assert.equal(created.status,0,created.stderr);
  const project=created.stdout.match(/demo_[A-Za-z0-9_]+/)[0];
  const query=`?project=${encodeURIComponent(project)}`;
  const expected=JSON.parse(fs.readFileSync(path.join(root,'examples/first-project/expected.json')));
  await command('Page.navigate',{url:baseUrl+'/'});
  await until("!!document.querySelector('main')");
  await command('Page.navigate',{url:`${baseUrl}/project/${project}`});
  await until("document.querySelector('#normalization_scenario_select')?.value.endsWith('scenario.txt')");
  assert.equal(await evaluate("document.querySelector('#normalization_use_llm').checked"),false,'no key defaults to rule normalization');
  await click('#btn_normalize');
  await until("document.querySelector('#normalization_status').dataset.state==='draft'");
  const normalized=await evaluate("(async()=> (await import('/static/stage1/state.js')).appState.normalization)()");
  assert.deepEqual(normalized.blocks.map(block=>block.type),expected.block_types);
  assert.ok(normalized.blocks.some(block=>block.type==='sync' && String(block.normalized_text || block.text || '').includes(expected.reference_quote)));
  await click('#btn_confirm_normalization');
  await until("document.querySelector('#normalization_status').dataset.state==='confirmed' || document.querySelector('#normalization_learning_dialog').open");
  if(await evaluate("document.querySelector('#normalization_learning_dialog').open")) await click('#btn_confirm_with_learning');
  await until("document.querySelector('#normalization_status').dataset.state==='confirmed'");
  mark('normalize_and_confirm');
  await click('[data-tab=voiceover]');
  await click('#voice_mode_recorded');
  await until("(async()=> (await import('/static/stage1/recorded_voiceover.js')).getRecordedState()?.recordings.length>0)()");
  // Inbox discovery starts matching automatically. Do not start a second job
  // just as its cached predecessor completes and publishes a new generation.
  const existingJob=await evaluate("(async()=> (await import('/static/stage1/recorded_voiceover.js')).getRecordedState().job)()");
  if(!['queued','running','cancelling','completed'].includes(existingJob?.status)) await click('#btn_rv_align');
  await until("document.querySelector('#rv_job_status').dataset.state==='completed'",240000);
  let recording=await evaluate("(async()=> (await import('/static/stage1/recorded_voiceover.js')).getRecordedState())()");
  assert.equal(recording.blocks.length,2);
  assert.ok(recording.blocks.every(block=>block.range && block.range.status!=='missing'),'real ASR found both ranges');
  // Confirm only the actual ASR proposals. No reference timings/results are injected.
  for(const block of recording.blocks) {
    assert.ok(block.range.source_out_sec>block.range.source_in_sec);
    await click(`.rv-row[data-uid="${block.block_uid}"] [data-rv-action=confirm]`);
    await until(`(async()=> (await import('/static/stage1/recorded_voiceover.js')).getRecordedState().blocks.find(b=>b.block_uid===${JSON.stringify(block.block_uid)}).range.status==='confirmed')()`);
  }
  const apply=evaluate("document.querySelector('#btn_rv_apply').click()");
  await new Promise(resolve=>setTimeout(resolve,250));
  await command('Page.handleJavaScriptDialog',{accept:true});
  await apply;
  await until("(async()=> (await import('/static/stage1/recorded_voiceover.js')).getRecordedState().published.is_current)()");
  recording=await evaluate("(async()=> (await import('/static/stage1/recorded_voiceover.js')).getRecordedState())()");
  mark('recorded_voiceover_asr_and_publish');
  await click('[data-tab=assembly]');
  await until("!document.querySelector('#btn_sync_align').disabled");
  await evaluate("document.querySelector('#sync_asr_engine').value='local'; document.querySelector('#sync_whisper_model').value='small'");
  await click('#btn_sync_align');
  await until("document.querySelector('#sync_job_status').dataset.state==='completed'",240000);
  let matches=await request('/stage2/sync-matches'+query);
  matches=matches.items || matches.matches || matches;
  assert.equal(matches.length,1);
  const match=matches[0];
  assert.ok(['auto_matched','needs_review','confirmed'].includes(match.status),JSON.stringify(match));
  assert.match(match.matched_text || match.candidate?.matched_text || '',/Сегодня мы открываем новый маршрут вдоль реки/i);
  if(match.status==='needs_review') {
    await click('[data-action=confirm-sync-match]');
    await until("document.querySelector('#sync_count_confirmed').textContent==='1'");
  }
  const speech=match.metadata || match;
  assert.ok(match.source_in_sec <= speech.speech_in_sec && match.source_out_sec >= speech.speech_out_sec,'source contains the recognized words');
  mark('sync_asr_and_check');
  await click('#btn_build_timeline');
  await until("!document.querySelector('#btn_export_xml').disabled");
  await evaluate("document.querySelector('#stage3_prepare_mode').value='complete'");
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:path.join(temporary,'downloads')});
  await click('#btn_export_xml');
  await until("document.querySelector('#stage3_build').textContent.includes('без пропусков')",180000);
  const status=await request('/stage3/status'+query);
  assert.equal(status.build.mode,'complete');
  assert.equal(status.build.is_current,true);
  const xmlResponse=await fetch(`${baseUrl}/stage3/export/xml${query}&build_id=${encodeURIComponent(status.build.build_id)}`,{headers});
  assert.equal(xmlResponse.ok,true);
  const xml=await xmlResponse.text();
  assert.match(xml,/<xmeml/);
  const artifacts=keepPilot ? path.join(root,'artifacts/first-user') : path.join(temporary,'results');fs.mkdirSync(artifacts,{recursive:true});
  fs.writeFileSync(path.join(artifacts,'demo-timeline.xml'),xml);
  const xmlCheck=spawnSync(process.env.BROWSER_TEST_PYTHON || path.join(root,'.venv/bin/python'),[path.join(root,'tests/first_project_xml_check.py'),path.join(artifacts,'demo-timeline.xml')],{encoding:'utf8'});
  assert.equal(xmlCheck.status,0,xmlCheck.stderr);
  fs.writeFileSync(path.join(artifacts,'demo-xml-check.json'),xmlCheck.stdout);
  mark('timeline_and_xml');
  await command('Page.reload');
  await until("document.querySelector('#normalization_status')?.dataset.state==='confirmed' && document.querySelector('#stage3_build')?.textContent.includes('без пропусков')");
  assert.equal((await request('/stage3/status'+query)).build.is_current,true);
  // A real input change invalidates the build across a subsequent reload.
  await click('[data-tab=normalization]');
  await evaluate("const field=document.querySelector('#normalization_blocks [data-field=normalized_text]');field.value+=' Сегодня.';field.dispatchEvent(new Event('input',{bubbles:true}))");
  await click('#btn_save_normalization');
  await until("document.querySelector('#normalization_status').dataset.state==='draft' && document.querySelector('#btn_save_normalization').disabled");
  await command('Page.reload');
  await until("document.querySelector('#stage3_build')?.textContent.includes('устарела')");
  const stale=await request('/stage3/status'+query);
  assert.equal(stale.build.is_current,false);
  mark('reload_and_staleness');
  const report={date:new Date().toISOString(),project,mode:'real offline application pipeline; cached small CPU int8; no paid keys',timings,total_seconds:(Date.now()-started)/1000,blocks:normalized.blocks.map(({type,text,normalized_text,speaker})=>({type,text:normalized_text || text,speaker})),recorded_ranges:recording?.blocks?.map(({block_uid,range})=>({block_uid,range})),sync_match:match,build:status.build,reload_current:true,stale_after_scenario_edit:!stale.build.is_current,premiere_import:'NOT VERIFIED',human_listening:'NOT VERIFIED',temporary_media_root:app};
  fs.writeFileSync(path.join(artifacts,'demo-browser-pilot.json'),JSON.stringify(report,null,2)+'\n');
  console.log(`Real offline demo passed in ${report.total_seconds}s: ready recording → ASR → confirmation → complete XML → reload → stale input`);
};
