import os
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_left_edge_drag_resizes_kimai_entry():
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
const methods = sandbox.daytimeline.methods;

function drag(newStart, newEnd) {
  const entry = {
    module: 'kimai',
    start_time: '2026-09-29T09:00:00',
    end_time: '2026-09-29T11:00:00',
  };
  const ctx = {
    entries: [entry],
    modules: {kimai: {capabilities: ['updateentry']}},
    timelineEndForEntry: methods.timelineEndForEntry,
    $emit() {},
    $store: {commit() {}},
  };
  const drawnEnd = methods.timelineEndForEntry(entry);
  const item = {
    id: 0, group: 'kimai',
    start: new Date(newStart),
    end: newEnd ? new Date(newEnd) : drawnEnd,
  };
  let result;
  methods.onMove.call(ctx, item, (r) => { result = r; });
  return {entry, result};
}

// Left edge dragged earlier: start moves, end stays.
let r = drag('2026-09-29T08:00:00');
assert.strictEqual(r.entry.start_time.getTime(), new Date('2026-09-29T08:00:00').getTime());
assert.strictEqual(r.entry.end_time.getTime(), new Date('2026-09-29T11:00:00').getTime());
assert.strictEqual(r.result.end.getTime(), new Date('2026-09-29T11:00:01').getTime());

// Left edge dragged later: entry shortens.
r = drag('2026-09-29T10:00:00');
assert.strictEqual(r.entry.start_time.getTime(), new Date('2026-09-29T10:00:00').getTime());
assert.strictEqual(r.entry.end_time.getTime(), new Date('2026-09-29T11:00:00').getTime());

// Whole-bar move still keeps the duration.
r = drag('2026-09-29T12:00:00', '2026-09-29T14:00:01');
assert.strictEqual(r.entry.start_time.getTime(), new Date('2026-09-29T12:00:00').getTime());
assert.strictEqual(r.entry.end_time.getTime(), new Date('2026-09-29T14:00:00').getTime());
'''
    env = dict(os.environ, TZ='Europe/Helsinki')
    subprocess.run(['node', '-e', script, day_path], check=True, env=env)
