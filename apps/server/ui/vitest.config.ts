import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Separate from vite.config.ts, whose dev server insists on JEFF_URL; tests talk to a mocked fetch instead.
export default defineConfig({
  plugins: [react()],
  // vite.config.ts requires VITE_JEFF_DOCS_URL for a real build (the public hub's docs URL, not decided yet); tests
  // need a stand-in so App.tsx's rail link has something to assert against.
  define: { 'import.meta.env.VITE_JEFF_DOCS_URL': JSON.stringify('https://jeff-docs.test/docs/guides/introduction') },
  test: {
    environment: 'jsdom',
    // The server always serves the console at <root>/ui/ (api.ts serverRoot), so the tests' page is there too.
    environmentOptions: { jsdom: { url: 'http://localhost:3000/ui/' } },
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    setupFiles: ['./src/test/setup.ts'],
  },
});
