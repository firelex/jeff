import { describe, expect, it } from 'vitest';
import { runShortcut } from './shortcut';

describe('runShortcut', () => {
  it('is Cmd on Apple systems and Ctrl elsewhere', () => {
    expect(runShortcut('MacIntel')).toBe('⌘ Enter');
    expect(runShortcut('iPad')).toBe('⌘ Enter');
    expect(runShortcut('Win32')).toBe('Ctrl Enter');
    expect(runShortcut('Linux x86_64')).toBe('Ctrl Enter');
  });
});
