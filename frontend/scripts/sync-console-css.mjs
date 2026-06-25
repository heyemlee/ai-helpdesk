#!/usr/bin/env node
/**
 * 把设计工程里的 console.css 同步到 src/console.css。
 *
 * 为什么需要这个脚本：这份 CSS 有两份副本——设计工程里的原型页（5 个静态
 * HTML）和 React 工程各用一份。之前靠人工 `cp` 保持一致，漏一次两边就会
 * 静默分叉，而分叉的表现是「React 应用样式比原型旧」，不会报错、不会挂测试，
 * 只会在某次截图里被发现。
 *
 * 设计目录是 OpenDesign 的应用数据目录，路径本机专属且带空格，所以不硬编码：
 * 用 AI_HELPDESK_DESIGN_DIR 覆盖，找不到就静默跳过——CI、同事的机器、
 * 只 clone 了仓库的人，都不该因为缺少这个目录而构建失败。仓库里的
 * src/console.css 本身就是可用的完整文件，同步只是让它保持最新。
 *
 * 用法：
 *   node scripts/sync-console-css.mjs           同步（predev / prebuild 自动跑）
 *   node scripts/sync-console-css.mjs --check    只检查，分叉时 exit 1
 */
import { copyFileSync, existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const DEST = resolve(HERE, '..', 'src', 'console.css')

const DEFAULT_DESIGN_DIR =
  '/Users/l/Library/Application Support/Open Design/namespaces/release-stable' +
  '/data/projects/da991797-435b-4c73-953b-96ac335e2fc1'

const designDir = process.env.AI_HELPDESK_DESIGN_DIR || DEFAULT_DESIGN_DIR
const src = join(designDir, 'assets', 'console.css')
const checkOnly = process.argv.includes('--check')

if (!existsSync(src)) {
  // 不是错误：仓库里的副本是完整可用的，这里只是没法确认它是不是最新
  console.log(`[sync:css] 跳过——设计目录不存在（${designDir}）`)
  process.exit(0)
}

const from = readFileSync(src)
const to = existsSync(DEST) ? readFileSync(DEST) : null

if (to && from.equals(to)) {
  process.exit(0)
}

if (checkOnly) {
  console.error('[sync:css] 两份 console.css 已分叉，跑 `npm run sync:css` 同步')
  process.exit(1)
}

copyFileSync(src, DEST)
console.log(
  `[sync:css] 已同步 console.css（${to ? `${to.length} → ${from.length}` : from.length} 字节）`,
)
