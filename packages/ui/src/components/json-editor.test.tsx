import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { JsonEditor } from './json-editor';

describe('JsonEditor', () => {
  it('gives the editable content its accessible name', () => {
    render(<JsonEditor label="Request body" value="{}" onChange={() => {}} />);
    expect(screen.getByRole('textbox', { name: 'Request body' })).toBeTruthy();
  });

  it('renders the given value', () => {
    render(<JsonEditor label="Request body" value='{"a":1}' onChange={() => {}} />);
    expect(screen.getByText(/"a"/)).toBeTruthy();
  });

  it('reports edits through onChange', async () => {
    const onChange = vi.fn();
    const { container } = render(<JsonEditor label="Request body" value="{}" onChange={onChange} />);
    const editable = container.querySelector('[contenteditable="true"]') as HTMLElement;
    expect(editable).toBeTruthy();
    editable.focus();
    await userEvent.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalled();
    expect(onChange.mock.calls.at(-1)?.[0]).toContain('\n');
  });

  it('does not report edits when readOnly', async () => {
    const onChange = vi.fn();
    const { container } = render(<JsonEditor label="Request body" value="{}" onChange={onChange} readOnly />);
    const editable = container.querySelector('[contenteditable]') as HTMLElement;
    expect(editable.getAttribute('contenteditable')).toBe('false');
    editable.focus();
    await userEvent.keyboard('{Enter}');
    expect(onChange).not.toHaveBeenCalled();
  });
});
