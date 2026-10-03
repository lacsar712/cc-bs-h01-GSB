import { test } from "node:test";
import assert from "node:assert/strict";

import { displayVerdict, verdictClass } from "../src/verdict.js";

// 颜色必须与库里最终结论同一意思，不得把合格映成红色。
test("verdictClass 按真实结论着色", () => {
  assert.equal(verdictClass("合格"), "tag pass");
  assert.equal(verdictClass("越界"), "tag fail");
  assert.equal(verdictClass(null), "tag wait");
  assert.equal(verdictClass(undefined), "tag wait");
});

test("displayVerdict 优先展示库里的结论，未判定才显示占位", () => {
  assert.equal(displayVerdict({ verdict: "合格", status: "done" }), "合格");
  assert.equal(displayVerdict({ verdict: "越界", status: "done" }), "越界");
  assert.equal(displayVerdict({ verdict: null, status: "pending" }), "待处理");
  assert.equal(displayVerdict({ verdict: null, status: "processing" }), "处理中");
  assert.equal(displayVerdict({ verdict: null, status: "other" }), "—");
});
