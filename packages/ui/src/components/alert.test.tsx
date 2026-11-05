import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Alert } from './alert';

describe('Alert', () => {
  it('renders the error, warning and info tones with their message', () => {
    render(
      <>
        <Alert tone="error" title="bad_request">
          Something was wrong with the request.
        </Alert>
        <Alert tone="warning" title="slow">
          This is taking longer than usual.
        </Alert>
        <Alert tone="info" title="note">
          For your information.
        </Alert>
      </>,
    );
    expect(screen.getByText('bad_request')).toBeTruthy();
    expect(screen.getByText('Something was wrong with the request.')).toBeTruthy();
    expect(screen.getByText('slow')).toBeTruthy();
    expect(screen.getByText('note')).toBeTruthy();
  });

  it('an error or a warning is role=alert, announced at once; a notice is role=status, announced politely', () => {
    render(
      <>
        <Alert tone="error">Failed</Alert>
        <Alert tone="warning">Slow</Alert>
        <Alert tone="info">Note</Alert>
      </>,
    );
    expect(screen.getAllByRole('alert').map((node) => node.textContent)).toEqual(['Failed', 'Slow']);
    expect(screen.getByRole('status').textContent).toBe('Note');
  });

  it('gives each tone a distinct class', () => {
    render(
      <>
        <Alert tone="error">e</Alert>
        <Alert tone="warning">w</Alert>
        <Alert tone="info">i</Alert>
      </>,
    );
    const classNames = [...screen.getAllByRole('alert'), screen.getByRole('status')].map((node) => node.className);
    expect(new Set(classNames).size).toBe(3);
  });
});
