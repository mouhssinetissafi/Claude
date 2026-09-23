// Drives Studio's Generate Edit flow on an installed Windows build with Playwright.
//
// usage: node generate_e2e.cjs <mode> <exe> <photosDir> [minutes]
//   probe  : run the user's scenario, record every job event; on a stall dump the
//            process tree, the engine job log/state and py-spy stacks; then Cancel.
//   verify : fixed build must finish with a preview, show stage progress in the UI,
//            and a second run must be stopped by Cancel with no engine child left.
const {_electron: electron} = require('playwright-core');
const {execSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const [mode, exe, photosDir, minutesArg] = process.argv.slice(2);
const LIMIT_MS = Number(minutesArg || 8) * 60 * 1000;
const log = (...a) => console.log(`[e2e ${new Date().toISOString().slice(11, 19)}]`, ...a);
const SCRIPT = [
  'This SUV looks calm from far away, and that calm is carefully designed.',
  'Up close, every surface has a job, and nothing is there by accident.',
  'The wheels are large and sharp, and they fill the arches without looking heavy.',
  'The headlights draw one clean line across the front of the car.',
  'The hood is long and quiet, with a single crease that catches the light.',
  'The badge sits low and small, so the shape does the talking.',
  'Inside, the seats look soft and simple, with stitching that follows every curve.',
  'The dashboard stays clean, with fewer buttons than you would expect.',
  'Even the door panels match the seats, so the cabin reads as one piece.',
  'From the side, the roof line runs straight and calm to the tail lights.',
  'The tail lights echo the front, which ties the whole design together.',
  'Nothing on this car shouts for attention, and that is the point.',
  'It is built to feel expensive without ever needing to say so.',
  'Look again at the first shot, and it reads differently now.',
].join('\n');

const ps = (cmd) => {
  try { return execSync(`powershell -NoProfile -Command "${cmd.replace(/"/g, '\\"')}"`, {encoding: 'utf8', timeout: 120000}); }
  catch (e) { return `(command failed: ${e.message})`; }
};
const processTree = () => ps("Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'autoeditor|ffmpeg|ffprobe|node|npx|chrome' } | ForEach-Object { '{0} parent={1} {2} :: {3}' -f $_.ProcessId, $_.ParentProcessId, $_.Name, $_.CommandLine }");
const enginePids = () => ps("(Get-Process autoeditor-engine -ErrorAction SilentlyContinue).Id -join ','").trim().split(',').filter(Boolean);

async function setup() {
  // E2E_APP_DIR / E2E_EXTRA_ARGS are only used for local dry runs of an unpackaged build.
  const args = [...(process.env.E2E_APP_DIR ? [process.env.E2E_APP_DIR] : []), ...(process.env.E2E_EXTRA_ARGS || '').split(' ').filter(Boolean)];
  const app = await electron.launch({executablePath: exe, args, timeout: 120000});
  const win = await app.firstWindow();
  win.on('pageerror', (e) => log('renderer uncaught exception:', e.message));
  await win.waitForSelector('text=New project', {timeout: 90000});
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'ae-gen-'));
  const photos = fs.readdirSync(photosDir).filter((f) => /\.jpe?g$/i.test(f)).sort().map((f) => path.join(photosDir, f));
  await app.evaluate(({dialog}, queue) => {
    globalThis.__pickerQueue = queue;
    dialog.showOpenDialog = async () => ({canceled: false, filePaths: globalThis.__pickerQueue.shift() || []});
  }, [[parent], photos]);
  await win.click('button:has-text("New project")');
  await win.locator('.modal input').fill('Hang Repro');
  await win.click('.modal button:has-text("Create project")');
  await win.waitForSelector('.topbar >> text=Hang Repro', {timeout: 60000});
  await win.click('button:has-text("Add photos / videos")');
  await win.waitForFunction((n) => document.querySelectorAll('.media-item').length === n, photos.length, {timeout: 120000});
  await win.click('.segmented button:has-text("My script")');
  await win.locator('.script-box').fill(SCRIPT);
  await win.locator('label:has-text("Free test mode") input[type=checkbox]').check();
  await win.waitForTimeout(1200); // let the debounced autosave land
  log(`project ready: ${photos.length} photos, manual script, free test mode`);
  await win.evaluate(() => { window.__events = []; window.studio.onEvent((e) => window.__events.push({t: Date.now(), e})); });
  return {app, win, projectRoot: path.join(parent, 'Hang Repro')};
}

// Poll captured events; returns the last job object seen.
async function watch(win, until, limitMs) {
  const t0 = Date.now();
  let seen = 0;
  let job = null;
  let lastProgress = Date.now();
  while (Date.now() - t0 < limitMs) {
    const events = await win.evaluate((from) => window.__events.slice(from), seen);
    seen += events.length;
    for (const {t, e} of events) {
      const stamp = `+${((t - t0) / 1000).toFixed(0)}s`;
      if (e.event === 'job.log') { log(stamp, 'LOG', e.params.line.slice(0, 220)); lastProgress = Date.now(); }
      else if (e.event === 'job.status') { job = e.params.job; log(stamp, 'STATUS', job.status, job.message || '', job.stage ? `stage=${JSON.stringify(job.stage)}` : ''); lastProgress = Date.now(); }
      else if (e.event === 'job.progress') { log(stamp, 'PROGRESS', JSON.stringify(e.params).slice(0, 220)); lastProgress = Date.now(); }
      else log(stamp, e.event, JSON.stringify(e.params).slice(0, 200));
    }
    if (job && until(job)) return {job, stalled: false};
    await win.waitForTimeout(5000);
    if (Date.now() - lastProgress > 180000) { log('no job event for 180s'); return {job, stalled: true}; }
  }
  return {job, stalled: true};
}

async function dumpDiagnostics(projectRoot, job) {
  log('=== DIAGNOSTICS: process tree ===\n' + processTree());
  if (job) log('job command:', JSON.stringify(job.command));
  const work = path.join(projectRoot, '.engine', 'work');
  for (const name of fs.existsSync(work) ? fs.readdirSync(work) : []) {
    for (const f of ['job_state.json', 'job.log']) {
      const p = path.join(work, name, f);
      if (fs.existsSync(p)) log(`=== ${name}/${f} (tail) ===\n` + fs.readFileSync(p, 'utf8').split(/\r?\n/).slice(-40).join('\n'));
    }
  }
  if (!fs.existsSync(work)) log('no .engine/work folder yet');
  for (const pid of enginePids()) log(`=== py-spy dump pid ${pid} ===\n` + ps(`py-spy dump --pid ${pid}`));
}

async function probe() {
  const {app, win, projectRoot} = await setup();
  await win.click('button:has-text("Generate Edit")');
  const {job, stalled} = await watch(win, (j) => !['queued', 'running', 'cancelling'].includes(j.status), LIMIT_MS);
  log('RESULT:', stalled ? 'STALLED' : 'FINISHED', job ? job.status : '(no job)', job ? job.message : '');
  await dumpDiagnostics(projectRoot, job);
  if (job && job.status === 'running') {
    const before = enginePids();
    await win.click('.job-card button:has-text("Cancel")');
    await win.waitForTimeout(20000);
    const after = enginePids();
    const status = await win.locator('.job-head strong').innerText().catch(() => '?');
    log(`CANCEL: UI status now "${status}"; engine processes before=${before.join(',')} after=${after.join(',')}`);
    log('=== process tree after Cancel ===\n' + processTree());
  }
  await app.close().catch(() => {});
}

async function verify() {
  // 1. The same scenario must finish, with stage progress visible in the UI.
  let {app, win, projectRoot} = await setup();
  const baseline = new Set(enginePids());
  await win.click('button:has-text("Generate Edit")');
  const stagesShown = new Set();
  const poll = setInterval(async () => {
    const text = await win.locator('.job-stage').innerText().catch(() => '');
    if (text) stagesShown.add(text.replace(/\s+/g, ' ').trim());
  }, 700);
  const {job, stalled} = await watch(win, (j) => !['queued', 'running', 'cancelling'].includes(j.status), LIMIT_MS);
  clearInterval(poll);
  log('stage labels seen in the UI:', JSON.stringify([...stagesShown]));
  if (stalled || !job || job.status !== 'prepared') { await dumpDiagnostics(projectRoot, job); throw new Error(`generation did not finish: ${job && job.status}`); }
  if (stagesShown.size < 3) throw new Error('UI did not show stage progress');
  await win.waitForSelector('.player-wrap', {timeout: 60000});
  log('preview player visible after generation');
  await app.close();

  // 2. Cancel must really stop the job: no engine child left, UI shows cancelled.
  ({app, win, projectRoot} = await setup());
  const serviceOnly = new Set(enginePids());
  await win.click('button:has-text("Generate Edit")');
  await win.waitForSelector('.job-card button:has-text("Cancel")', {timeout: 60000});
  await win.waitForFunction(() => window.__events.some((x) => x.e.event === 'job.log'), null, {timeout: 120000});
  const during = enginePids();
  log('engine processes while running:', during.join(','), '(service only before start:', [...serviceOnly].join(','), ')');
  await win.click('.job-card button:has-text("Cancel")');
  await win.waitForFunction(() => window.__events.some((x) => x.e.event === 'job.status' && x.e.params.job.status === 'cancelled'), null, {timeout: 30000});
  await win.waitForTimeout(3000);
  const leftover = enginePids().filter((pid) => !serviceOnly.has(pid));
  const status = (await win.locator('.job-head strong').innerText()).trim();
  log(`after Cancel: UI status "${status}", engine child processes left: ${leftover.length ? leftover.join(',') : 'none'}`);
  if (leftover.length) { log(processTree()); throw new Error('Cancel left engine processes running'); }
  if (!/cancel/i.test(status)) throw new Error('UI does not show cancelled');
  await app.close();
  log('VERIFY PASSED');
}

(mode === 'probe' ? probe() : verify()).catch((e) => { console.error('[e2e] FAILED:', (e && e.stack) || e); process.exit(1); });
