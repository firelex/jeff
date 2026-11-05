import { describe, expect, it } from 'vitest';
import { isRecord, pretty } from './json';

describe('isRecord', () => {
  it('is true for a plain object only', () => {
    expect(isRecord({ a: 1 })).toBe(true);
    expect(isRecord([])).toBe(false);
    expect(isRecord(null)).toBe(false);
    expect(isRecord('text')).toBe(false);
  });
});

describe('pretty', () => {
  it('indents by two spaces', () => {
    expect(pretty({ a: [1] })).toBe('{\n  "a": [\n    1\n  ]\n}');
  });
});
