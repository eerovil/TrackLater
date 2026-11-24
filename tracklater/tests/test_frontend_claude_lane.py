import os
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_only_the_claude_row_is_kept_in_one_lane():
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
  logCommitsOutsideLocalEntries() {},
};

const groups = computed.groups.call(ctx);
assert.deepStrictEqual(groups.map((g) => g.id), ['claude', 'kimai']);
const items = methods.entriesToItems.call(ctx, ['claude', 'kimai'].map((name) => ({
  module: name,
  start_time: '2026-11-12T09:00:00',
  end_time: '2026-11-12T09:02:00',
})));
assert.strictEqual(items[0].subgroup, 'claude');
// vis stacks a subgroup only when subgroupStack names it, and reads
// subgroupStack only when something in it is true.
assert.ok(Object.values(groups[0].subgroupStack).includes(true));
assert.ok(!groups[0].subgroupStack[items[0].subgroup]);
assert.strictEqual(items[1].subgroup, undefined);
assert.strictEqual(groups[1].subgroupStack, undefined);
'''
    result = subprocess.run(
        ['node', '-e', script, day_path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
