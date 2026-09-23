import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  plugins: [react()],
  root: path.resolve(__dirname),
  base: './',
  server: {
    port: 5173,
    strictPort: true,
    fs: {allow: [path.resolve(__dirname, '..')]},
  },
  build: {
    outDir: 'dist/renderer',
    emptyOutDir: false,
  },
});
