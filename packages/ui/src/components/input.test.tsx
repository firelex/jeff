import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Input } from './input';
import { Textarea } from './textarea';

describe('Input', () => {
  it('renders the given value and reports changes', async () => {
    const onChange = vi.fn();
    render(<Input aria-label="email" value="a" onChange={onChange} />);
    const input = screen.getByLabelText('email') as HTMLInputElement;
    expect(input.value).toBe('a');
    await userEvent.type(input, 'b');
    expect(onChange).toHaveBeenCalled();
  });
});

describe('Textarea', () => {
  it('renders the given value and reports changes', async () => {
    const onChange = vi.fn();
    render(<Textarea aria-label="notes" value="a" onChange={onChange} />);
    const textarea = screen.getByLabelText('notes') as HTMLTextAreaElement;
    expect(textarea.value).toBe('a');
    await userEvent.type(textarea, 'b');
    expect(onChange).toHaveBeenCalled();
  });
});
