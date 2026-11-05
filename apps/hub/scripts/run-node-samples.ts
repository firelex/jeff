// Runs the site's Node code, so every Node sample a page shows is code a check has run (spec 3.2). Run with tsx, from
// apps/hub: `pnpm exec tsx scripts/run-node-samples.ts <mode>`. scripts/hub_examples.py calls it (--check runs
// `remote`, --check-local runs `local`); the ordinary test run calls `offline` (test/sdk-coverage.test.ts).
//
//   offline         typechecks every Node sample file (content/snippets/sdk/*.mjs, content/home-example*.mjs) against
//                   jeff-sdk's type declarations, so a local or remote sample that cannot run here still calls only
//                   what the SDK has; then runs the snippets marked `// offline`: no server, no model.
//   remote          every Node snippet with no marker, the home page's content/home-example.mjs, and the Node code of
//                   every recorded worked example (examples.server.ts: exampleCode, exactly what the pages show),
//                   against the server at JEFF_HUB_TEST_URL with the key in JEFF_API_KEY (both read from the
//                   environment, never from the command line; the key is never printed). A worked example's printed
//                   value must be its recorded value, and its confidence within 0.02 of the recorded one.
//   local <socket>  the Node snippets marked `// local`, content/home-example-local.mjs and every worked example's
//                   "On your machine" Node code (local-examples.ts), attached to the engine listening on <socket>
//                   (each sample's /tmp/jeff.sock is replaced by it; the engine must serve every adapter
//                   local-examples.ts lists). A worked example's printed value must be its recorded value; its
//                   confidence is not compared (the recorded one is the live server's, on another machine).
//
// Each sample runs as its own `node` process, from a temporary copy of the snippets folder (a sample may read a file
// beside it, such as graphs-message-check.yaml) where `jeff-sdk` resolves to packages/node, built first. Every
// failure is listed, naming the sample, and the run exits 1.
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { exampleCode, loadExamples } from '../app/lib/examples.server';
import { HUB_ROOT, REPO_ROOT } from '../app/lib/paths.server';
import { localCode, localExamples } from './local-examples';

const SNIPPETS = path.join(HUB_ROOT, 'content/snippets/sdk');
const CONFIDENCE_TOLERANCE = 0.02;
const ENGINE_SOCKET_IN_SAMPLES = '/tmp/jeff.sock';

type Marker = 'offline' | 'local' | 'remote';
interface Sample { name: string; code: string; check?: (stdout: string) => string | null }

function required(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === '') throw new Error(`Set ${name} in the environment`);
  return value;
}

function markerOf(code: string): Marker {
  if (code.startsWith('// offline\n')) return 'offline';
  if (code.startsWith('// local\n')) return 'local';
  return 'remote';
}

function snippets(marker: Marker): Sample[] {
  return fs.readdirSync(SNIPPETS).filter((f) => f.endsWith('.mjs')).sort()
    .map((f) => ({ name: `content/snippets/sdk/${f}`, code: fs.readFileSync(path.join(SNIPPETS, f), 'utf8') }))
    .filter((s) => markerOf(s.code) === marker);
}

// A worked example prints `console.log(decision.value, decision.confidence)`: the value, then the confidence (a
// number, or null).
// `withConfidence`: whether the confidence is compared too (only against the live server).
function answerCheck(recorded: { value: boolean | number | string; confidence: number | null }, withConfidence: boolean) {
  return (stdout: string): string | null => {
    const line = stdout.trim().split('\n').at(-1) ?? '';
    const cut = line.lastIndexOf(' ');
    if (cut === -1) return `printed ${JSON.stringify(line)}, not a value and a confidence`;
    const [value, confidence] = [line.slice(0, cut), line.slice(cut + 1)];
    const problems: string[] = [];
    if (value !== String(recorded.value)) problems.push(`value: recorded ${String(recorded.value)}, now ${value}`);
    if (!withConfidence) return problems.length === 0 ? null : problems.join('; ');
    if (recorded.confidence === null ? confidence !== 'null'
      : confidence === 'null' || Math.round(Math.abs(Number(confidence) - recorded.confidence) * 1e6) / 1e6 > CONFIDENCE_TOLERANCE) {
      problems.push(`confidence: recorded ${recorded.confidence}, now ${confidence} (tolerance ${CONFIDENCE_TOLERANCE})`);
    }
    return problems.length === 0 ? null : problems.join('; ');
  };
}

function remoteSamples(url: string): Sample[] {
  const home = fs.readFileSync(path.join(HUB_ROOT, 'content/home-example.mjs'), 'utf8');
  const examples = [...loadExamples(path.join(HUB_ROOT, 'content/examples')).values()];
  const unrecorded = examples.filter((e) => !e.offline && e.recorded === null).map((e) => e.id);
  if (unrecorded.length > 0) throw new Error(`Examples with no recorded answer (run scripts/hub_examples.py --record): ${unrecorded.join(', ')}`);
  return [
    ...snippets('remote'),
    { name: 'content/home-example.mjs', code: home },
    ...examples.filter((e) => !e.offline).map((e) => {
      const node = exampleCode(e, 'node', url);
      if (node === null) throw new Error(`example ${e.id}: no Node code, though it is not offline`);
      return { name: `example ${e.id} (Node)`, code: node, check: answerCheck(e.recorded!, true) };
    }),
  ];
}

// A folder where `import ... from "jeff-sdk"` resolves to packages/node, holding a copy of the snippets folder.
function workFolder(): string {
  execFileSync('pnpm', ['--filter', 'jeff-sdk', 'build'], { cwd: REPO_ROOT, stdio: 'pipe' });
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-node-samples-'));
  fs.cpSync(SNIPPETS, work, { recursive: true });
  fs.mkdirSync(path.join(work, 'node_modules'));
  fs.symlinkSync(path.join(REPO_ROOT, 'packages/node'), path.join(work, 'node_modules/jeff-sdk'), 'dir');
  return work;
}

// tsc over every .mjs sample, as JavaScript checked against packages/node's declarations (dist/index.d.ts, built by
// workFolder first). Not strict: the samples are plain JavaScript, with no type annotations.
function typecheck(work: string): string[] {
  const files = [
    ...fs.readdirSync(SNIPPETS).filter((f) => f.endsWith('.mjs')).map((f) => path.join(SNIPPETS, f)),
    ...fs.readdirSync(path.join(HUB_ROOT, 'content')).filter((f) => f.endsWith('.mjs')).map((f) => path.join(HUB_ROOT, 'content', f)),
  ];
  const config = path.join(work, 'tsconfig.json');
  fs.writeFileSync(config, JSON.stringify({
    compilerOptions: {
      allowJs: true, checkJs: true, noEmit: true, strict: false, skipLibCheck: true, target: 'ES2022', module: 'ESNext',
      moduleResolution: 'bundler', lib: ['ES2022', 'DOM'], types: ['node'], typeRoots: [path.join(HUB_ROOT, 'node_modules/@types')],
      paths: { 'jeff-sdk': [path.join(REPO_ROOT, 'packages/node/dist/index.d.ts')] },
    },
    files,
  }));
  const tsc = spawnSync(path.join(HUB_ROOT, 'node_modules/.bin/tsc'), ['-p', config], { encoding: 'utf8' });
  if (tsc.status === 0) {
    console.log(`ok   typecheck of ${files.length} Node sample files`);
    return [];
  }
  return [`typecheck of the Node samples failed:\n${tsc.stdout}${tsc.stderr}`];
}

function run(samples: Sample[], substitute: (code: string) => string, env: NodeJS.ProcessEnv, check: boolean): string[] {
  if (samples.length === 0) throw new Error('No samples to run');
  const work = workFolder();
  const failures: string[] = [];
  try {
    if (check) failures.push(...typecheck(work));
    samples.forEach((sample, i) => {
      const file = path.join(work, `sample-${i}.mjs`);
      fs.writeFileSync(file, substitute(sample.code));
      const result = spawnSync(process.execPath, [file], { cwd: work, env, encoding: 'utf8', timeout: 120_000 });
      if (result.error !== undefined) {
        failures.push(`${sample.name}: could not run: ${result.error.message}`);
      } else if (result.status !== 0) {
        // The thrown error's own line (Node prints the failing source line and a caret above it), else the last lines.
        const lines = result.stderr.trim().split('\n');
        const thrown = lines.find((line) => /^\w*Error\b/.test(line));
        failures.push(`${sample.name}: exited with ${result.status ?? result.signal}: ${thrown ?? lines.slice(-6).join(' | ')}`);
      } else {
        const problem = sample.check?.(result.stdout) ?? null;
        if (problem !== null) failures.push(`${sample.name}: ${problem}`);
        else console.log(`ok   ${sample.name}`);
      }
    });
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
  return failures;
}

function main(argv: string[]): void {
  const [mode, socket] = argv;
  let samples: Sample[];
  let substitute: (code: string) => string;
  // The samples get only what they need from the environment: PATH and HOME for node itself, and the key.
  const env: NodeJS.ProcessEnv = { PATH: required('PATH'), HOME: required('HOME') };
  if (mode === 'offline') {
    samples = snippets('offline');
    substitute = (code) => code;
  } else if (mode === 'remote') {
    const url = required('JEFF_HUB_TEST_URL').replace(/\/$/, '');
    env.JEFF_API_KEY = required('JEFF_API_KEY');
    samples = remoteSamples(url);
    substitute = (code) => code.replaceAll('{{origin}}', url);
  } else if (mode === 'local') {
    if (socket === undefined) throw new Error('Usage: run-node-samples.ts local <engine socket>');
    samples = [
      ...snippets('local'),
      { name: 'content/home-example-local.mjs', code: fs.readFileSync(path.join(HUB_ROOT, 'content/home-example-local.mjs'), 'utf8') },
      ...localExamples().map((l) => ({ name: `example ${l.example.id} (Node, local)`, code: localCode(l, 'node'), check: answerCheck(l.example.recorded!, false) })),
    ];
    const missing = samples.filter((s) => !s.code.includes(ENGINE_SOCKET_IN_SAMPLES)).map((s) => s.name);
    if (missing.length > 0) throw new Error(`Local samples that do not name ${ENGINE_SOCKET_IN_SAMPLES}: ${missing.join(', ')}`);
    substitute = (code) => code.replaceAll(ENGINE_SOCKET_IN_SAMPLES, socket);
  } else {
    throw new Error('Usage: run-node-samples.ts offline | remote | local <engine socket>');
  }
  const failures = run(samples, substitute, env, mode === 'offline');
  if (failures.length > 0) {
    console.error(`${failures.length} of ${samples.length} Node samples failed:\n${failures.join('\n')}`);
    process.exit(1);
  }
  console.log(`All ${samples.length} Node samples ran (${mode}).`);
}

main(process.argv.slice(2));
