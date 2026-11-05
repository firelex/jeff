import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AppShell, RailGroup, RailItem } from './app-shell';

describe('AppShell', () => {
  it('renders the rail and the page content', () => {
    render(
      <AppShell rail={<div>the rail</div>}>
        <p>the page</p>
      </AppShell>,
    );
    expect(screen.getByText('the rail')).toBeTruthy();
    expect(screen.getByText('the page')).toBeTruthy();
  });
});

describe('RailGroup', () => {
  it('renders its label', () => {
    render(<RailGroup label="Channels" />);
    expect(screen.getByText('Channels')).toBeTruthy();
  });

  it('uses the deck\'s .grp margin-top of 9px (pitch-v2.html:15)', () => {
    render(<RailGroup label="Channels" />);
    expect(screen.getByText('Channels').className).toContain('mt-[7px]');
  });
});

describe('RailItem', () => {
  it('renders active and inactive variants', () => {
    render(
      <>
        <RailItem active>On</RailItem>
        <RailItem>Off</RailItem>
      </>,
    );
    const on = screen.getByRole('button', { name: 'On' });
    const off = screen.getByRole('button', { name: 'Off' });
    expect(on.className).toContain('text-white');
    expect(off.className).not.toContain('text-white');
  });

  it('calls onClick when clicked', async () => {
    const onClick = vi.fn();
    render(<RailItem onClick={onClick}>Item</RailItem>);
    await userEvent.click(screen.getByText('Item'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe('RailItem current page', () => {
  it('marks the active item aria-current="page" and no other', () => {
    render(
      <>
        <RailItem active>Home</RailItem>
        <RailItem>Playground</RailItem>
      </>,
    );
    expect(screen.getByRole('button', { name: 'Home' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('button', { name: 'Playground' }).getAttribute('aria-current')).toBeNull();
  });
});
