const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.resolve(__dirname, "../../module/gg_modifier/panel/index.html"), "utf8");
const code = html.slice(html.indexOf("function fmtNum("), html.indexOf("function renderStatus("));
const elements = {};
const context = { $: id => (elements[id] ??= {}) };
vm.runInNewContext(code, context);
context.renderResources({
  latest: { oil: 9000, coin: 270000, time: "test", source: "goto_main" },
  samples: Array(200).fill({ oil: 9666, coin: 269388 }),
  count: 784, window_hours: 48, range: { oil: [3501, 14140], coin: [217432, 289409] },
});
assert.match(elements.resSummary.textContent, /14,140/);
assert.match(elements.resSummary.textContent, /217,432/);
assert.match(elements.resSummary.textContent, /784/);
context.renderResources({ count: 0, samples: [] });
assert.equal(elements.resSummary.textContent, "");
assert.equal(elements.stOil.textContent, "—");
console.log("Resource view passed for a full window and an empty window.");
