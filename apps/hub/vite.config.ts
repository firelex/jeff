import { reactRouter } from '@react-router/dev/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// Fonts are always their own fingerprinted file, never inlined into the CSS as a data URL, even when smaller than
// Vite's 4 KB inline limit (Jeff Pixel is 1.8 KB): root.tsx preloads the font by its URL, and a preload of a file the
// CSS does not use would load it twice.
export default defineConfig({
  build: { assetsInlineLimit: (file) => (file.endsWith('.woff2') ? false : undefined) },
  plugins: [tailwindcss(), reactRouter()],
});
