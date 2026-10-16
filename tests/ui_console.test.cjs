/* Optional browser integration check: NODE_PATH must include Playwright.
 * Only this isolated test injects a mock bridge. Production assets have no mock.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname,'../pages/console');
const schema = JSON.parse(fs.readFileSync(path.resolve(__dirname,'../_conf_schema.json'),'utf8'));
const artifacts = path.resolve(__dirname,'artifacts');
fs.mkdirSync(artifacts,{recursive:true});
const server = http.createServer((req,res) => {
  const target = path.resolve(root,'.' + decodeURIComponent(new URL(req.url,'http://localhost').pathname));
  const requested = target === root ? path.join(target,'index.html') : target;
  if (!requested.startsWith(root + path.sep) || !fs.existsSync(requested)) { res.writeHead(404); res.end(); return; }
  res.setHeader('Content-Type', requested.endsWith('.html') ? 'text/html;charset=utf-8' : requested.endsWith('.css') ? 'text/css;charset=utf-8' : 'text/javascript;charset=utf-8');
  res.end(fs.readFileSync(requested));
});

function mockBridge(input) {
  const schema = input.schema || input, bridgeName = input.bridgeName || 'AstrBotPluginView';
  const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4SMAAAAASUVORK5CYII=';
  // Persist only this mock backend's public settings to exercise a real reload.
  // The production console does not persist backend settings or credentials.
  const savedSettings = sessionStorage.getItem('tieba-lens-test-settings');
  let settings = savedSettings ? JSON.parse(savedSettings) : {enabled:true,theme:'denia',font_override:'lxgw',render_backend:'auto',browser_engine:'headless',single_image:true,show_source:true,cookie_set:false,group_overrides:{'998877':{theme:'miku',font_override:'lxgw',single_image:false,show_source:false,retired_setting:'legacy'}}};
  window.__renderer = {backend:'auto',engine:'headless',active:'service',service:{available:true,configured:true},local:{available:false,browser:''},install:{status:'idle',engine:'headless',stage:'',error:''},engines:[{id:'headless',name:'精简 Chromium',installed:false},{id:'chromium',name:'完整 Chromium',installed:false}]};
  const rendererSnapshot = () => JSON.parse(JSON.stringify({...window.__renderer,backend:settings.render_backend || 'auto',engine:settings.browser_engine || 'headless'}));
  const emptyFont = {uploaded:false,name:'',size:0,sha256:'',max_bytes:33554432,formats:['ttf','otf','woff','woff2']};
  let font = JSON.parse(sessionStorage.getItem('tieba-lens-test-font') || 'null') || {...emptyFont};
  const persistFont = () => {
    sessionStorage.setItem('tieba-lens-test-font',JSON.stringify(font));
    sessionStorage.setItem('tieba-lens-test-settings',JSON.stringify(settings));
  };
  const fontUploads = new Map();
  let fontUploadSequence = 0;
  const jobs = {
    old:{id:'old',status:'done',title:'可核对的历史任务',input:'https://tieba.baidu.com/p/1',source_url:'https://tieba.baidu.com/p/1',started_at:new Date().toISOString(),finished_at:new Date().toISOString(),progress:100,pages:2,duration_ms:1234,warnings:[],document:{title:'可核对的历史任务',url:'https://tieba.baidu.com/p/1',warnings:[],pagination:{has_more:false}}},
    failed:{id:'failed',status:'failed',title:'读取失败的任务',input:'https://tieba.baidu.com/p/2',source_url:'https://tieba.baidu.com/p/2',started_at:new Date().toISOString(),progress:20,pages:0,error:'源站暂时不可用',warnings:[]},
    sending:{id:'sending',status:'sending',title:'发送中的任务',source_url:'https://tieba.baidu.com/p/3',started_at:new Date().toISOString(),progress:96,pages:1,result_ready:true,warnings:[]},
    delivery:{id:'delivery',status:'delivery_failed',title:'QQ发送失败但产物保留',source_url:'https://tieba.baidu.com/p/4',started_at:new Date().toISOString(),finished_at:new Date().toISOString(),progress:96,pages:1,result_ready:true,error:'QQ 图片上传连接超时',warnings:[],document:{title:'QQ发送失败但产物保留',warnings:[]}},
  };
  let reads = 0;
  window.__requests = [];
  window.__downloads = [];
  window[bridgeName] = {
    ready:async () => {},
    apiGet:async (endpoint,params) => {
      window.__requests.push({method:'GET',endpoint,params});
      if (endpoint === 'state') return {settings,font,renderer:rendererSnapshot(),stats:{total:Object.keys(jobs).length,success:1,failed:1,cache_hits:0,active:0},jobs:[],history:Object.values(jobs),cache:{entries:3,bytes:1024*1024},capabilities:{render:true,forum:true,user:false}};
      if (endpoint === 'renderer') {
        if (window.__rendererReadFail) throw new Error('检测暂时失败，请稍后刷新。');
        return {renderer:rendererSnapshot(),settings};
      }
      if (endpoint === 'font') return {font,settings};
      if (endpoint === 'preview') return {data_url:png};
      if (endpoint === 'diagnostics') return {plugin_version:'1.0-test',render:{available:true},client:{cookie:'should-never-appear'},storage:{entries:3}};
      if (endpoint === 'job') {
        const id = params.id;
        if (id === 'new' && ++reads >= 2) jobs.new = {...jobs.new,status:'done',progress:100,pages:2,stage:'完成',document:{title:'真实接口流程测试',warnings:['解析范围内结果'],pagination:{has_more:true}}};
        return {job:jobs[id]};
      }
      throw new Error('Unexpected GET ' + endpoint);
    },
    apiPost:async (endpoint,body) => {
      window.__requests.push({method:'POST',endpoint,body});
      if (endpoint === 'renderer/prepare') {
        if (!['headless','chromium'].includes(body.engine) || Object.keys(body).length !== 1) throw new Error('Invalid renderer preparation request');
        if (window.__holdRendererPrepare) await new Promise(resolve => { window.__releaseRendererPrepare = resolve; });
        if (window.__rendererPrepareFail) throw new Error('无法启动准备任务。');
        window.__renderer.install = {status:'running',engine:body.engine,stage:'下载浏览器文件',error:''};
        return {renderer:rendererSnapshot(),settings};
      }
      if (endpoint === 'font') {
        if (window.__holdFontUpload) await new Promise(resolve => { window.__releaseFontUpload = resolve; });
        if (window.__holdFontAfterFirst && body.offset > 0) await new Promise(resolve => { window.__releaseNextFontChunk = resolve; });
        if (window.__fontFail) throw new Error('字体内容无效，请选择有效字体。');
        assertFontPayload(body);
        const id = body.upload_id || (++fontUploadSequence).toString(16).padStart(32,'0');
        if (!body.upload_id) fontUploads.set(id,{name:body.name,total:body.total_size,received:0});
        const pending = fontUploads.get(id);
        if (!pending || pending.name !== body.name || pending.total !== body.total_size || pending.received !== body.offset) throw new Error('Unexpected font upload offset');
        pending.received += atob(body.data).length;
        if (window.__fontWrongOffset) return {complete:false,upload_id:id,received:pending.received + 1};
        if (window.__fontFailAfterFirst && body.offset > 0) throw new Error('上传中断，请重新上传。');
        if (pending.received < pending.total) return {complete:false,upload_id:id,received:pending.received};
        if (pending.received !== pending.total) throw new Error('Unexpected total font upload size');
        font = {...emptyFont,uploaded:true,name:body.name,size:pending.total,sha256:'a'.repeat(64)};
        fontUploads.delete(id);
        settings = {...settings,font_override:'custom'};
        persistFont();
        return {complete:true,font,settings};
      }
      if (endpoint === 'font/reset') {
        font = {...emptyFont}; settings = {...settings,font_override:'lxgw'};
        persistFont(); return {font,settings};
      }
      if (endpoint === 'settings') {
        for (const [key,value] of Object.entries(body)) {
          if (!Object.hasOwn(schema,key)) throw new Error('Unsupported setting: ' + key);
          if (schema[key].options && !schema[key].options.includes(value)) throw new Error('Invalid setting value: ' + key);
        }
        for (const values of Object.values(body.group_overrides || {})) {
          for (const key of Object.keys(values)) if (!Object.hasOwn(schema,key)) throw new Error('Unsupported group setting: ' + key);
        }
        const {cookie,...publicValues} = body;
        settings = {...settings,...publicValues,cookie_set:Boolean(cookie || settings.cookie_set)};
        sessionStorage.setItem('tieba-lens-test-settings',JSON.stringify(settings));
        return {settings};
      }
      if (endpoint === 'parse') { jobs.new={id:'new',status:'queued',stage:'排队中',progress:0,pages:0,title:'真实接口流程测试',input:body.input,started_at:new Date().toISOString(),warnings:[]}; return {job:jobs.new}; }
      if (endpoint === 'retry' && body.id === 'failed') { jobs.retried={...jobs.failed,id:'retried',status:'queued',error:''}; return {job:jobs.retried}; }
      if (endpoint === 'cache/clear') return {cleared:3};
      if (endpoint === 'history/clear') { delete jobs.old; delete jobs.failed; return {cleared:2}; }
      throw new Error('Unexpected POST ' + endpoint);
    },
    download:async (endpoint,params,filename) => { window.__downloads.push({endpoint,params,filename}); },
  };
  function assertFontPayload(body) {
    if (!body || !/\.(ttf|otf|woff|woff2)$/i.test(body.name) || !/^[A-Za-z0-9+/]+={0,2}$/.test(body.data)) throw new Error('Invalid font upload payload');
    if (body.data.length > 524288 || JSON.stringify(body).length >= 1024 * 1024) throw new Error('Font chunk too large');
    if (!Number.isInteger(body.offset) || body.offset < 0 || !Number.isInteger(body.total_size) || body.total_size <= 0 || body.total_size > 33554432) throw new Error('Invalid font upload size');
    if (body.upload_id !== undefined && !/^[0-9a-f]{32}$/.test(body.upload_id)) throw new Error('Invalid font upload id');
    if (Object.keys(body).sort().join(',') !== (body.upload_id ? 'data,name,offset,total_size,upload_id' : 'data,name,offset,total_size')) throw new Error('Unexpected font payload fields');
  }
}

(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({headless:true});
  try {
    const offline = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    offline.on('pageerror',error => errors.push(error.message));
    await offline.goto(base);
    await offline.locator('#connection-state[data-state="offline"]').waitFor();
    assert.equal(await offline.locator('#parse-submit').isDisabled(),true);
    assert.match(await offline.locator('#connection-banner').textContent(),/尚未连接 AstrBot/);
    assert.match(await offline.locator('.sample-caption').textContent(),/样式示意/);
    assert.equal(schema.theme.default,'denia');
    assert.equal(await offline.locator('#style-sample').getAttribute('data-theme'),'denia');
    assert.equal(await offline.locator('#parse-themes input:checked').inputValue(),'denia');
    assert.deepEqual(schema.font_override.options,['lxgw','custom']);
    assert.equal(schema.font_override.default,'lxgw');
    assert.equal(await offline.locator('#parse-font-override').inputValue(),'lxgw');
    assert.equal(await offline.locator('#font-file').isDisabled(),true);
    assert.equal(schema.render_backend.default,'auto');
    assert.equal(schema.browser_engine.default,'headless');
    assert.equal(await offline.locator('#renderer-refresh').isDisabled(),true);
    assert.equal(await offline.locator('[data-renderer-prepare="headless"]').isDisabled(),true);
    assert.equal(await offline.locator('[data-renderer-prepare="chromium"]').isDisabled(),true);
    assert.equal(await offline.locator('[data-sample-theme="daniya"],[data-sample-theme="danyia"]').count(),0);
    assert.equal(await offline.locator('[role="tab"][data-sample-theme]').count(),6);
    for (const key of schema.theme.options) {
      await offline.locator(`[data-sample-theme="${key}"]`).click();
      assert.equal(await offline.locator('#style-sample').getAttribute('data-theme'),key);
      assert.equal(await offline.locator(`#sample-motif use`).getAttribute('href'),'#theme-' + key);
      assert.equal(await offline.locator(`[data-sample-theme="${key}"]`).getAttribute('aria-selected'),'true');
      assert.ok((await offline.locator('#sample-theme-description').textContent()).length > 10);
      if (['miku','denia'].includes(key)) await offline.locator('.style-panel').screenshot({path:path.join(artifacts,'console-theme-' + key + '.png'),animations:'disabled'});
    }
    await offline.locator('[data-sample-theme="bilibili"]').focus();
    await offline.keyboard.press('ArrowRight');
    assert.equal(await offline.locator('[data-sample-theme="paper"]').evaluate(element => element === document.activeElement),true);
    await offline.keyboard.press('End');
    assert.equal(await offline.locator('#style-sample').getAttribute('data-theme'),'bilibili');
    await offline.keyboard.press('Home');
    assert.equal(await offline.locator('#style-sample').getAttribute('data-theme'),'paper');
    await offline.screenshot({path:path.join(artifacts,'console-overview-desktop.png'),fullPage:true,animations:'disabled'});
    await offline.setViewportSize({width:375,height:812});
    assert.equal(await offline.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),true);
    await offline.screenshot({path:path.join(artifacts,'console-overview-mobile.png'),fullPage:true,animations:'disabled'});
    await offline.locator('#color-mode').click();
    await offline.screenshot({path:path.join(artifacts,'console-overview-mobile-dark.png'),fullPage:true,animations:'disabled'});
    assert.deepEqual(errors,[]);
    await offline.close();

    const rendering = await browser.newPage({viewport:{width:1440,height:1000}});
    rendering.on('pageerror',error => errors.push(error.message));
    await rendering.addInitScript(mockBridge,schema);
    await rendering.goto(base + '#settings');
    await rendering.locator('#connection-state[data-state="online"]').waitFor();
    assert.equal(await rendering.locator('#setting-render_backend').inputValue(),'auto');
    assert.equal(await rendering.locator('#setting-browser_engine').inputValue(),'headless');
    assert.deepEqual(await rendering.locator('#setting-render_backend option').evaluateAll(items => items.map(item => item.value)),schema.render_backend.options);
    assert.deepEqual(await rendering.locator('#setting-browser_engine option').evaluateAll(items => items.map(item => item.value)),schema.browser_engine.options);
    assert.match(await rendering.locator('#renderer-active').textContent(),/AstrBot 渲染服务/);
    assert.match(await rendering.locator('#renderer-service').textContent(),/已配置，可用/);
    assert.match(await rendering.locator('#renderer-local').textContent(),/暂未检测/);
    assert.match(await rendering.locator('.renderer-recommended').textContent(),/低占用推荐/);
    assert.equal(await rendering.locator('[data-override-key] option[value="render_backend"],[data-override-key] option[value="browser_engine"]').count(),0);
    await rendering.locator('#setting-comment_limit').fill('19');
    await rendering.locator('#setting-render_backend').selectOption('local');
    await rendering.locator('#setting-cookie').fill('BDUSS=unsaved-renderer-test');
    await rendering.locator('#font-file').setInputFiles({name:'保留选择.ttf',mimeType:'font/ttf',buffer:Buffer.from('pending-font')});
    await rendering.locator('#renderer-refresh').click();
    await rendering.waitForFunction(() => window.__requests.some(item => item.method === 'GET' && item.endpoint === 'renderer'));
    assert.equal(await rendering.locator('#setting-comment_limit').inputValue(),'19');
    assert.equal(await rendering.locator('#setting-render_backend').inputValue(),'local');
    assert.equal(await rendering.locator('#setting-cookie').inputValue(),'BDUSS=unsaved-renderer-test');
    assert.match(await rendering.locator('#font-selected').textContent(),/保留选择.ttf/);
    await rendering.evaluate(() => { window.__holdRendererPrepare = true; });
    await rendering.locator('[data-renderer-prepare="headless"]').click();
    await rendering.waitForFunction(() => typeof window.__releaseRendererPrepare === 'function');
    assert.equal(await rendering.locator('[data-renderer-prepare="headless"]').isDisabled(),true);
    assert.equal(await rendering.locator('[data-renderer-prepare="chromium"]').isDisabled(),true);
    assert.equal(await rendering.locator('#renderer-refresh').isDisabled(),true);
    await rendering.evaluate(() => { window.__holdRendererPrepare = false; window.__releaseRendererPrepare(); });
    await rendering.waitForFunction(() => document.querySelector('#renderer-install-state').textContent === '后台准备中');
    assert.equal(await rendering.locator('#renderer-install-progress').isVisible(),true);
    assert.match(await rendering.locator('#renderer-install-stage').textContent(),/下载浏览器文件/);
    assert.equal(await rendering.locator('#renderer-refresh').isDisabled(),false);
    assert.equal(await rendering.locator('#setting-render_backend').inputValue(),'local');
    assert.equal(await rendering.locator('#setting-browser_engine').inputValue(),'headless');
    await rendering.evaluate(() => { window.__renderer.install.stage = '正在校验浏览器文件'; });
    await rendering.locator('#renderer-refresh').click();
    await rendering.waitForFunction(() => document.querySelector('#renderer-install-stage').textContent.includes('正在校验'));
    await rendering.waitForTimeout(5700);
    assert.equal(await rendering.evaluate(() => window.__requests.filter(item => item.endpoint === 'renderer/prepare').length),1,'polling must not trigger another preparation');
    assert.equal(await rendering.evaluate(() => window.__requests.filter(item => item.endpoint === 'settings').length),0,'preparing must not save preferences');
    assert.equal(await rendering.locator('#setting-comment_limit').inputValue(),'19');
    assert.equal(await rendering.locator('#setting-cookie').inputValue(),'BDUSS=unsaved-renderer-test');
    assert.match(await rendering.locator('#font-selected').textContent(),/保留选择.ttf/);
    await rendering.evaluate(() => {
      window.__renderer.engines[0].installed = true;
      window.__renderer.local = {available:true,browser:'精简 Chromium'};
      window.__renderer.install = {status:'done',engine:'headless',stage:'浏览器已准备',error:''};
    });
    await rendering.locator('#renderer-refresh').click();
    await rendering.waitForFunction(() => document.querySelector('#renderer-install-state').textContent === '已准备');
    assert.match(await rendering.locator('#renderer-local').textContent(),/可用.*精简 Chromium/);
    assert.equal(await rendering.locator('#renderer-installed-headless').textContent(),'已准备');
    assert.equal(await rendering.locator('#renderer-install-progress').isVisible(),false);
    await rendering.locator('#settings-renderer').screenshot({path:path.join(artifacts,'console-renderer-desktop.png'),animations:'disabled'});
    await rendering.setViewportSize({width:375,height:812});
    assert.equal(await rendering.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true,'renderer mobile overflow');
    await rendering.locator('#settings-renderer').screenshot({path:path.join(artifacts,'console-renderer-mobile.png'),animations:'disabled'});
    await rendering.locator('#color-mode').click();
    await rendering.locator('#settings-renderer').screenshot({path:path.join(artifacts,'console-renderer-mobile-dark.png'),animations:'disabled'});
    await rendering.setViewportSize({width:1440,height:1000});
    await rendering.evaluate(() => { window.__renderer.install = {status:'failed',engine:'chromium',stage:'下载失败',error:'网络连接中断，请重新准备。'}; });
    await rendering.locator('#renderer-refresh').click();
    await rendering.waitForFunction(() => document.querySelector('#renderer-install-state').textContent === '准备失败');
    assert.match(await rendering.locator('#renderer-error').textContent(),/网络连接中断/);
    assert.equal(await rendering.locator('[data-renderer-prepare="chromium"]').textContent(),'重新准备');
    await rendering.evaluate(() => { window.__rendererPrepareFail = true; });
    await rendering.locator('[data-renderer-prepare="chromium"]').click();
    await rendering.waitForFunction(() => document.querySelector('#renderer-error').textContent.includes('无法启动'));
    assert.equal(await rendering.locator('[data-renderer-prepare="chromium"]').isDisabled(),false);
    await rendering.evaluate(() => { window.__rendererPrepareFail = false; window.__rendererReadFail = true; });
    await rendering.locator('#renderer-refresh').click();
    await rendering.waitForFunction(() => document.querySelector('#renderer-error').textContent.includes('检测暂时失败'));
    await rendering.evaluate(() => { window.__rendererReadFail = false; });
    await rendering.locator('[data-renderer-prepare="chromium"]').click();
    await rendering.waitForFunction(() => document.querySelector('#renderer-install-state').textContent === '后台准备中');
    assert.equal(await rendering.locator('#renderer-error').isVisible(),false);
    assert.equal(await rendering.locator('#setting-render_backend').inputValue(),'local');
    assert.equal(await rendering.locator('#setting-browser_engine').inputValue(),'headless','preparing a different browser must not modify the selection');
    await rendering.locator('#setting-browser_engine').selectOption('chromium');
    await rendering.locator('#settings-save').click();
    await rendering.waitForFunction(() => document.querySelector('#settings-save-status').textContent === '已与后端同步');
    const rendererSettings = await rendering.evaluate(() => window.__requests.filter(item => item.endpoint === 'settings').at(-1).body);
    assert.equal(rendererSettings.render_backend,'local');
    assert.equal(rendererSettings.browser_engine,'chromium');
    assert(Object.values(rendererSettings.group_overrides).every(values => !Object.hasOwn(values,'render_backend') && !Object.hasOwn(values,'browser_engine')));
    assert.equal(await rendering.evaluate(() => window.__requests.filter(item => item.endpoint === 'font').length),0,'renderer controls must not upload a selected font');
    await rendering.reload();
    await rendering.locator('#connection-state[data-state="online"]').waitFor();
    assert.equal(await rendering.locator('#setting-render_backend').inputValue(),'local');
    assert.equal(await rendering.locator('#setting-browser_engine').inputValue(),'chromium');
    await rendering.close();

    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',error => errors.push(error.message));
    await page.addInitScript(mockBridge,schema);
    await page.goto(base);
    await page.locator('#connection-state[data-state="online"]').waitFor();
    assert.equal(await page.locator('[data-retry-job="failed"]').isDisabled(),false);
    assert.equal(await page.locator('#setting-comment_limit').inputValue(),'12');
    assert.deepEqual(await page.locator('#setting-theme option').evaluateAll(items => items.map(item => item.value)),schema.theme.options);
    assert.deepEqual(await page.locator('#setting-font_override option').evaluateAll(items => items.map(item => item.value)),schema.font_override.options);
    assert.deepEqual(await page.locator('#parse-font-override option').evaluateAll(items => items.map(item => item.value)),schema.font_override.options);
    assert.deepEqual(await page.locator('#parse-themes input').evaluateAll(items => items.map(item => item.value)),schema.theme.options);
    assert.equal(await page.locator('#setting-font_override').inputValue(),'lxgw');
    assert.equal(await page.locator('#setting-single_image').isChecked(),true);
    assert.equal(await page.locator('#setting-page_height').isDisabled(),true);
    assert.equal(await page.locator('#setting-reaction_fetch,#setting-reaction_render').count(),0);
    assert.equal(await page.locator('#settings-reactions input[type="text"]').count(),3);
    assert.equal(await page.locator('.legacy-note').count(),1);
    assert.equal(await page.locator('[data-override-key]').count(),4);
    assert.equal(await page.locator('[data-override-value][data-key="show_source"]').inputValue(),'false');
    assert.equal(await page.locator('#setting-show_source').isChecked(),true);
    assert.equal(await page.locator('#parse-show-source').isChecked(),true);
    assert.match(await page.locator('#setting-show_source-help').textContent(),/新解析生效/);
    assert.equal(await page.locator('#parse-themes input:checked').inputValue(),'denia');
    assert.equal(await page.locator('#parse-font-override').inputValue(),'lxgw');
    await page.locator('[data-nav="settings"]').click();
    await page.locator('#setting-comment_limit').fill('101');
    await page.locator('#settings-save').click();
    await page.locator('#settings-errors').waitFor({state:'visible'});
    assert.equal(await page.locator('#setting-comment_limit').getAttribute('aria-invalid'),'true');
    await page.locator('#setting-comment_limit').fill('17');
    await page.locator('#setting-theme').selectOption('bilibili');
    await page.locator('#setting-font_override').selectOption('lxgw');
    await page.locator('#setting-single_image').uncheck();
    await page.locator('#setting-show_source').uncheck();
    assert.equal(await page.locator('#setting-page_height').isDisabled(),false);
    await page.locator('#setting-page_height').fill('2400');
    await page.locator('#add-group').click();
    const group = page.locator('.group-card').last();
    await group.locator('[data-group-id]').fill('123456');
    for (const [key,value] of [['theme','denia'],['font_override','custom'],['single_image','false'],['show_source','true']]) {
      await group.locator('[data-add-override]').click();
      await group.locator('.override-row').last().locator('[data-override-key]').selectOption(key);
      await group.locator(`[data-override-value][data-key="${key}"]`).selectOption(value);
    }
    await page.locator('#setting-blacklist').fill('223344\n334455');
    await page.locator('#setting-cookie').fill('BDUSS=private-test-value');
    await page.locator('#settings-save').click();
    await page.waitForFunction(() => document.querySelector('#settings-save-status').textContent === '已与后端同步');
    const saved = await page.evaluate(() => window.__requests.filter(item => item.endpoint === 'settings').at(-1).body);
    assert.equal(saved.comment_limit,17);
    assert.equal(saved.theme,'bilibili');
    assert.equal(saved.font_override,'lxgw');
    assert.equal(saved.render_backend,'auto');
    assert.equal(saved.browser_engine,'headless');
    assert.equal(saved.single_image,false);
    assert.equal(Object.hasOwn(saved,'show_source'),true);
    assert.equal(saved.show_source,false);
    assert.equal(saved.page_height,2400);
    assert.deepEqual(saved.group_overrides,{'998877':{theme:'miku',font_override:'lxgw',single_image:false,show_source:false},'123456':{theme:'denia',font_override:'custom',single_image:false,show_source:true}});
    assert.equal(Object.hasOwn(saved,'reaction_fetch'),false);
    assert.equal(Object.hasOwn(saved,'reaction_render'),false);
    assert.equal(await page.locator('.legacy-note').count(),0);
    assert.deepEqual(saved.blacklist,['223344','334455']);
    assert.equal(await page.locator('#setting-cookie').inputValue(),'');
    // Reload from the saved backend snapshot, not from the current DOM state.
    await page.reload();
    await page.locator('#connection-state[data-state="online"]').waitFor();
    assert.equal(await page.locator('#setting-show_source').isChecked(),false);
    assert.equal(await page.locator('#parse-show-source').isChecked(),false);
    const reloadedGroups = page.locator('.group-card');
    assert.equal(await reloadedGroups.filter({has:page.locator('[data-group-id][value="998877"]')}).locator('[data-override-value][data-key="show_source"]').inputValue(),'false');
    assert.equal(await reloadedGroups.filter({has:page.locator('[data-group-id][value="123456"]')}).locator('[data-override-value][data-key="show_source"]').inputValue(),'true');
    assert.equal(await page.locator('#setting-cookie').inputValue(),'');
    await page.screenshot({path:path.join(artifacts,'console-settings-desktop.png'),fullPage:true,animations:'disabled'});
    await page.locator('[data-nav="settings"]').click();
    assert.match(await page.locator('#font-current').textContent(),/尚未上传/);
    assert.equal(await page.locator('#font-upload-action').isDisabled(),true);
    const fontChooserPromise = page.waitForEvent('filechooser');
    await page.locator('#font-choose-action').click();
    await (await fontChooserPromise).setFiles({name:'font.exe',mimeType:'application/octet-stream',buffer:Buffer.from('invalid')});
    assert.match(await page.locator('#font-upload-error').textContent(),/TTF.*OTF.*WOFF/);
    assert.equal(await page.locator('#font-upload-action').isDisabled(),true);
    await page.evaluate(() => {
      const input = document.querySelector('#font-file'), transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array(33554433)],'large.ttf'));
      input.files = transfer.files; input.dispatchEvent(new Event('change',{bubbles:true}));
    });
    assert.match(await page.locator('#font-upload-error').textContent(),/32 MB/);
    assert.equal(await page.locator('#font-upload-action').isDisabled(),true);
    assert.equal(await page.evaluate(() => window.__requests.some(item => item.endpoint === 'font')),false);
    await page.locator('#font-file').setInputFiles({name:'broken.woff2',mimeType:'font/woff2',buffer:Buffer.from('broken-font')});
    await page.evaluate(() => { window.__fontFail = true; });
    await page.locator('#font-upload-action').click();
    await page.waitForFunction(() => document.querySelector('#font-upload-error').textContent.includes('字体内容无效'));
    assert.equal(await page.locator('#font-upload-action').isDisabled(),false);
    assert.equal(await page.locator('#setting-font_override').inputValue(),'lxgw');
    assert.match(await page.locator('#font-current').textContent(),/尚未上传/);
    await page.evaluate(() => { window.__fontFail = false; window.__holdFontUpload = true; });
    await page.locator('#setting-comment_limit').fill('18');
    await page.locator('#font-file').setInputFiles({name:'自选字体.ttf',mimeType:'font/ttf',buffer:Buffer.from([0,1,0,0,1,2,3,4])});
    await page.locator('#font-upload-action').click();
    await page.waitForFunction(() => typeof window.__releaseFontUpload === 'function');
    assert.equal(await page.locator('#font-upload-progress').isHidden(),false);
    assert.equal(await page.locator('#font-file').isDisabled(),true);
    assert.equal(await page.locator('#font-upload-action').isDisabled(),true);
    assert.equal(await page.locator('#font-reset-action').isDisabled(),true);
    assert.equal(await page.locator('#settings-save').isDisabled(),true);
    await page.evaluate(() => { window.__holdFontUpload = false; window.__releaseFontUpload(); });
    await page.waitForFunction(() => document.querySelector('#font-current').textContent.includes('自选字体.ttf'));
    assert.equal(await page.locator('#setting-font_override').inputValue(),'custom');
    assert.equal(await page.locator('#parse-font-override').inputValue(),'custom');
    assert.equal(await page.locator('#setting-comment_limit').inputValue(),'18');
    assert.match(await page.locator('#settings-save-status').textContent(),/未保存/);
    await page.locator('#settings-reset').click();
    assert.equal(await page.locator('#setting-comment_limit').inputValue(),'17');
    assert.equal(await page.locator('#setting-font_override').inputValue(),'custom');
    await page.reload();
    await page.locator('#connection-state[data-state="online"]').waitFor();
    assert.match(await page.locator('#font-current').textContent(),/自选字体.ttf/);
    assert.equal(await page.locator('#setting-font_override').inputValue(),'custom');
    await page.evaluate(() => { window.__fontWrongOffset = true; });
    await page.locator('#font-file').setInputFiles({name:'错序字体.ttf',mimeType:'font/ttf',buffer:Buffer.alloc(1024 * 1024,1)});
    await page.locator('#font-upload-action').click();
    await page.waitForFunction(() => document.querySelector('#font-upload-error').textContent.includes('分块顺序异常'));
    assert.equal(await page.evaluate(() => window.__requests.filter(item => item.endpoint === 'font' && item.body.name === '错序字体.ttf').length),1);
    assert.match(await page.locator('#font-current').textContent(),/自选字体.ttf/);
    await page.evaluate(() => { window.__fontWrongOffset = false; window.__fontFailAfterFirst = true; });
    await page.locator('#font-file').setInputFiles({name:'中断字体.ttf',mimeType:'font/ttf',buffer:Buffer.alloc(1024 * 1024,2)});
    await page.locator('#font-upload-action').click();
    await page.waitForFunction(() => document.querySelector('#font-upload-error').textContent.includes('上传中断'));
    assert.equal(await page.evaluate(() => window.__requests.filter(item => item.endpoint === 'font' && item.body.name === '中断字体.ttf').length),2);
    assert.match(await page.locator('#font-current').textContent(),/自选字体.ttf/);
    assert.equal(await page.locator('#setting-font_override').inputValue(),'custom');
    await page.evaluate(() => { window.__fontFailAfterFirst = false; });
    await page.locator('#font-upload-action').click();
    await page.waitForFunction(() => document.querySelector('#font-current').textContent.includes('中断字体.ttf'));
    const resumed = await page.evaluate(() => window.__requests.filter(item => item.endpoint === 'font' && item.body.name === '中断字体.ttf').map(item => item.body));
    assert.deepEqual(resumed.map(item => item.offset),[0,393216,0,393216,786432]);
    assert.equal(Object.hasOwn(resumed[0],'upload_id'),false);
    assert.equal(Object.hasOwn(resumed[2],'upload_id'),false);
    assert.notEqual(resumed[1].upload_id,resumed[3].upload_id);
    await page.evaluate(() => { window.__holdFontAfterFirst = true; });
    await page.locator('#font-file').setInputFiles({name:'替换字体.woff2',mimeType:'font/woff2',buffer:Buffer.alloc(1024 * 1024,3)});
    await page.locator('#font-upload-action').click();
    await page.waitForFunction(() => typeof window.__releaseNextFontChunk === 'function');
    assert.equal(await page.locator('#font-upload-progress').getAttribute('value'),'37');
    assert.match(await page.locator('#font-upload-status').textContent(),/393,216.*1,048,576/);
    assert.match(await page.locator('#font-current').textContent(),/中断字体.ttf/);
    await page.evaluate(() => { window.__holdFontAfterFirst = false; window.__releaseNextFontChunk(); });
    await page.waitForFunction(() => document.querySelector('#font-current').textContent.includes('替换字体.woff2'));
    const chunks = await page.evaluate(() => window.__requests.filter(item => item.endpoint === 'font' && item.body.name === '替换字体.woff2').map(item => item.body));
    assert.deepEqual(chunks.map(item => item.offset),[0,393216,786432]);
    assert.deepEqual(chunks.map(item => item.total_size),[1048576,1048576,1048576]);
    assert.equal(Object.hasOwn(chunks[0],'upload_id'),false);
    assert.equal(chunks[1].upload_id,chunks[2].upload_id);
    assert(chunks.every(item => item.data.length <= 524288 && JSON.stringify(item).length < 1024 * 1024));
    await page.screenshot({path:path.join(artifacts,'console-custom-font-desktop.png'),fullPage:true,animations:'disabled'});
    await page.setViewportSize({width:375,height:812});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.locator('.font-upload').screenshot({path:path.join(artifacts,'console-custom-font-mobile.png'),animations:'disabled'});
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('#font-reset-action').click();
    await page.waitForFunction(() => document.querySelector('#font-current').textContent.includes('尚未上传'));
    assert.equal(await page.locator('#setting-font_override').inputValue(),'lxgw');
    assert.equal(await page.locator('#parse-font-override').inputValue(),'lxgw');
    await page.reload();
    await page.locator('#connection-state[data-state="online"]').waitFor();
    assert.match(await page.locator('#font-current').textContent(),/尚未上传/);
    assert.equal(await page.locator('#setting-font_override').inputValue(),'lxgw');
    await page.locator('[data-nav="parse"]').click();
    assert.equal(await page.locator('#parse-themes input:checked').inputValue(),'bilibili');
    assert.equal(await page.locator('#parse-font-override').inputValue(),'lxgw');
    assert.equal(await page.locator('#parse-single-image').isChecked(),false);
    assert.equal(await page.locator('#parse-show-source').isChecked(),false);
    await page.locator('#parse-themes label[data-theme="miku"]').click();
    await page.locator('#parse-themes input[value="miku"]').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#parse-themes input:checked').inputValue(),'bilibili');
    await page.keyboard.press('ArrowLeft');
    assert.equal(await page.locator('#parse-themes input:checked').inputValue(),'miku');
    await page.locator('#parse-font-override').selectOption('custom');
    await page.locator('#parse-single-image').check();
    await page.locator('#parse-input').fill('https://tieba.baidu.com/p/11060929398');
    await page.locator('#parse-original-images').check();
    await page.locator('#parse-submit').click();
    await page.locator('#preview-surface > img').waitFor({timeout:10000});
    assert.match(await page.locator('#preview-page-number').textContent(),/1 \/ 2/);
    const parseOptions = await page.evaluate(() => window.__requests.filter(item => item.endpoint === 'parse').at(-1).body.options);
    assert.equal(parseOptions.forward_original_images,true);
    assert.equal(parseOptions.theme,'miku');
    assert.equal(parseOptions.font_override,'custom');
    assert.equal(parseOptions.single_image,true);
    assert.equal(Object.hasOwn(parseOptions,'show_source'),true);
    assert.equal(parseOptions.show_source,false);
    assert.equal(await page.locator('#setting-theme').inputValue(),'bilibili');
    assert.equal(await page.locator('#setting-single_image').isChecked(),false);
    assert.equal(await page.locator('#setting-show_source').isChecked(),false);
    await page.locator('#preview-next').click();
    await page.waitForFunction(() => document.querySelector('#preview-page-number').textContent === '2 / 2');
    await page.locator('#export-format').selectOption('markdown');
    await page.locator('#export-button').click();
    const downloaded = await page.evaluate(() => window.__downloads[0]);
    assert.equal(downloaded.endpoint,'export'); assert.equal(downloaded.params.format,'markdown'); assert.match(downloaded.filename,/\.md$/);
    await page.locator('#parse-show-source').check();
    await page.locator('#parse-submit').click();
    await page.waitForFunction(() => window.__requests.filter(item => item.endpoint === 'parse').length === 2);
    assert.equal(await page.evaluate(() => window.__requests.filter(item => item.endpoint === 'parse').at(-1).body.options.show_source),true);
    assert.equal(await page.locator('#setting-show_source').isChecked(),false);
    await page.locator('#parse-show-source').uncheck();
    await page.locator('#parse-submit').click();
    await page.waitForFunction(() => window.__requests.filter(item => item.endpoint === 'parse').length === 3);
    assert.equal(await page.evaluate(() => window.__requests.filter(item => item.endpoint === 'parse').at(-1).body.options.show_source),false);
    await page.locator('[data-nav="history"]').click();
    await page.locator('#history-filter').selectOption('active');
    assert.match(await page.locator('#history-table').textContent(),/发送中的任务/);
    assert.match(await page.locator('#history-table').textContent(),/发送卡片/);
    await page.locator('#history-filter').selectOption('failed');
    assert.match(await page.locator('#history-table').textContent(),/读取失败的任务/);
    assert.match(await page.locator('#history-table').textContent(),/QQ发送失败但产物保留/);
    await page.locator('#history-filter').selectOption('done');
    assert.doesNotMatch(await page.locator('#history-table').textContent(),/QQ发送失败但产物保留/);
    await page.locator('#history-filter').selectOption('all');
    await page.locator('#history-table [data-open-job="delivery"]').click();
    await page.locator('#preview-surface > img').waitFor();
    assert.match(await page.locator('#current-job .job-error').textContent(),/QQ 图片上传连接超时/);
    assert.match(await page.locator('#current-job .job-warning').textContent(),/不会重新发送到 QQ/);
    assert.match(await page.locator('#current-job [data-retry-job="delivery"]').textContent(),/重新解析/);
    assert.equal(await page.locator('#export-button').isDisabled(),false);
    await page.locator('#export-format').selectOption('zip');
    await page.locator('#export-button').click();
    assert.equal(await page.evaluate(() => window.__downloads.at(-1).params.id),'delivery');
    await page.locator('[data-nav="history"]').click();
    await page.locator('[data-retry-job="failed"]').first().click();
    await page.waitForFunction(() => window.__requests.some(item => item.endpoint === 'retry' && item.body.id === 'failed'));
    await page.locator('[data-nav="maintenance"]').click();
    await page.locator('#cache-clear').click();
    await page.locator('#confirm-dialog[open]').waitFor();
    assert.equal(await page.locator('#confirm-dialog .secondary-button').evaluate(element => element === document.activeElement),true);
    await page.locator('#confirm-accept').click();
    await page.waitForFunction(() => window.__requests.some(item => item.endpoint === 'cache/clear'));
    await page.locator('#diagnostics-refresh').click();
    await page.waitForFunction(() => document.querySelector('#diagnostics-output').textContent.includes('1.0-test'));
    assert.doesNotMatch(await page.locator('#diagnostics-output').textContent(),/should-never-appear/);
    assert.match(await page.locator('#diagnostics-output').textContent(),/已隐藏/);
    await page.setViewportSize({width:375,height:812});
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await page.locator('[data-sample-theme="paper"]').evaluate(element => getComputedStyle(element).transitionDuration),'0s');
    assert.equal(await page.locator('[data-sample-theme="paper"]').evaluate(element => getComputedStyle(element).animationName),'none');
    for (const name of ['overview','parse','settings','history','maintenance']) {
      await page.locator(`[data-nav="${name}"]`).click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true,`${name} mobile overflow`);
    }
    await page.locator('[data-nav="settings"]').click();
    await page.screenshot({path:path.join(artifacts,'console-settings-mobile.png'),fullPage:true,animations:'disabled'});
    await page.setViewportSize({width:812,height:375});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true,'landscape overflow');
    assert.equal(await page.evaluate(() => window.__requests.some(item => item.endpoint.startsWith('jobs/'))),false);
    assert.equal(await page.evaluate(() => window.__requests.some(item => item.endpoint === 'job' && item.params.id === 'new')),true);
    assert.deepEqual(errors,[]);
    const legacy = await browser.newPage({viewport:{width:1440,height:1000}});
    legacy.on('pageerror',error => errors.push(error.message));
    await legacy.addInitScript(mockBridge,{schema,bridgeName:'AstrBotPluginPage'});
    await legacy.goto(base);
    await legacy.locator('#connection-state[data-state="online"]').waitFor();
    assert.equal(await legacy.evaluate(() => typeof window.AstrBotPluginView),'undefined');
    await legacy.locator('[data-nav="settings"]').click();
    await legacy.locator('#font-file').setInputFiles({name:'旧桥接字体.ttf',mimeType:'font/ttf',buffer:Buffer.from('legacy-font')});
    await legacy.locator('#font-upload-action').click();
    await legacy.waitForFunction(() => document.querySelector('#font-current').textContent.includes('旧桥接字体.ttf'));
    assert.equal(await legacy.locator('#setting-font_override').inputValue(),'custom');
    await legacy.close();
    assert.deepEqual(errors,[]);
    console.log('PASS: renderer global preferences, service/local detection, both browser preparations, pending/completed/failed progress, retry/refresh errors, busy guards, polling without duplicate downloads and preserved unsaved settings/fonts; both AstrBot bridge names; bounded font chunks, checked offsets, real progress, interrupted preservation and fresh retry; lxgw/custom fonts, format/32MB validation, upload errors, busy/progress, apply/replace/reset/reload and unsaved edits; denia default/canonical keys; sending and delivery-failed filters, retained preview/export and reparse; source visibility false/global/group save and reload, per-parse inheritance and true/false override; six themes, keyboard tabs, fonts/long images, strict schema save, legacy cleanup, three reactions, original images, preview/export/retry, diagnostics, mobile/landscape/reduced motion; no page errors.');
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode=1; });
