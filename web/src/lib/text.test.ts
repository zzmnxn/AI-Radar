import assert from "node:assert/strict";
import test from "node:test";
import { splitCode } from "./text.ts";

test("splitCode marks backtick spans as code", () => {
  assert.deepEqual(splitCode("`uv tool install agent-mcts`로 설치한다."), [
    { text: "uv tool install agent-mcts", code: true },
    { text: "로 설치한다.", code: false },
  ]);
});

test("splitCode leaves plain text and unmatched backticks alone", () => {
  assert.deepEqual(splitCode("그냥 문장"), [{ text: "그냥 문장", code: false }]);
  assert.deepEqual(splitCode("a ` b"), [{ text: "a ` b", code: false }]);
});
