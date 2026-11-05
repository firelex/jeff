// The hub's recurring class lists, so a button, a heading or a table cell looks the same on every page. The colours,
// fonts and shapes behind them are tokens in app.css.

// Buttons are keycaps: a solid square block on a hard offset shadow, which sinks onto its shadow when pressed.
// Primary is an ink key on a violet shadow, secondary a paper key with an ink border and shadow, danger a red key
// for a destructive confirm (ConfirmDialog). Pixel font. Jeff Pixel sizes are whole multiples of 10 px (app.css:
// --text-pixel-*, .font-pixel).
const KEY_BASE = 'inline-flex items-center justify-center gap-2 border-2 border-ink transition-[transform,box-shadow] duration-75 '
  + 'hover:translate-x-px hover:translate-y-px active:translate-x-[3px] active:translate-y-[3px] active:shadow-none';
const KEY_PRIMARY = `${KEY_BASE} bg-ink text-background shadow-key-accent hover:shadow-[2px_2px_0_var(--v)]`;
const KEY_SECONDARY = `${KEY_BASE} bg-panel text-ink shadow-key hover:shadow-[2px_2px_0_var(--ink)]`;
const KEY_DANGER = `${KEY_BASE} bg-danger text-background shadow-key hover:shadow-[2px_2px_0_var(--ink)]`;
export const BUTTON_PRIMARY = `${KEY_PRIMARY} font-pixel text-pixel-20`;
export const BUTTON_SECONDARY = `${KEY_SECONDARY} font-pixel text-pixel-20`;
export const BUTTON_DANGER = `${KEY_DANGER} font-pixel text-pixel-20`;
// The header's keycap: mono below 1280 px and the pixel font from there, as the header's links are (Header.tsx).
export const HEADER_BUTTON = `${KEY_PRIMARY} font-mono text-sm xl:font-pixel xl:text-pixel-20`;

// Page title (h1) and section heading (h2) outside Markdown: the pixel font, sentence case, the h2 led by the block
// cursor.
export const PAGE_TITLE = 'font-pixel text-pixel-30 text-text';
export const SECTION_HEADING = 'cursor-mark font-pixel text-pixel-20 leading-[32px] text-text';

// A card or panel: square, ink-bordered, lifting onto a hard ink block shadow on hover when it is a link.
export const CARD = 'border border-ink bg-panel';
// CARD_LIFT is the lift alone, for a card whose border is not ink (an open adapter's on the catalogue).
export const CARD_LIFT = 'transition hover:-translate-x-0.5 hover:-translate-y-0.5 hover:shadow-block';
export const CARD_LINK = `${CARD} ${CARD_LIFT}`;

// Tables: an ink-bordered frame, mono column headers, faint row rules.
export const TABLE_FRAME = `overflow-x-auto ${CARD}`;
export const TH = 'border-b border-ink bg-background px-4 py-2 font-mono text-xs font-medium text-mute';
export const TD = 'border-b border-rule px-4 py-2.5 align-top [tr:last-child_&]:border-b-0';

// An in-text link: violet, underlined.
export const TEXT_LINK = 'font-medium text-primary underline underline-offset-2 hover:text-primary-dark';

// A tab on paper (the docs' Guides / SDK / API reference): the pixel font, the selected one violet and led by the
// block cursor. Mono at phone width, as the header's links are: the three tabs in the pixel font at 20 px are about
// 480 px wide and would scroll off a phone's screen.
export function tabClass(selected: boolean): string {
  return [
    'px-3 py-1.5 font-mono text-sm sm:font-pixel sm:text-pixel-20',
    selected ? 'cursor-mark text-primary' : 'text-mute hover:text-text',
  ].join(' ');
}

// A tab in a screen's frame (an Example's Python/curl, the home screen's Python/Node): light on the violet frame,
// the selected one inverted into a light key. text-pixel-10 (not the usual pixel-20 floor) and tight padding, so a
// code window's six languages, curl and Copy share one row rather than wrapping (app.css: --text-pixel-10).
export function screenTabClass(selected: boolean): string {
  return [
    'px-1.5 py-0.5 font-pixel text-pixel-10',
    selected ? 'bg-screen-text text-screen' : 'text-white hover:bg-white/15',
  ].join(' ');
}
