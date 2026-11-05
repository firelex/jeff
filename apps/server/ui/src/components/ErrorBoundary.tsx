import { Component, type ReactNode } from 'react';
import { shownError, type ShownError } from '../api';
import { ErrorAlert } from './ErrorAlert';

/**
 * Shows a page that throws while rendering as an ErrorAlert (the error's name and message) instead of a blank screen.
 * Give it a `key` that changes with the page, so moving to another page starts again without the error.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: ShownError | null }> {
  state: { error: ShownError | null } = { error: null };

  static getDerivedStateFromError(error: unknown): { error: ShownError } {
    return { error: error instanceof Error ? shownError(error) : { type: 'Error', message: String(error), requestId: null } };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="px-[clamp(20px,4vw,64px)] pt-[clamp(24px,5vh,56px)]">
          <p className="mb-2 text-sm text-soft">This page failed while drawing itself:</p>
          <ErrorAlert error={this.state.error} />
        </div>
      );
    }
    return this.props.children;
  }
}
