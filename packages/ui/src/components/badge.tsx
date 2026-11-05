import type { ReactNode } from 'react';

export type BadgeTagTone = 'neutral' | 'teal' | 'proprietary' | 'preview' | 'brand';
export type BadgeStatusTone = 'ok' | 'info' | 'empty';
export type BadgeTone = BadgeTagTone | BadgeStatusTone;

export interface BadgeProps {
  tone?: BadgeTone;
  children?: ReactNode;
  className?: string;
}

const TAG_TONES = new Set<BadgeTone>(['neutral', 'teal', 'proprietary', 'preview', 'brand']);

// The deck's real copy is mixed case, so neither shape transforms text — only size,
// padding, radius and letter-spacing differ between the two families.
// `.tag` (jeff-pitches/main/pitch-v2.html:62): font-size 10.5px, weight 700, padding
// 1px 6px, radius 4px, letter-spacing .02em.
const TAG_BASE = 'inline-block rounded-tag px-[6px] py-px text-[10.5px] font-bold tracking-[0.02em]';
// `.st` (pitch-v2.html:310): font-size 11px, weight 700, padding 2px 7px, radius 5px,
// no letter-spacing.
const STATUS_BASE = 'inline-block rounded-status px-[7px] py-[2px] text-[11px] font-bold';

// The deck's `.tag` (neutral/teal/proprietary/preview) and `.st` (ok/info/empty) colours
// (jeff-pitches/main/pitch-v2.html), plus a `brand` tone from the primary palette.
const TONE_COLOR: Record<BadgeTone, string> = {
  neutral: 'bg-[#f1f1f4] text-[#5c6270]',
  teal: 'bg-[#e0f2f1] text-[#0f766e]',
  proprietary: 'bg-[#fff7e0] text-[#8a5a00]',
  preview: 'bg-[#fde68a] text-[#92400e]',
  brand: 'bg-primary-light text-primary-dark',
  ok: 'bg-[#d1fae5] text-[#047857]',
  info: 'bg-[#e0f2fe] text-[#075985]',
  empty: 'border border-dashed border-line bg-transparent text-mute',
};

/** A small coloured tag, matching the deck's `.tag` (tag-family tones) and `.st` (status-family tones) shapes. */
export function Badge({ tone = 'neutral', children, className = '' }: BadgeProps) {
  const base = TAG_TONES.has(tone) ? TAG_BASE : STATUS_BASE;
  return <span className={[base, TONE_COLOR[tone], className].join(' ')}>{children}</span>;
}
