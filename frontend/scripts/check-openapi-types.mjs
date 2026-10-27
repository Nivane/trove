#!/usr/bin/env node
/**
 * 校验 `src/api/openapi.d.ts` 与 `src/api/openapi.json` 同步（漂移门）。
 *
 * 生成链：后端 Pydantic 模型 → scripts/gen_openapi.py → openapi.json
 *        → openapi-typescript → openapi.d.ts。链上任何一环没重跑，前端
 *        类型就停在上一个契约版本 —— 而手写类型会继续"编译通过"。
 *
 * 两份都是提交物，所以这里不需要跑后端、不需要网络：重新生成到内存再比对。
 * 与 `npm run types:generate` 走同一个 openapi-typescript API，输出逐字节
 * 应相同。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import openapiTS, { COMMENT_HEADER, astToString } from "openapi-typescript";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const schemaPath = path.join(root, "src/api/openapi.json");
const typesPath = path.join(root, "src/api/openapi.d.ts");

// 与 CLI 同一构造:`COMMENT_HEADER` + `astToString(openapiTS(schemaPath))`
// （CLI 的 `generateSchema` 就是这两步 —— 不复制它的 private 函数，只复制
// 已导出的同一组零件）。
const expected = `${COMMENT_HEADER}${astToString(await openapiTS(pathToFileURL(schemaPath)))}`;
const actual = fs.readFileSync(typesPath, "utf8");

if (expected !== actual) {
  console.error(
    "openapi.d.ts is out of date with openapi.json.\n" +
      "Run `npm run types:generate` and commit the result.",
  );
  process.exit(1);
}
console.log("openapi.d.ts is up to date.");
