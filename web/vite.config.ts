import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

// The console is a plain SPA served under /admin by the admin listener (BR-212): every asset is
// bundled, nothing is fetched from another origin, no inline script or style is emitted.
export default defineConfig({
  base: '/admin/',
  plugins: [svelte()],
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    emptyOutDir: true,
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
