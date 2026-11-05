#!/usr/bin/env node
/**
 * 从 SQL Agent 的 OpenAPI 快照和 A2A 契约的 JSON Schema 生成控制台用的 TypeScript 类型。
 *
 *   node frontend/scripts/gen-sql-types.mjs            # 写 src/api/sql/generated.ts
 *
 * 输入：
 *   - ai-sql-agent/openapi.json（ai-sql-agent/scripts/export_openapi.py 导出，SQL Agent 的测试断言它和代码一致）
 *   - a2a-contract/schema/*.schema.json（契约本身）
 *
 * 为什么不用现成的生成器：两份输入用到的 JSON Schema 只有一小块（$ref、enum、const、anyOf、
 * 数组、对象），几十行就够；引一个生成器要多装一串依赖，每次升级还得核对输出变没变。
 * 只表达"形状"：pattern、minimum、if/then/not 这类校验约束在 TS 里写不出来，直接略过——
 * 校验是契约测试的事（a2a-contract/tools、两边的契约用例），这里只管"字段改名、删字段、改类型"
 * 能让前端的类型检查报错。
 *
 * src/api/sql/__tests__/generated.test.ts 断言提交进去的文件就是这个脚本现在的输出：
 * 改了接口或契约却没重新生成，前端那一项 CI 就不过。
 */
import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = path.resolve(HERE, '..')
const ROOT = path.resolve(FRONTEND, '..')
export const OPENAPI = path.join(ROOT, 'ai-sql-agent', 'openapi.json')
export const CONTRACT_DIR = path.join(ROOT, 'a2a-contract', 'schema')
export const OUTPUT = path.join(FRONTEND, 'src', 'api', 'sql', 'generated.ts')

const pascal = (text) => text
  .replace(/\.schema\.json$/, '')
  .split(/[^A-Za-z0-9]+/)
  .filter(Boolean)
  .map((part) => part[0].toUpperCase() + part.slice(1))
  .join('')

const IDENT = /^[A-Za-z_$][A-Za-z0-9_$]*$/
const key = (name) => (IDENT.test(name) ? name : JSON.stringify(name))
const literal = (value) => JSON.stringify(value)

/**
 * 一段说明写成 JSDoc。只取第一段，太长的截掉：生成文件不是文档。字段只用 description——
 * pydantic 给每个字段自动生成的 title（"Created At"）没有信息量。
 */
function doc(schema, indent, { title = false } = {}) {
  const text = (schema.description || (title ? schema.title : '') || '').split(/\n\s*\n/)[0].replace(/\s+/g, ' ').trim()
  if (!text) return ''
  const clipped = text.length > 160 ? `${text.slice(0, 157)}...` : text
  return `${indent}/** ${clipped.replace(/\*\//g, '* /')} */\n`
}

/**
 * schema → TS 类型表达式。refName 把 $ref 翻成类型名（两种输入的 $ref 写法不同）。
 */
function typeOf(schema, refName, indent = '') {
  if (schema === true || schema === undefined) return 'unknown'
  if (schema === false) return 'never'
  if (schema.$ref) return refName(schema.$ref)
  if ('const' in schema) return literal(schema.const)
  if (Array.isArray(schema.enum)) return schema.enum.map(literal).join(' | ')

  const variants = schema.anyOf || schema.oneOf
  if (variants) return union(variants.map((item) => typeOf(item, refName, indent)))
  if (schema.allOf) {
    // 只有带形状的分支才算数：if/then/not 这类纯校验的分支在 TS 里表达不了
    const parts = schema.allOf
      .filter((item) => item.$ref || item.type || item.properties || item.enum || 'const' in item)
      .map((item) => typeOf(item, refName, indent))
    const own = schema.type || schema.properties ? [objectOrPrimitive(schema, refName, indent)] : []
    const all = [...own, ...parts]
    return all.length ? all.join(' & ') : 'unknown'
  }
  return objectOrPrimitive(schema, refName, indent)
}

function union(types) {
  const unique = [...new Set(types)]
  return unique.length === 1 ? unique[0] : unique.join(' | ')
}

function objectOrPrimitive(schema, refName, indent) {
  const types = Array.isArray(schema.type) ? schema.type : schema.type ? [schema.type] : []
  if (!types.length) return schema.properties ? objectType(schema, refName, indent) : 'unknown'
  return union(types.map((type) => {
    switch (type) {
      case 'string': return 'string'
      case 'integer':
      case 'number': return 'number'
      case 'boolean': return 'boolean'
      case 'null': return 'null'
      case 'array': return arrayType(schema, refName, indent)
      case 'object': return objectType(schema, refName, indent)
      default: return 'unknown'
    }
  }))
}

function arrayType(schema, refName, indent) {
  const item = schema.items ? typeOf(schema.items, refName, indent) : 'unknown'
  return /[ |&]/.test(item) ? `Array<${item}>` : `${item}[]`
}

function objectType(schema, refName, indent) {
  const props = schema.properties || {}
  const names = Object.keys(props)
  if (!names.length) {
    const extra = schema.additionalProperties
    return extra && typeof extra === 'object' ? `Record<string, ${typeOf(extra, refName, indent)}>` : 'Record<string, unknown>'
  }
  const required = new Set(schema.required || [])
  const inner = `${indent}  `
  const lines = names.map((name) => {
    const prop = props[name]
    return `${doc(prop, inner)}${inner}${key(name)}${required.has(name) ? '' : '?'}: ${typeOf(prop, refName, inner)}`
  })
  return `{\n${lines.join('\n')}\n${indent}}`
}

function declare(name, schema, refName) {
  const body = typeOf(schema, refName)
  const head = doc(schema, '', { title: true })
  return body.startsWith('{')
    ? `${head}export interface ${name} ${body}\n`
    : `${head}export type ${name} = ${body}\n`
}

/** SQL Agent 的 OpenAPI：只生成 components.schemas（请求体和有 response_model 的响应）。 */
function fromOpenApi(spec) {
  const refName = (ref) => ref.replace('#/components/schemas/', '')
  const schemas = spec.components?.schemas || {}
  return Object.keys(schemas).sort().map((name) => declare(name, schemas[name], refName))
}

/**
 * 契约的 JSON Schema：每个文件一个根类型（文件名转成名字），文件里的 $defs 只生成根类型
 * 用得到的那些，名字带上文件的前缀。跨文件的 $ref（`stage.schema.json`）指向那个文件的根类型。
 */
function fromContract(files) {
  const out = []
  for (const [file, schema] of files) {
    const base = pascal(file)
    const used = new Set()
    const refName = (ref) => {
      if (ref.startsWith('#/$defs/')) {
        const def = ref.slice('#/$defs/'.length)
        used.add(def)
        return base + pascal(def)
      }
      if (ref.endsWith('.schema.json')) return pascal(path.basename(ref))
      throw new Error(`${file}: 不认识的 $ref ${ref}`)
    }
    const root = declare(base, schema, refName)
    const defs = []
    const done = new Set()
    // 定义之间也会互相引用：一直展开到没有新的为止
    while ([...used].some((name) => !done.has(name))) {
      for (const name of [...used].sort()) {
        if (done.has(name)) continue
        done.add(name)
        const def = schema.$defs?.[name]
        if (!def) throw new Error(`${file}: $defs 里没有 ${name}`)
        defs.push(declare(base + pascal(name), def, refName))
      }
    }
    out.push(root, ...defs)
  }
  return out
}

export function render() {
  const spec = JSON.parse(readFileSync(OPENAPI, 'utf8'))
  const contract = readdirSync(CONTRACT_DIR)
    .filter((name) => name.endsWith('.schema.json'))
    .sort()
    .map((name) => [name, JSON.parse(readFileSync(path.join(CONTRACT_DIR, name), 'utf8'))])
  return [
    '/* 生成的文件，别手改。改了 SQL Agent 的接口或 A2A 契约之后重新生成：',
    ' *   (cd ai-sql-agent && PYTHONPATH=src:. .venv/bin/python scripts/export_openapi.py)',
    ' *   node frontend/scripts/gen-sql-types.mjs',
    ' * 来源：ai-sql-agent/openapi.json、a2a-contract/schema/*.schema.json。 */',
    '/* eslint-disable */',
    '',
    `// ── SQL Agent（OpenAPI ${spec.info?.version ?? ''}）${'─'.repeat(40)}`,
    '',
    ...fromOpenApi(spec),
    `// ── A2A 契约${'─'.repeat(52)}`,
    '',
    ...fromContract(contract),
  ].join('\n')
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  writeFileSync(OUTPUT, render())
  console.log(`写入 ${path.relative(ROOT, OUTPUT)}`)
}
