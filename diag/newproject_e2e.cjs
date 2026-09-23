// Drives the Studio "New project" flow with Playwright's Electron support.
//
// usage: node newproject_e2e.cjs <mode> <executable> [appDir]
//   mode = "probe"   : record what the current build does (no pass/fail on behaviour)
//   mode = "verify"  : fixed build must create a project and show a visible error on failure
//
// Only the native folder picker is stubbed (automation cannot click OS dialogs);
// everything else - renderer, preload, IPC, main process, Python engine - is real.
const {_electron: electron} = require('playwright-core');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const [mode, executablePath, appDir] = process.argv.slice(2);
const log = (...a) => console.log('[e2e]', ...a);

async function launch() {
  const extra = (process.env.E2E_EXTRA_ARGS || '').split(' ').filter(Boolean);
  const app = await electron.launch({executablePath, args: [...(appDir ? [appDir] : []), ...extra], timeout: 120000, env: {...process.env}});
  const win = await app.firstWindow();
  const consoleLines = [];
  win.on('console', (m) => { if (m.type() === 'error') consoleLines.push(`[renderer console.error] ${m.text()}`); });
  win.on('pageerror', (e) => consoleLines.push(`[renderer uncaught exception] ${e.message}`));
  win.on('dialog', async (d) => { consoleLines.push(`[renderer dialog event] type=${d.type()} message=${d.message()}`); await d.dismiss().catch(() => {}); });
  await win.waitForSelector('text=New project', {timeout: 90000});
  return {app, win, consoleLines};
}

async function stubPicker(app, returnPath) {
  await app.evaluate(({dialog}, p) => {
    globalThis.__pickerCalls = [];
    dialog.showOpenDialog = async (...args) => {
      const opts = args[args.length - 1] || {};
      globalThis.__pickerCalls.push(opts.title || '(no title)');
      return {canceled: false, filePaths: [p]};
    };
  }, returnPath);
}

const pickerCalls = (app) => app.evaluate(() => globalThis.__pickerCalls || []);
const bodyText = (win) => win.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').trim().slice(0, 400));

async function probe() {
  const {app, win, consoleLines} = await launch();
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'ae-probe-'));
  await stubPicker(app, parent);
  const promptProbe = await win.evaluate(() => {
    try { const v = window.prompt('probe', 'default'); return {threw: false, type: typeof v, value: v === undefined ? 'undefined' : v}; }
    catch (e) { return {threw: true, message: String(e)}; }
  });
  log('window.prompt in this build ->', JSON.stringify(promptProbe));
  await win.click('button:has-text("New project")');
  await win.waitForTimeout(4000);
  log('folder picker calls after clicking New project ->', JSON.stringify(await pickerCalls(app)));
  log('project folders created ->', JSON.stringify(fs.readdirSync(parent)));
  log('visible text after click ->', await bodyText(win));
  log('renderer console ->', JSON.stringify(consoleLines));
  await app.close();
}

async function verify() {
  // 1. Success path.
  let {app, win, consoleLines} = await launch();
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'ae-new-'));
  await stubPicker(app, parent);
  await win.click('button:has-text("New project")');
  const input = win.locator('.modal input');
  await input.waitFor({timeout: 10000});
  log('name dialog shown with default ->', await input.inputValue());
  await input.fill('E2E Test Short');
  await win.click('.modal button:has-text("Create project")');
  await win.waitForSelector('.topbar >> text=E2E Test Short', {timeout: 60000});
  const projectFile = path.join(parent, 'E2E Test Short', 'project.json');
  const exists = fs.existsSync(projectFile);
  log('folder picker calls ->', JSON.stringify(await pickerCalls(app)));
  log('project.json created at', projectFile, '->', exists);
  if (!exists) throw new Error('project.json was not created');
  log('workspace text ->', await bodyText(win));
  await app.close();

  // 2. Failure path: the chosen location is a file, so the engine cannot create the folder.
  ({app, win, consoleLines} = await launch());
  const blocker = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'ae-fail-')), 'not-a-folder.txt');
  fs.writeFileSync(blocker, 'x');
  await stubPicker(app, blocker);
  await win.click('button:has-text("New project")');
  await win.locator('.modal input').waitFor({timeout: 10000});
  await win.click('.modal button:has-text("Create project")');
  const errorBox = win.locator('.modal .error-banner');
  await errorBox.waitFor({timeout: 60000});
  const message = (await errorBox.innerText()).trim();
  log('visible error message ->', message);
  if (!message.startsWith('Could not create the project')) throw new Error('unexpected error text');
  if (!(await errorBox.isVisible())) throw new Error('error message not visible');
  await app.close();
  log('VERIFY PASSED');
}

(mode === 'probe' ? probe() : verify()).catch((e) => { console.error('[e2e] FAILED:', e && e.stack || e); process.exit(1); });
