import { describe, expect, it } from 'vitest';
import { privateMarkersIn } from '../app/lib/private-markers';

describe('privateMarkersIn', () => {
  it('finds every private name, the Keychain item names included', () => {
    expect(privateMarkersIn('Scissero, Suzie, jeff-desk, firelex/jeff-dev, x.sslip.io, jeff-hub, jeff-v14-api, /mnt/d, teamsuzie')).toEqual([
      'teamsuzie', '/mnt/', 'Scissero', 'Suzie', 'jeff-desk', 'jeff-dev', 'sslip', 'jeff-hub', 'jeff-v14-api',
    ]);
  });
  it('finds Delta as a word of its own', () => {
    expect(privateMarkersIn('a plan for Delta Holdings')).toEqual(['Delta']);
    expect(privateMarkersIn('"Delta"')).toEqual(['Delta']);
  });
  it("leaves vendor code's \\Delta and dragDelta alone", () => {
    expect(privateMarkersIn(String.raw`katex: "\Delta", cy.dragDelta, DeltaX`)).toEqual([]);
  });
});

it('catches Delta after an escaped newline, return or tab, and joined to a capitalised word', () => {
  expect(privateMarkersIn('{"text":"Intro\\nDelta Holdings"}')).toContain('Delta');
  expect(privateMarkersIn('"\\tDelta"')).toContain('Delta');
  expect(privateMarkersIn('DeltaHoldings')).toEqual(['Delta+word']);
  expect(privateMarkersIn('"\\nDeltaHoldings"')).toEqual(['Delta+word']);
  expect(privateMarkersIn('katex: "\\\\Delta" dragDelta wheelDelta')).toEqual([]);
});
