import { describe, expect, it } from 'vitest';
import { SESSION_KEY, clearSession, readSession, writeSession } from './session';

describe('the stored session', () => {
  it('is kept under jeff-console.session as {user, token}', () => {
    writeSession({ user: 'a@b.c', token: 't' });
    expect(JSON.parse(localStorage.getItem(SESSION_KEY) as string)).toEqual({ user: 'a@b.c', token: 't' });
    expect(readSession()).toEqual({ session: { user: 'a@b.c', token: 't' }, problem: null });
    clearSession();
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();
    expect(readSession()).toEqual({ session: null, problem: null });
  });

  it('an unreadable saved value is removed and the reason given', () => {
    localStorage.setItem(SESSION_KEY, '{not json');
    const read = readSession();
    expect(read.session).toBeNull();
    expect(read.problem).toContain('not valid JSON');
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();

    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'a' }));
    expect(readSession().problem).toContain('no user and token');
  });
});
