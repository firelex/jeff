import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Field } from './field';
import { Input } from './input';

describe('Field', () => {
  it('renders a label, a hint and the given control', () => {
    render(
      <Field label="Name" hint="As it appears on the invoice" htmlFor="name">
        <Input id="name" />
      </Field>,
    );
    expect(screen.getByText('Name')).toBeTruthy();
    expect(screen.getByText('As it appears on the invoice')).toBeTruthy();
    expect(screen.getByLabelText('Name')).toBeTruthy();
  });

  it('shows an error instead of the hint', () => {
    render(
      <Field label="Name" hint="As it appears on the invoice" error="Required" htmlFor="name">
        <Input id="name" />
      </Field>,
    );
    expect(screen.getByText('Required')).toBeTruthy();
    expect(screen.queryByText('As it appears on the invoice')).toBeNull();
  });
});
