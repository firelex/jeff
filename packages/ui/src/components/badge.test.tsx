import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Badge } from './badge';
import type { BadgeTone } from './badge';

const TAG_TONES: BadgeTone[] = ['neutral', 'teal', 'proprietary', 'preview', 'brand'];
const STATUS_TONES: BadgeTone[] = ['ok', 'info', 'empty'];
const TONES: BadgeTone[] = [...TAG_TONES, ...STATUS_TONES];

describe('Badge', () => {
  it('renders every tone with its own text', () => {
    render(
      <>
        {TONES.map((tone) => (
          <Badge key={tone} tone={tone}>
            {tone}
          </Badge>
        ))}
      </>,
    );
    for (const tone of TONES) {
      expect(screen.getByText(tone)).toBeTruthy();
    }
  });

  it('gives each tone a distinct class', () => {
    render(
      <>
        {TONES.map((tone) => (
          <Badge key={tone} tone={tone}>
            {tone}
          </Badge>
        ))}
      </>,
    );
    const classNames = TONES.map((tone) => screen.getByText(tone).className);
    expect(new Set(classNames).size).toBe(TONES.length);
  });

  it('never uses text-transform: the deck copy is mixed case (.tag/.st have none)', () => {
    render(
      <>
        {TONES.map((tone) => (
          <Badge key={tone} tone={tone}>
            {tone}
          </Badge>
        ))}
      </>,
    );
    for (const tone of TONES) {
      expect(screen.getByText(tone).className).not.toContain('uppercase');
    }
  });

  it('gives tag-family tones the .tag shape (pitch-v2.html:62): radius 4px, 10.5px, 1px 6px padding, letter-spacing .02em', () => {
    render(
      <>
        {TAG_TONES.map((tone) => (
          <Badge key={tone} tone={tone}>
            {tone}
          </Badge>
        ))}
      </>,
    );
    for (const tone of TAG_TONES) {
      const className = screen.getByText(tone).className;
      expect(className).toContain('rounded-tag');
      expect(className).toContain('text-[10.5px]');
      expect(className).toContain('px-[6px]');
      expect(className).toContain('py-px');
      expect(className).toContain('tracking-[0.02em]');
      expect(className).not.toContain('rounded-status');
    }
  });

  it('gives status-family tones the .st shape (pitch-v2.html:310): radius 5px, 11px, 2px 7px padding, no letter-spacing', () => {
    render(
      <>
        {STATUS_TONES.map((tone) => (
          <Badge key={tone} tone={tone}>
            {tone}
          </Badge>
        ))}
      </>,
    );
    for (const tone of STATUS_TONES) {
      const className = screen.getByText(tone).className;
      expect(className).toContain('rounded-status');
      expect(className).toContain('text-[11px]');
      expect(className).toContain('px-[7px]');
      expect(className).toContain('py-[2px]');
      expect(className).not.toContain('tracking-[0.02em]');
      expect(className).not.toContain('rounded-tag');
    }
  });
});
