import { defineConfig } from 'vite';

// Standalone benchmark harness — no dependency on the rest of the monorepo's
// build pipeline. `base: './'` keeps a `vite build` output openable straight
// from disk (file://) in a pinch, in addition to the normal dev-server flow.
export default defineConfig({
  base: './',
  server: {
    open: false,
  },
});
