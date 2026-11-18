import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Proxying /api and /auth keeps the session cookie first-party in dev, as in production.
const backend = 'http://localhost:8080';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/auth': { target: backend, changeOrigin: true },
    },
  },
  test: { include: ['src/**/*.test.ts'], environment: 'node' },
});
