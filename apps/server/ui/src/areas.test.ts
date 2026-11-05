import { describe, expect, it } from 'vitest';
import { adapterArea, areaOf } from './areas';

describe('areas', () => {
  it('reads the area from an adapter ID and from a function ID', () => {
    expect(adapterArea('jeff.support.triage')).toBe('support');
    expect(areaOf('jeff.support.triage.route')).toBe('support');
  });

  it('refuses an adapter ID of the wrong shape', () => {
    expect(() => adapterArea('jeff.support.triage.route')).toThrow(/jeff\.<area>\.<adapter>/);
    expect(() => adapterArea('acme.support.triage')).toThrow(/not an adapter ID/);
  });
});
