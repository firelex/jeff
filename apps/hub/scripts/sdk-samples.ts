// Builds and runs the site's Rust, C# and Java code against each SDK's own source, so a tab the site turns on
// (content/sdk-languages.yaml) shows only code a check has compiled and run. Run with tsx, from apps/hub:
// `pnpm exec tsx scripts/sdk-samples.ts <mode> [languages]`. Not part of the ordinary test run: it needs each SDK's
// source tree (SDK_TREES below) and its toolchain.
//
//   compile [rust|csharp|java ...]   every snippet the site can show in those languages (all three when none is
//                                    named), whether or not the language is on: the code of every worked example
//                                    through the API (examples.server.ts: exampleCode, exactly what a page would
//                                    show) and on the reader's machine (local-examples.ts: every open adapter's
//                                    recorded example, its "On your machine" code), the home page's two files
//                                    (content/home-example{,-local}.<ext>), and every SDK docs snippet in those
//                                    languages (content/snippets/sdk/*.<ext>: <name>.local.<ext> on the reader's
//                                    machine, the others through the API or offline). Rust: cargo check;
//                                    C#: dotnet build; Java: javac (--release 21). Every snippet that does not
//                                    compile is listed with the compiler's errors, and the run exits 1.
//   remote <languages>               builds and runs, against the server at JEFF_HUB_TEST_URL with the key in
//                                    JEFF_API_KEY (both read from the environment, never from the command line; the
//                                    key is never printed), every worked example's code, home-example.<ext> and
//                                    every docs snippet that is not local (the offline ones need no server, and run
//                                    here too). A worked example's printed value must be its recorded value, and its
//                                    confidence within 0.02 of the recorded one; a snippet must exit with status 0. scripts/hub_examples.py --check runs this for
//                                    the languages that are on.
//   local <languages>                builds and runs home-example-local.<ext> and every worked example's local code
//                                    and every local docs snippet. Each calls local mode with its adapters, so it
//                                    uses the engine listening in this user's engine folder (contract/
//                                    local-engine.md: engine.sock in the cache folder's jeff/engine), which the
//                                    caller has started serving every adapter local-examples.ts lists, and the
//                                    guard, triage and spam (a sample that names an engine socket is refused). A
//                                    worked example's printed value must be its recorded value; its confidence is
//                                    not compared (the recorded one is the live server's, on another machine and
//                                    backend). scripts/hub_examples.py --check-local runs this for the languages
//                                    that are on.
//
// Each SDK is used from its source tree, read only: Rust through a path dependency (built into this run's own target
// folder), C# from a copy of the package's project, Java compiled from its sources into this run's folder (with the
// jars its gradle.lockfile names, from the Gradle cache). Each sample runs in a folder holding the snippets folder's
// data files (graphs-message-check.yaml), which a snippet reads by its bare name. The work folder is $TMPDIR/jeff-hub-sdk-samples, or
// JEFF_HUB_SDK_SAMPLES_WORK when set; it is kept between runs, so a second build is incremental.
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { exampleCode, loadExamples, type Example } from '../app/lib/examples.server';
import { HUB_ROOT, REPO_ROOT } from '../app/lib/paths.server';
import { localCode, localExamples } from './local-examples';

type Language = 'rust' | 'csharp' | 'java';
type Mode = 'local' | 'remote';
const LANGUAGES: Language[] = ['rust', 'csharp', 'java'];
const EXTENSIONS: Record<Language, string> = { rust: 'rs', csharp: 'cs', java: 'java' };
const CONFIDENCE_TOLERANCE = 0.02;
// The socket the docs name for an engine started by hand: no local sample may use it, since local mode with adapters
// is what the pages show.
const ENGINE_SOCKET_IN_DOCS = '/tmp/jeff.sock';
// The origin compiled into the snippets when nothing runs them.
const COMPILE_ORIGIN = 'https://api.example.com';
const SNIPPETS = path.join(HUB_ROOT, 'content/snippets/sdk');

// Where each SDK's source is, for the local mode (the engine socket client) and the remote mode (HTTP: the remote
// client and RestTransport). Rust, C# and Java are all merged into this repository (packages/rust, packages/dotnet,
// packages/java). Set JEFF_SDK_<LANGUAGE>_<MODE> (for example JEFF_SDK_JAVA_REMOTE) to an SDK package folder to use
// another.
const SDK_TREES: Record<Language, Record<Mode, string>> = {
  rust: { local: path.join(REPO_ROOT, 'packages/rust'), remote: path.join(REPO_ROOT, 'packages/rust') },
  csharp: { local: path.join(REPO_ROOT, 'packages/dotnet'), remote: path.join(REPO_ROOT, 'packages/dotnet') },
  java: { local: path.join(REPO_ROOT, 'packages/java'), remote: path.join(REPO_ROOT, 'packages/java') },
};

const RUSTUP_BIN = '/opt/homebrew/opt/rustup/bin';
const JAVA_HOME = '/opt/homebrew/opt/openjdk@27/libexec/openjdk.jdk/Contents/Home';

interface Recorded { value: boolean | number | string; confidence: number | null }
// confidence: whether a run compares the printed confidence with the recorded one (only against the live server).
interface Sample { name: string; language: Language; mode: Mode; code: string; recorded: Recorded | null; confidence: boolean }

function required(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === '') throw new Error(`Set ${name} in the environment`);
  return value;
}

function sdkTree(language: Language, mode: Mode): string {
  const variable = `JEFF_SDK_${language.toUpperCase()}_${mode.toUpperCase()}`;
  const tree = process.env[variable] ?? SDK_TREES[language][mode];
  if (!fs.existsSync(tree)) throw new Error(`No ${language} SDK for ${mode} mode at ${tree} (set ${variable} to its package folder)`);
  return tree;
}

function homeSample(language: Language, mode: Mode, origin: string): Sample {
  const file = `content/${mode === 'local' ? 'home-example-local' : 'home-example'}.${EXTENSIONS[language]}`;
  const code = fs.readFileSync(path.join(HUB_ROOT, file), 'utf8').replaceAll('{{origin}}', origin);
  return { name: file, language, mode, code, recorded: null, confidence: false };
}

function exampleSamples(language: Language, origin: string): Sample[] {
  const examples = [...loadExamples(path.join(HUB_ROOT, 'content/examples')).values()].filter((e) => !e.offline);
  return examples.map((e: Example) => {
    const code = exampleCode(e, language, origin);
    if (code === null) throw new Error(`example ${e.id}: no ${language} code, though it is not offline`);
    return { name: `example ${e.id} (${language})`, language, mode: 'remote' as const, code, recorded: e.recorded, confidence: true };
  });
}

// Every worked example's "On your machine" code in `language` (local-examples.ts), checked by its recorded value.
function localExampleSamples(language: Language): Sample[] {
  return localExamples().map((l) => ({
    name: `example ${l.example.id} (${language}, local)`, language, mode: 'local' as const, code: localCode(l, language),
    recorded: l.example.recorded, confidence: false,
  }));
}

// Every SDK docs snippet in `language` that runs in `mode`: <name>.local.<ext> on the reader's machine, every other
// one through the API (an offline one, which needs neither, is built and run with them). Each is checked by its exit
// status only: it has no recorded answer.
function docSamples(language: Language, mode: Mode, origin: string): Sample[] {
  const extension = `.${EXTENSIONS[language]}`;
  return fs.readdirSync(SNIPPETS).filter((f) => f.endsWith(extension)).sort()
    .filter((f) => f.endsWith(`.local${extension}`) === (mode === 'local'))
    .map((f) => ({
      name: `content/snippets/sdk/${f}`, language, mode, recorded: null, confidence: false,
      code: fs.readFileSync(path.join(SNIPPETS, f), 'utf8').replaceAll('{{origin}}', origin),
    }));
}

// The snippets folder's data files (graphs-message-check.yaml), copied into `folder`, where the samples run.
function copySnippetData(folder: string): void {
  for (const f of fs.readdirSync(SNIPPETS).filter((name) => name.endsWith('.yaml'))) fs.copyFileSync(path.join(SNIPPETS, f), path.join(folder, f));
}

function workRoot(): string {
  const root = process.env.JEFF_HUB_SDK_SAMPLES_WORK ?? path.join(os.tmpdir(), 'jeff-hub-sdk-samples');
  fs.mkdirSync(root, { recursive: true });
  return root;
}

interface Result { status: number | null; output: string }

// Runs a command to the end, collecting its output (stdout and stderr together); never throws for a failing command.
function run(command: string, args: string[], options: { cwd: string; env?: NodeJS.ProcessEnv; timeoutMs?: number }): Promise<Result> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: options.cwd, env: options.env ?? process.env });
    let output = '';
    child.stdout.on('data', (d: Buffer) => { output += d.toString(); });
    child.stderr.on('data', (d: Buffer) => { output += d.toString(); });
    const timer = setTimeout(() => child.kill('SIGKILL'), options.timeoutMs ?? 1_800_000);
    child.on('error', reject);
    child.on('close', (status) => {
      clearTimeout(timer);
      resolve({ status, output });
    });
  });
}

// Runs `jobs` with at most `limit` at a time, in order of their results.
async function pool<T>(jobs: (() => Promise<T>)[], limit: number): Promise<T[]> {
  const results: T[] = new Array(jobs.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, jobs.length) }, async () => {
    while (next < jobs.length) {
      const i = next++;
      results[i] = await jobs[i]();
    }
  }));
  return results;
}

// One built group of samples: per sample, null when it built, else the compiler's errors for it; and how to run it.
interface Built {
  problems: (string | null)[];
  command: (i: number) => { command: string; args: string[]; cwd: string };
}

// The compiler's error and warning lines that name `file` (a sample's source): a sample must compile cleanly.
function errorsFor(output: string, file: string): string | null {
  const picked: string[] = [];
  for (const line of output.split('\n')) {
    if (line.includes(file) && /\b(error|warning)\b/.test(line)) picked.push(line.trim());
  }
  return picked.length === 0 ? null : [...new Set(picked)].join('\n      ');
}

// ---------------------------------------------------------------------------------------------------------- Rust

async function buildRust(samples: Sample[], tree: string, work: string, check: boolean): Promise<Built> {
  const dir = path.join(work, 'project');
  fs.rmSync(path.join(dir, 'src'), { recursive: true, force: true });
  fs.mkdirSync(path.join(dir, 'src/bin'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'Cargo.toml'), [
    '[package]', 'name = "hub-samples"', 'version = "0.0.0"', 'edition = "2024"', 'publish = false', '',
    '[dependencies]', `jeff-sdk = { path = ${JSON.stringify(tree)} }`, 'serde_json = "1"',
    'tokio = { version = "1", features = ["macros", "rt-multi-thread"] }',
    // The docs snippet that implements the Engine trait (content/snippets/sdk/engines-1.rs) needs it, as a reader's
    // own engine does.
    'async-trait = "0.1"', '',
  ].join('\n'));
  // The SDK's own lock, so the samples build with exactly the versions the SDK is tested with.
  if (!fs.existsSync(path.join(dir, 'Cargo.lock'))) fs.copyFileSync(path.join(tree, 'Cargo.lock'), path.join(dir, 'Cargo.lock'));
  fs.copyFileSync(path.join(tree, 'rust-toolchain.toml'), path.join(dir, 'rust-toolchain.toml'));
  samples.forEach((s, i) => fs.writeFileSync(path.join(dir, `src/bin/sample${i}.rs`), s.code));
  const env = { ...process.env, PATH: `${RUSTUP_BIN}:${required('PATH')}`, CARGO_TARGET_DIR: path.join(work, 'target') };
  const result = await run('cargo', [check ? 'check' : 'build', '--bins', '--message-format', 'short'], { cwd: dir, env });
  const problems = samples.map((_, i) => errorsFor(result.output, `src/bin/sample${i}.rs`));
  if (result.status !== 0 && problems.every((p) => p === null)) throw new Error(`cargo failed outside the samples:\n${result.output.slice(-3000)}`);
  return {
    problems,
    command: (i) => ({ command: path.join(work, 'target/debug', `sample${i}`), args: [], cwd: dir }),
  };
}

// ------------------------------------------------------------------------------------------------------------ C#

async function buildCsharp(samples: Sample[], tree: string, work: string): Promise<Built> {
  // A copy of the package (its project, the files it embeds, the build settings), so nothing is written in its tree.
  const sdk = path.join(work, 'sdk');
  fs.rmSync(sdk, { recursive: true, force: true });
  fs.mkdirSync(sdk, { recursive: true });
  for (const item of ['Directory.Build.props', 'Directory.Packages.props', 'shipped', 'src/Jeff.Sdk']) {
    fs.cpSync(path.join(tree, item), path.join(sdk, item), {
      recursive: true,
      filter: (source) => !['bin', 'obj'].includes(path.basename(source)),
    });
  }
  const projects: string[] = [];
  samples.forEach((s, i) => {
    const dir = path.join(sdk, 'samples', `Sample${i}`);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `Sample${i}.csproj`), [
      '<Project Sdk="Microsoft.NET.Sdk">',
      '  <PropertyGroup>',
      '    <OutputType>Exe</OutputType>',
      '    <TargetFramework>net10.0</TargetFramework>',
      '  </PropertyGroup>',
      '  <ItemGroup>',
      '    <ProjectReference Include="../../src/Jeff.Sdk/Jeff.Sdk.csproj" />',
      '  </ItemGroup>',
      '</Project>',
      '',
    ].join('\n'));
    fs.writeFileSync(path.join(dir, 'Program.cs'), s.code);
    projects.push(`samples/Sample${i}/Sample${i}.csproj`);
  });
  fs.writeFileSync(path.join(sdk, 'samples.slnx'),
    ['<Solution>', ...projects.map((p) => `  <Project Path="${p}" />`), '</Solution>', ''].join('\n'));
  const result = await run('dotnet', ['build', 'samples.slnx', '-nologo', '-v', 'q', '-clp:NoSummary'], { cwd: sdk });
  const problems = samples.map((_, i) => errorsFor(result.output, `Sample${i}/Program.cs`));
  if (result.status !== 0 && problems.every((p) => p === null)) throw new Error(`dotnet build failed outside the samples:\n${result.output.slice(-3000)}`);
  return {
    problems,
    command: (i) => ({ command: 'dotnet', args: [path.join(sdk, 'samples', `Sample${i}`, 'bin/Debug/net10.0', `Sample${i}.dll`)], cwd: sdk }),
  };
}

// ---------------------------------------------------------------------------------------------------------- Java

// The jars the package's gradle.lockfile puts on its runtime class path, from the Gradle cache (a BOM has none).
function javaJars(tree: string): string[] {
  const cache = path.join(required('HOME'), '.gradle/caches/modules-2/files-2.1');
  const jars: string[] = [];
  for (const line of fs.readFileSync(path.join(tree, 'gradle.lockfile'), 'utf8').split('\n')) {
    const match = /^([^#:]+):([^:]+):([^=]+)=(.*)$/.exec(line.trim());
    if (match === null || !match[4].split(',').includes('runtimeClasspath')) continue;
    const [, group, artifact, version] = match;
    if (artifact.endsWith('-bom')) continue;
    const dir = path.join(cache, group, artifact, version);
    const jar = fs.existsSync(dir)
      ? fs.readdirSync(dir).map((h) => path.join(dir, h, `${artifact}-${version}.jar`)).find((f) => fs.existsSync(f))
      : undefined;
    if (jar === undefined) throw new Error(`${group}:${artifact}:${version} (from ${tree}/gradle.lockfile) is not in the Gradle cache ${cache}; build the package once with ./gradlew`);
    jars.push(jar);
  }
  return jars;
}

function javaFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    return entry.isDirectory() ? javaFiles(full) : entry.name.endsWith('.java') ? [full] : [];
  });
}

// A Java sample's public class (Decide for a worked example; a docs snippet names its own), which names its file.
function javaClassOf(sample: Sample): string {
  const match = /^public (?:final )?class (\w+)/m.exec(sample.code);
  if (match === null) throw new Error(`${sample.name}: has no public class`);
  return match[1];
}

async function buildJava(samples: Sample[], tree: string, work: string): Promise<Built> {
  const javac = path.join(JAVA_HOME, 'bin/javac');
  const jars = javaJars(tree);
  const classes = path.join(work, 'sdk-classes');
  fs.rmSync(classes, { recursive: true, force: true });
  const sources = path.join(work, 'sdk-sources.txt');
  fs.writeFileSync(sources, javaFiles(path.join(tree, 'src/main/java')).join('\n'));
  const sdk = await run(javac, ['--release', '21', '-encoding', 'UTF-8', '-nowarn', '-d', classes, '-cp', jars.join(':'), `@${sources}`], { cwd: work });
  if (sdk.status !== 0) throw new Error(`The Java SDK at ${tree} does not compile:\n${sdk.output.slice(-3000)}`);
  const classPath = [classes, path.join(tree, 'src/main/resources'), ...jars];
  const problems = await pool(samples.map((s, i) => async () => {
    const dir = path.join(work, 'samples', `sample${i}`);
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(path.join(dir, 'out'), { recursive: true });
    fs.writeFileSync(path.join(dir, `${javaClassOf(s)}.java`), s.code);
    const result = await run(javac, ['--release', '21', '-encoding', 'UTF-8', '-Xlint:all', '-Werror', '-d', 'out', '-cp', classPath.join(':'), `${javaClassOf(s)}.java`], { cwd: dir });
    return result.status === 0 ? null : result.output.trim().split('\n').slice(0, 12).join('\n      ');
  }), Math.max(2, os.cpus().length - 2));
  return {
    problems,
    command: (i) => ({
      command: path.join(JAVA_HOME, 'bin/java'),
      args: ['-cp', [path.join(work, 'samples', `sample${i}`, 'out'), ...classPath].join(':'), javaClassOf(samples[i])],
      cwd: work,
    }),
  };
}

// ------------------------------------------------------------------------------------------------- the answers

// What a sample printed as its value and confidence (the last line): Rust prints both with {:?} (Str("x"),
// Bool(true), Int(3); Some(0.97) or None); C# prints them interpolated (an absent confidence as nothing); Java
// concatenates them (null for an absent confidence).
function printedAnswer(language: Language, stdout: string): { value: string; confidence: number | null } | string {
  const line = stdout.replace(/\n+$/, '').split('\n').at(-1) ?? '';
  if (language === 'rust') {
    const match = /^(?:Str\((".*")\)|Bool\((true|false)\)|Int\((-?\d+)\)) (?:Some\((.+)\)|(None))$/.exec(line);
    if (match === null) return `printed ${JSON.stringify(line)}, not a Rust value and confidence`;
    const value = match[1] !== undefined ? (JSON.parse(match[1]) as string) : (match[2] ?? match[3]);
    return { value, confidence: match[5] !== undefined ? null : Number(match[4]) };
  }
  const cut = line.lastIndexOf(' ');
  if (cut === -1) return `printed ${JSON.stringify(line)}, not a value and a confidence`;
  const [value, confidence] = [line.slice(0, cut), line.slice(cut + 1)];
  const absent = language === 'csharp' ? '' : 'null';
  if (confidence !== absent && !Number.isFinite(Number(confidence))) return `printed ${JSON.stringify(line)}: ${JSON.stringify(confidence)} is not a confidence`;
  return { value, confidence: confidence === absent ? null : Number(confidence) };
}

function answerProblem(sample: Sample, stdout: string): string | null {
  if (sample.recorded === null) return null;
  const printed = printedAnswer(sample.language, stdout);
  if (typeof printed === 'string') return printed;
  const { recorded } = sample;
  const problems: string[] = [];
  if (printed.value !== String(recorded.value)) problems.push(`value: recorded ${String(recorded.value)}, now ${printed.value}`);
  if (!sample.confidence) return problems.length === 0 ? null : problems.join('; ');
  if (recorded.confidence === null ? printed.confidence !== null
    : printed.confidence === null || Math.round(Math.abs(printed.confidence - recorded.confidence) * 1e6) / 1e6 > CONFIDENCE_TOLERANCE) {
    problems.push(`confidence: recorded ${recorded.confidence}, now ${printed.confidence} (tolerance ${CONFIDENCE_TOLERANCE})`);
  }
  return problems.length === 0 ? null : problems.join('; ');
}

// ------------------------------------------------------------------------------------------------------ the run

async function build(language: Language, mode: Mode, samples: Sample[], check: boolean): Promise<Built> {
  const tree = sdkTree(language, mode);
  const work = path.join(workRoot(), `${language}-${mode}`);
  fs.mkdirSync(work, { recursive: true });
  console.log(`build ${samples.length} ${language} samples (${mode} mode) against ${tree}`);
  if (language === 'rust') return buildRust(samples, tree, work, check);
  if (language === 'csharp') return buildCsharp(samples, tree, work);
  return buildJava(samples, tree, work);
}

async function compileAll(languages: Language[]): Promise<string[]> {
  const failures: string[] = [];
  for (const language of languages) {
    const groups: [Mode, Sample[]][] = [
      ['local', [homeSample(language, 'local', COMPILE_ORIGIN), ...localExampleSamples(language), ...docSamples(language, 'local', COMPILE_ORIGIN)]],
      ['remote', [homeSample(language, 'remote', COMPILE_ORIGIN), ...exampleSamples(language, COMPILE_ORIGIN), ...docSamples(language, 'remote', COMPILE_ORIGIN)]],
    ];
    for (const [mode, samples] of groups) {
      const built = await build(language, mode, samples, true);
      let ok = 0;
      built.problems.forEach((problem, i) => {
        if (problem === null) ok += 1;
        else failures.push(`${samples[i].name} (${mode} mode): does not compile:\n      ${problem}`);
      });
      console.log(`${ok === samples.length ? 'ok  ' : 'FAIL'} ${language} ${mode}: ${ok} of ${samples.length} compile`);
    }
  }
  return failures;
}

async function runAll(languages: Language[], mode: Mode, samplesOf: (language: Language) => Sample[], env: NodeJS.ProcessEnv): Promise<string[]> {
  const failures: string[] = [];
  for (const language of languages) {
    const samples = samplesOf(language);
    const built = await build(language, mode, samples, false);
    copySnippetData(built.command(0).cwd);
    for (let i = 0; i < samples.length; i++) {
      const sample = samples[i];
      if (built.problems[i] !== null) {
        failures.push(`${sample.name}: does not compile:\n      ${built.problems[i]}`);
        continue;
      }
      const { command, args, cwd } = built.command(i);
      const result = spawnSync(command, args, { cwd, env, encoding: 'utf8', timeout: 120_000 });
      if (result.error !== undefined) failures.push(`${sample.name}: could not run: ${result.error.message}`);
      else if (result.status !== 0) {
        failures.push(`${sample.name}: exited with ${result.status ?? result.signal}: ${`${result.stdout}${result.stderr}`.trim().split('\n').slice(-6).join(' | ')}`);
      } else {
        const problem = answerProblem(sample, result.stdout);
        if (problem !== null) failures.push(`${sample.name}: ${problem}`);
        else console.log(`ok   ${sample.name}`);
      }
    }
  }
  return failures;
}

function languagesOf(args: string[], usage: string): Language[] {
  const unknown = args.filter((a) => !(LANGUAGES as string[]).includes(a));
  if (unknown.length > 0) throw new Error(`Unknown languages ${unknown.join(', ')}; ${usage}`);
  return args as Language[];
}

async function main(argv: string[]): Promise<void> {
  const usage = 'usage: sdk-samples.ts compile [rust|csharp|java ...] | remote <languages> | local <languages>';
  const [mode, ...rest] = argv;
  let failures: string[];
  let count: string;
  if (mode === 'compile') {
    const languages = rest.length === 0 ? LANGUAGES : languagesOf(rest, usage);
    failures = await compileAll(languages);
    count = `every ${languages.join(', ')} snippet compiles`;
  } else if (mode === 'remote') {
    const languages = languagesOf(rest, usage);
    if (languages.length === 0) throw new Error(`Name the languages to run; ${usage}`);
    const url = required('JEFF_HUB_TEST_URL').replace(/\/$/, '');
    // The samples get only what they need from the environment: PATH and HOME, and the key.
    const env = { PATH: required('PATH'), HOME: required('HOME'), JEFF_API_KEY: required('JEFF_API_KEY') };
    failures = await runAll(languages, 'remote', (language) => [homeSample(language, 'remote', url), ...exampleSamples(language, url), ...docSamples(language, 'remote', url)], env);
    count = `every ${languages.join(', ')} sample ran against ${url}, each worked example giving its recorded answer`;
  } else if (mode === 'local') {
    const languages = languagesOf(rest, usage);
    if (languages.length === 0) throw new Error(`Name the languages to run; ${usage}`);
    // PATH and HOME, and XDG_CACHE_HOME when it is set: on Linux it names the engine folder local mode looks in.
    const env: NodeJS.ProcessEnv = { PATH: required('PATH'), HOME: required('HOME') };
    if (process.env.XDG_CACHE_HOME !== undefined) env.XDG_CACHE_HOME = process.env.XDG_CACHE_HOME;
    failures = await runAll(languages, 'local', (language) => [homeSample(language, 'local', COMPILE_ORIGIN), ...localExampleSamples(language), ...docSamples(language, 'local', COMPILE_ORIGIN)].map((sample) => {
      if (sample.code.includes(ENGINE_SOCKET_IN_DOCS)) throw new Error(`${sample.name} names the socket ${ENGINE_SOCKET_IN_DOCS} instead of its adapters`);
      return sample;
    }), env);
    count = `every ${languages.join(', ')} local sample ran against the engine in this user's engine folder, each worked example giving its recorded value`;
  } else {
    throw new Error(usage);
  }
  if (failures.length > 0) {
    console.error(`${failures.length} SDK samples failed:\n${failures.join('\n')}`);
    process.exit(1);
  }
  console.log(`All done: ${count}.`);
}

await main(process.argv.slice(2));
