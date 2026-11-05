// @vitest-environment node
// scripts/hub-version.mjs: the build's version, written into the function's generated content (final review I8):
// VERCEL_GIT_COMMIT_SHA on Vercel, refused by name when it is missing; this checkout's short commit elsewhere, and
// an error when git fails.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
// @ts-expect-error: a plain JavaScript script, with no type declarations.
import { hubVersion, versionModule } from '../scripts/hub-version.mjs';

const HUB = path.join(__dirname, '..');

describe('hubVersion', () => {
  it('on Vercel is VERCEL_GIT_COMMIT_SHA', () => {
    expect(hubVersion({ VERCEL: '1', VERCEL_GIT_COMMIT_SHA: '0123456789abcdef0123456789abcdef01234567' }, HUB))
      .toBe('0123456789abcdef0123456789abcdef01234567');
  });
  it('on Vercel without VERCEL_GIT_COMMIT_SHA fails, naming it', () => {
    expect(() => hubVersion({ VERCEL: '1' }, HUB)).toThrow(/VERCEL_GIT_COMMIT_SHA is not set/);
    expect(() => hubVersion({ VERCEL: '1', VERCEL_GIT_COMMIT_SHA: '' }, HUB)).toThrow(/VERCEL_GIT_COMMIT_SHA is not set/);
  });
  it('elsewhere is this checkout\'s short commit', () => {
    expect(hubVersion({}, HUB)).toMatch(/^[0-9a-f]{7,40}$/);
  });
  it('elsewhere fails when git does', () => {
    const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-version-'));
    expect(() => hubVersion({}, outside)).toThrow(/git rev-parse --short HEAD` failed/);
  });
  it('writes a module that exports it', () => {
    expect(versionModule('abc1234')).toContain('export const HUB_VERSION = "abc1234";');
  });
});
