#!/usr/bin/env node
// The build's version of the hub, written into the function's generated content (app/lib/hub-version.generated.ts,
// which /api/health reports; final review I8). The function reads no setting and no file for it.
// - On Vercel (VERCEL=1): the commit Vercel builds, VERCEL_GIT_COMMIT_SHA. Vercel sets it for a deployment from the
//   Git integration; a build without it (a `vercel deploy` from a folder, which Vercel gives no commit) is refused,
//   naming it.
// - Anywhere else: `git rev-parse --short HEAD` of this checkout; a failure of git is an error saying so.
// The file is generated, never committed (.gitignore): `pnpm build`, `pnpm test` and `pnpm typecheck` write it first.
//
// Usage (in apps/hub):  node scripts/hub-version.mjs
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HUB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const VERSION_FILE = 'app/lib/hub-version.generated.ts';

export function hubVersion(env, cwd) {
  if (env.VERCEL === '1') {
    const sha = env.VERCEL_GIT_COMMIT_SHA;
    if (sha === undefined || sha === '') {
      throw new Error('VERCEL_GIT_COMMIT_SHA is not set: the hub\'s version is the commit Vercel builds, which Vercel sets only '
        + 'for a deployment from the Git integration; deploy through it (a push), not with `vercel deploy` from a folder');
    }
    if (!/^[0-9a-f]{7,40}$/.test(sha)) throw new Error(`VERCEL_GIT_COMMIT_SHA=${JSON.stringify(sha)} is not a git commit`);
    return sha;
  }
  let sha;
  try {
    sha = execFileSync('git', ['rev-parse', '--short', 'HEAD'], { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (error) {
    throw new Error(`the hub's version is this checkout's commit, but \`git rev-parse --short HEAD\` failed in ${cwd}: ${error.message}`);
  }
  if (!/^[0-9a-f]{7,40}$/.test(sha)) throw new Error(`\`git rev-parse --short HEAD\` answered ${JSON.stringify(sha)}, not a commit`);
  return sha;
}

export function versionModule(version) {
  return `// Written by scripts/hub-version.mjs at build and test time; never committed, never edited by hand.\nexport const HUB_VERSION = ${JSON.stringify(version)};\n`;
}

if (process.argv[1] !== undefined && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const version = hubVersion(process.env, HUB_ROOT);
  fs.writeFileSync(path.join(HUB_ROOT, VERSION_FILE), versionModule(version));
  console.log(`hub-version: ${VERSION_FILE} says ${version}`);
}
