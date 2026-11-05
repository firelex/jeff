// Times as the internal API writes them, and the UTC month bounds the usage periods use.
import { expect, it } from 'vitest';
import { isoSeconds, monthStart, nextMonthStart } from '../app/lib/time';

it('writes times as the internal API does and finds UTC month bounds', () => {
  const moment = new Date('2026-12-31T23:59:59.999Z');
  expect(isoSeconds(moment)).toBe('2026-12-31T23:59:59Z');
  expect(isoSeconds(monthStart(moment))).toBe('2026-12-01T00:00:00Z');
  expect(isoSeconds(nextMonthStart(moment))).toBe('2027-01-01T00:00:00Z');
});
