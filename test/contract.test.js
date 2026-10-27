// Workflow launch-contract guard for .claude/workflows/research-batch.js.
//
// These checks assert the Workflow-RUNTIME contract that `node -c` and ordinary
// unit tests structurally cannot (the dossier launch-bug class): zero
// import/require, no export but the leading `export const meta`, a top-level
// run() dispatch, compiles as an async-fn body, and a pure-literal meta. They
// do NOT replace the launch-smoke (Phase 5) — only a real launch proves bugs of
// the args-as-string / agent()-input class — but they catch everything
// mechanical before that. Plus: the inlined confidence/schemas must still match
// lib/ (the no-bundler sync guard).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const WF = join(ROOT, '.claude', 'workflows', 'research-batch.js');

const stripExports = (s) =>
  s.replace(/^export\s+(?=(async\s+)?(const|let|var|function|class)\b)/gm, '').trimEnd();

const lines = (src) => src.split(/\r?\n/).map((line, i) => ({ line, n: i + 1 }));

// Normalize CRLF -> LF so the inline-sync marker regex (matches `>>>\n`) and the
// block comparison work regardless of git's working-copy line endings.
const norm = (s) => s.replace(/\r\n/g, '\n');

test('workflow file exists', () => {
  assert.ok(existsSync(WF), `expected workflow at ${WF}`);
});

test('declares a single `export const meta`', () => {
  const src = readFileSync(WF, 'utf8');
  assert.match(src, /export\s+const\s+meta\b/, 'must export the workflow meta');
});

test('Guard 1: zero import/require (no module/filesystem access at runtime)', () => {
  const src = readFileSync(WF, 'utf8');
  const offenders = lines(src).filter(
    ({ line }) => /^\s*import\s/.test(line) || /\brequire\s*\(/.test(line),
  );
  assert.equal(offenders.length, 0, `found:\n${offenders.map((o) => `  L${o.n}: ${o.line.trim()}`).join('\n')}`);
});

test('Guard 2: no `export` other than the leading `export const meta`', () => {
  const src = readFileSync(WF, 'utf8');
  const offenders = lines(src).filter(
    ({ line }) => /^\s*export\s/.test(line) && !/^\s*export\s+const\s+meta\b/.test(line),
  );
  assert.equal(offenders.length, 0, `found:\n${offenders.map((o) => `  L${o.n}: ${o.line.trim()}`).join('\n')}`);
});

test('Guard 3: a top-level entry dispatches run(args)', () => {
  const src = readFileSync(WF, 'utf8');
  assert.match(src, /return\s+await\s+run\s*\(/, 'must invoke run(args) at the top level');
  // and must normalize the JSON-string args form
  assert.match(src, /typeof\s+args\s*===\s*'string'\s*\?\s*JSON\.parse\(\s*args\s*\)/, 'must normalize string args');
});

test('Guard 4: compiles as an async function body (meta parsed out)', () => {
  const src = readFileSync(WF, 'utf8');
  const AsyncFunction = (async () => {}).constructor;
  assert.doesNotThrow(() => {
    // eslint-disable-next-line no-new-func
    new AsyncFunction('args', src.replace(/^export\s+const\s+meta\b/m, 'const meta'));
  }, 'workflow must compile as a Workflow body (async fn, top-level await/return legal)');
});

test('Guard 5: meta is a pure literal (no concat / template / interpolation)', () => {
  const src = readFileSync(WF, 'utf8');
  const start = src.indexOf('export const meta');
  const end = src.indexOf('\n};', start);
  assert.ok(start !== -1 && end !== -1, 'could not locate the meta block');
  const metaBlock = src.slice(start, end).replace(/\/\/[^\n]*/g, '');
  const problems = [];
  if (/`/.test(metaBlock)) problems.push('template literal (backtick)');
  if (/\$\{/.test(metaBlock)) problems.push('interpolation ${');
  if (/['"]\s*\+/.test(metaBlock)) problems.push('string concatenation (+)');
  assert.equal(problems.length, 0, `meta must be a pure literal — found ${problems.join(', ')}`);
});

test('inline-sync: workflow confidence block matches lib/confidence.js', () => {
  const src = norm(readFileSync(WF, 'utf8'));
  const m = src.match(
    /\/\/ >>> BEGIN inlined lib\/confidence\.js[^\n]*>>>\n([\s\S]*?)\n\/\/ <<< END inlined lib\/confidence\.js <<</,
  );
  assert.ok(m, 'confidence inline markers not found');
  const expected = norm(stripExports(readFileSync(join(ROOT, 'lib', 'confidence.js'), 'utf8')));
  assert.equal(m[1].trim(), expected.trim(), 'inlined confidence drifted from lib/confidence.js — run `node tools/reinline-workflow.mjs`');
});

test('inline-sync: workflow schemas block matches lib/schemas.js', () => {
  const src = norm(readFileSync(WF, 'utf8'));
  const m = src.match(
    /\/\/ >>> BEGIN inlined lib\/schemas\.js[^\n]*>>>\n([\s\S]*?)\n\/\/ <<< END inlined lib\/schemas\.js <<</,
  );
  assert.ok(m, 'schemas inline markers not found');
  const expected = norm(stripExports(readFileSync(join(ROOT, 'lib', 'schemas.js'), 'utf8')));
  assert.equal(m[1].trim(), expected.trim(), 'inlined schemas drifted from lib/schemas.js — run `node tools/reinline-workflow.mjs`');
});

test('manual_urls: runner schema accepts them and the workflow surfaces them', async () => {
  const { SUMMARIES } = await import('../lib/schemas.js');
  const mu = SUMMARIES.properties.manual_urls;
  assert.equal(mu?.type, 'array', 'SUMMARIES must declare manual_urls');
  assert.deepEqual(mu.items.required, ['url']);
  const src = norm(readFileSync(WF, 'utf8'));
  assert.match(src, /summaryResult\?\.manual_urls/, 'ingestHopResults must collect manual_urls');
  assert.match(src, /manual_urls:\s*research\.manual_urls/, 'run() result must carry manual_urls');
});

test('manual_urls: the no-sources abort carries them through run()', () => {
  const src = norm(readFileSync(WF, 'utf8'));
  assert.match(src, /aborted: true, reason: 'no_sources', manual_urls: collectManualUrls\(topics\)/);
  const m = src.match(/if \(research\.aborted\) \{[\s\S]*?\n  \}/);
  assert.ok(m && /manual_urls: research\.manual_urls \?\? \[\]/.test(m[0]), 'aborted result must include manual_urls');
});

test('workflow file is LF-only (Workflow scriptPath rejects CRLF)', () => {
  assert.ok(!readFileSync(WF, 'utf8').includes('\r'));
});
