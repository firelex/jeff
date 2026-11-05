import { describe, expect, it } from 'vitest';
import { eventView, parseHash, routeHash } from './routes';

describe('hash routes', () => {
  it('parses every console address', () => {
    expect(parseHash('')).toEqual({ view: 'home' });
    expect(parseHash('#home')).toEqual({ view: 'home' });
    expect(parseHash('#playground')).toEqual({ view: 'playground' });
    expect(parseHash('#factory')).toEqual({ view: 'factory' });
    expect(parseHash('#adapters')).toEqual({ view: 'adapters' });
    expect(parseHash('#api')).toEqual({ view: 'api' });
    expect(parseHash('#access')).toEqual({ view: 'access' });
  });

  it('an unknown address is not found, never quietly the home page', () => {
    expect(parseHash('#nope')).toEqual({ view: 'not-found', hash: '#nope' });
  });

  it('#docs/x is not found: the console dropped its own documentation view in favour of a link to the hub', () => {
    expect(parseHash('#docs')).toEqual({ view: 'not-found', hash: '#docs' });
    expect(parseHash('#docs/x')).toEqual({ view: 'not-found', hash: '#docs/x' });
  });

  it('round-trips, and names each view the way POST /ui/events accepts', () => {
    expect(routeHash(parseHash('#playground'))).toBe('#playground');
    expect(eventView(parseHash('#playground'))).toBe('playground');
    for (const hash of ['', '#api', '#whatever']) {
      expect(eventView(parseHash(hash))).toMatch(/^[a-z0-9-]+$/);
    }
  });
});
