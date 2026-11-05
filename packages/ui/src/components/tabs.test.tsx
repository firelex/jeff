import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Tabs, tabPanelProps } from './tabs';
import type { TabItem } from './tabs';

const ITEMS: TabItem[] = [
  { value: 'a', label: 'Tab A' },
  { value: 'b', label: 'Tab B' },
  { value: 'c', label: 'Tab C' },
];

/** A controlled wrapper, since the WAI-ARIA model requires selection to actually follow focus. */
function ControlledTabs({ onChange }: { onChange?: (value: string) => void }) {
  const [value, setValue] = useState('a');
  return (
    <Tabs
      id="demo"
      items={ITEMS}
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange?.(next);
      }}
    />
  );
}

describe('Tabs', () => {
  it('names each tab\'s panel, and a panel built with tabPanelProps is labelled by its tab', () => {
    render(
      <>
        <ControlledTabs />
        <div {...tabPanelProps('demo', 'a')}>Panel A</div>
      </>,
    );
    const tab = screen.getByRole('tab', { name: 'Tab A' });
    const panel = screen.getByRole('tabpanel', { name: 'Tab A' });
    expect(tab.getAttribute('aria-controls')).toBe(panel.id);
    expect(panel.textContent).toBe('Panel A');
  });

  it('renders every item and marks the active one selected', () => {
    render(<Tabs id="demo" items={ITEMS} value="a" onChange={() => {}} />);
    const a = screen.getByRole('tab', { name: 'Tab A' });
    const b = screen.getByRole('tab', { name: 'Tab B' });
    expect(a.getAttribute('aria-selected')).toBe('true');
    expect(b.getAttribute('aria-selected')).toBe('false');
  });

  it('calls onChange with the clicked tab value', async () => {
    const onChange = vi.fn();
    render(<Tabs id="demo" items={ITEMS} value="a" onChange={onChange} />);
    await userEvent.click(screen.getByRole('tab', { name: 'Tab B' }));
    expect(onChange).toHaveBeenCalledWith('b');
  });

  it('gives only the active tab tabIndex 0 (roving tabIndex)', () => {
    render(<Tabs id="demo" items={ITEMS} value="b" onChange={() => {}} />);
    expect(screen.getByRole('tab', { name: 'Tab A' }).getAttribute('tabindex')).toBe('-1');
    expect(screen.getByRole('tab', { name: 'Tab B' }).getAttribute('tabindex')).toBe('0');
    expect(screen.getByRole('tab', { name: 'Tab C' }).getAttribute('tabindex')).toBe('-1');
  });

  it('ArrowRight moves both focus and selection to the next tab', async () => {
    render(<ControlledTabs />);
    screen.getByRole('tab', { name: 'Tab A' }).focus();
    await userEvent.keyboard('{ArrowRight}');
    const b = screen.getByRole('tab', { name: 'Tab B' });
    expect(document.activeElement).toBe(b);
    expect(b.getAttribute('aria-selected')).toBe('true');
    expect(b.getAttribute('tabindex')).toBe('0');
    expect(screen.getByRole('tab', { name: 'Tab A' }).getAttribute('tabindex')).toBe('-1');
  });

  it('ArrowLeft moves both focus and selection to the previous tab', async () => {
    render(<ControlledTabs />);
    screen.getByRole('tab', { name: 'Tab B' }).focus();
    await userEvent.keyboard('{ArrowLeft}');
    const a = screen.getByRole('tab', { name: 'Tab A' });
    expect(document.activeElement).toBe(a);
    expect(a.getAttribute('aria-selected')).toBe('true');
  });

  it('wraps from the last tab to the first with ArrowRight', async () => {
    render(<ControlledTabs />);
    screen.getByRole('tab', { name: 'Tab A' }).focus();
    await userEvent.keyboard('{ArrowRight}{ArrowRight}{ArrowRight}');
    const a = screen.getByRole('tab', { name: 'Tab A' });
    expect(document.activeElement).toBe(a);
    expect(a.getAttribute('aria-selected')).toBe('true');
  });

  it('wraps from the first tab to the last with ArrowLeft', async () => {
    render(<ControlledTabs />);
    screen.getByRole('tab', { name: 'Tab A' }).focus();
    await userEvent.keyboard('{ArrowLeft}');
    const c = screen.getByRole('tab', { name: 'Tab C' });
    expect(document.activeElement).toBe(c);
    expect(c.getAttribute('aria-selected')).toBe('true');
  });

  it('Home moves focus and selection to the first tab', async () => {
    render(<ControlledTabs />);
    screen.getByRole('tab', { name: 'Tab C' }).focus();
    await userEvent.keyboard('{Home}');
    const a = screen.getByRole('tab', { name: 'Tab A' });
    expect(document.activeElement).toBe(a);
    expect(a.getAttribute('aria-selected')).toBe('true');
  });

  it('End moves focus and selection to the last tab', async () => {
    render(<ControlledTabs />);
    screen.getByRole('tab', { name: 'Tab A' }).focus();
    await userEvent.keyboard('{End}');
    const c = screen.getByRole('tab', { name: 'Tab C' });
    expect(document.activeElement).toBe(c);
    expect(c.getAttribute('aria-selected')).toBe('true');
  });
});
