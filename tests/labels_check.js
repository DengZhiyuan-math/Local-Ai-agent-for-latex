"use strict";
const assert = require("assert");
const Chat = require("../prism_local/static/chatlogic.js");

const text = String.raw`See \eqref{eq:main} and \Cref{sec:intro, thm:missing}.`;
assert.deepStrictEqual(Chat.labelReferences(text, {"eq:main": [], "sec:intro": []}), [
  {from: 11, to: 18, label: "eq:main"},
  {from: 30, to: 39, label: "sec:intro"},
  {from: 41, to: 52, label: "thm:missing"},
]);
assert.deepStrictEqual(Chat.labelReferences(String.raw`\label {sec:new}`), [
  {from: 8, to: 15, label: "sec:new"},
]);
assert.deepStrictEqual(Chat.labelReferences("See eq:foo, (eq:foo). xeq:foo eq:foobar eq:foo.v2 unknown.", ["eq:foo"]), [
  {from: 4, to: 10, label: "eq:foo"},
  {from: 13, to: 19, label: "eq:foo"},
]);
assert.deepStrictEqual(Chat.labelReferences("Use eq:a+b[1] and x.*(y).", ["eq:a+b[1]", "x.*(y)"]), [
  {from: 4, to: 13, label: "eq:a+b[1]"},
  {from: 18, to: 24, label: "x.*(y)"},
]);
assert.deepStrictEqual(Chat.labelReferences("unknown eq:missing", ["eq:known"]), []);
assert.deepStrictEqual(Chat.labelReferences(String.raw`\ref{a} \eqref{b} \autoref{c} \cref{d,e} \Cref{f} \pageref{g}`)
  .map((r) => r.label), ["a", "b", "c", "d", "e", "f", "g"]);
assert.deepStrictEqual(Chat.labelReferences(String.raw`\ref* { eq:foo } and eq:foo`, ["eq:foo"]), [
  {from: 8, to: 14, label: "eq:foo"},
  {from: 21, to: 27, label: "eq:foo"},
]);
assert.deepStrictEqual(Chat.labelReferences(String.raw`\label{foo,bar}`), [
  {from: 7, to: 14, label: "foo,bar"},
]);
assert.deepStrictEqual(Chat.labelReferences(String.raw`\ref{<img onerror="x">}`), [
  {from: 5, to: 22, label: '<img onerror="x">'},
]);
assert.deepStrictEqual(Chat.labelReferences(null, null), []);
console.log("LaTeX chat label checks passed");
