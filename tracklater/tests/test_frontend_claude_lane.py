import os
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_only_a_claude_row_without_overlaps_is_set_to_one_lane():
    day_path = os.path.join(ROOT, 'tracklater', 'static', 'daytimeline.vue.js')
    script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const sandbox = {Vue: {component: (name, definition) => {
  sandbox[name] = definition;
  return definition;
}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const {methods, computed} = sandbox.daytimeline;
const module = {capabilities: ['entries'], color: {}};
const ctx = {
  modules: {claude: module, kimai: module},
  timelineEndForEntry: methods.timelineEndForEntry,
  claudeInOneLane: methods.claudeInOneLane,
  logCommitsOutsideLocalEntries() {},
};
const claude = (start, end) => ({
  module: 'claude',
  start_time: `2026-11-12T${start}`,
  end_time: `2026-11-12T${end}`,
});

const groups = computed.groups.call(ctx);
assert.strictEqual(groups.map((g) => g.id).join(), 'claude,kimai');
const items = methods.entriesToItems.call(ctx, ['claude', 'kimai'].map((name) => ({
  module: name,
  start_time: '2026-11-12T09:00:00',
  end_time: '2026-11-12T09:02:00',
})));
assert.strictEqual(items[0].subgroup, 'lane');
// vis stacks a subgroup only when subgroupStack names it, and reads
// subgroupStack only when something in it is true.
assert.ok(Object.values(groups[0].subgroupStack).includes(true));
assert.ok(!groups[0].subgroupStack[items[0].subgroup]);
assert.strictEqual(items[1].subgroup, undefined);
assert.strictEqual(groups[1].subgroupStack, undefined);

// Blocks that only touch are one lane, in any order.
const touching = [claude('09:02:00', '09:04:00'), claude('09:00:00', '09:02:00')];
for (const item of methods.entriesToItems.call(ctx, touching)) {
  assert.strictEqual(item.subgroup, 'lane');
}

// Rows that overlap in time keep stacking, so none of them is hidden.
const overlapping = [claude('09:00:00', '11:00:00'), claude('10:00:00', '10:30:00')];
for (const item of methods.entriesToItems.call(ctx, overlapping)) {
  assert.ok(groups[0].subgroupStack[item.subgroup]);
}
'''
    result = subprocess.run(
        ['node', '-e', script, day_path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
