import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DecisionView } from './DecisionView';
import { decision } from '../test/fixtures';

describe('DecisionView', () => {
  it('shows the value, a calibrated confidence, the probabilities, timing and the request id', () => {
    render(<DecisionView decision={decision()} requestId="req-42" roundTripMs={12.3} />);
    expect(screen.getByTestId('decision-value').textContent).toBe('k1');
    expect(screen.getByText('Calibrated')).toBeTruthy();
    expect(screen.getByTestId('decision-confidence').textContent).toBe('91.0%');
    expect(screen.queryByText('Abstained')).toBeNull();
    expect(screen.getAllByRole('meter')).toHaveLength(3);
    expect(screen.getByText('req-42')).toBeTruthy();
    expect(screen.getByText('model_ms')).toBeTruthy();
  });

  it('shows an abstained decision as abstained, and an uncalibrated confidence as not calibrated', () => {
    render(<DecisionView decision={decision({ abstained: true, calibrated: false, confidence: 0.3 })} requestId="r" roundTripMs={1} />);
    expect(screen.getByText('Abstained')).toBeTruthy();
    expect(screen.getByText('Not calibrated')).toBeTruthy();
    expect(screen.getByText(/no decision/i)).toBeTruthy();
  });

  it('shows the route taken and each escalation', () => {
    render(<DecisionView
      decision={decision({
        route: [{ router: 'jeff.core.router', chose: 'jeff.support.triage.route', confidence: 0.97 }],
        escalations: [{ decided_by: 'jeff.support.intents.intent@1.3.0', value: 'refund', confidence: 0.4, reason: 'abstained' }],
      })}
      requestId="r" roundTripMs={1} />);
    expect(screen.getByText('jeff.core.router')).toBeTruthy();
    expect(screen.getByText('jeff.support.intents.intent@1.3.0')).toBeTruthy();
  });
});
