const { defineConfig } = require(process.env.ACCEPTANCE_PLAYWRIGHT_MODULE)
const fs = require('node:fs')
const cfg = JSON.parse(fs.readFileSync(process.env.ACCEPTANCE_STACK + '/private-config.json', 'utf8'))
module.exports = defineConfig({
  // Image extraction (60s), bounded SQL delegation (183s), and its follow-up
  // Run are sequential. Keep the harness deadline outside those service limits.
  testDir: __dirname, testMatch: 'workflow.spec.cjs', workers: 1, retries: 0, timeout: 660000,
  use: { baseURL: cfg.origin, headless: true, screenshot: 'only-on-failure', trace: 'retain-on-failure',
    launchOptions: process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : undefined },
  reporter: [['list'], ['json', { outputFile: process.env.ACCEPTANCE_BROWSER_OUTPUT + '/playwright.json' }]],
  outputDir: process.env.ACCEPTANCE_BROWSER_OUTPUT + '/artifacts',
})
