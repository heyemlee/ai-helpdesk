import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: '.', testMatch: '*.pw.ts', workers: 1, retries: 0, timeout: 45_000,
  use: { baseURL: process.env.BROWSER_FRONTEND_URL, headless: true, screenshot: 'only-on-failure',
    launchOptions: process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : undefined },
  reporter: [['list']], outputDir: process.env.BROWSER_OUTPUT || '/tmp/aihd-browser-results',
})
