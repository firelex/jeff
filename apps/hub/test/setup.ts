import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// vitest.config.ts does not enable the global test API, so Testing Library's own auto-cleanup (which looks for a
// global `afterEach`) never registers; do it explicitly. Same reasoning as the console's own test setup
// (apps/server/ui/src/test/setup.ts).
afterEach(() => {
  cleanup();
});
