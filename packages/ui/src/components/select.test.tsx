import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Select } from './select';

describe('Select', () => {
  it('renders the options and reports the chosen one', async () => {
    const onChange = vi.fn();
    render(
      <Select aria-label="size" value="sm" onChange={onChange}>
        <option value="sm">Small</option>
        <option value="lg">Large</option>
      </Select>,
    );
    const select = screen.getByLabelText('size') as HTMLSelectElement;
    expect(select.value).toBe('sm');
    await userEvent.selectOptions(select, 'lg');
    expect(onChange).toHaveBeenCalled();
  });
});
