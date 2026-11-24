import os
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


needs_node = pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')

PRELUDE = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const sandbox = {console, Vue: {component: (name, definition) => {
  sandbox[name] = definition;
  return definition;
}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const {methods, computed} = sandbox.daytimeline;
const module = {capabilities: ['entries'], color: {}};
const ctx = Object.assign({}, methods, {
  modules: {
    claude: module, kimai: module, activitywatch: module,
    gitmodule: {capabilities: ['entries'], color: {metso: 'blue', global: 'gray'}},
  },
  logCommitsOutsideLocalEntries() {},
});
const groups = {};
computed.groups.call(ctx).forEach((g) => { groups[g.id] = g; });
const at = (time) => `2026-11-12T${time}`;
const span = (name, start, end) => ({
  module: name, start_time: at(start), end_time: end && at(end),
});
const commit = (time, group, text) => ({
  module: 'gitmodule', start_time: at(time), group, text,
});
const draw = (entries) => Array.from(methods.entriesToItems.call(ctx, entries));
// vis stacks a subgroup only when subgroupStack names it, and reads
// subgroupStack only when something in it is true.
const stacks = (item) => {
  const named = groups[item.group].subgroupStack;
  return !named || !Object.values(named).includes(true) || named[item.subgroup] === true;
};
"""


def run(script):
    day_path = os.path.join(ROOT, 'tracklater', 'static', 'daytimeline.vue.js')
    result = subprocess.run(
        ['node', '-e', PRELUDE + script, day_path], capture_output=True, text=True,
        env=dict(os.environ, TZ='Europe/Helsinki'))
    assert result.returncode == 0, result.stderr


@needs_node
def test_rows_are_one_lane_until_their_entries_overlap():
    run(r"""
for (const name of ['claude', 'kimai']) {
  // Entries that only touch are one lane, in any order.
  const touching = [span(name, '09:30:00', '10:00:00'), span(name, '09:00:00', '09:30:00')];
  assert.ok(draw(touching).every((item) => !stacks(item)), name);
  // Overlapping ones stack, so none of them is hidden.
  const overlapping = [span(name, '09:00:00', '11:00:00'), span(name, '10:00:00', '10:30:00')];
  assert.ok(draw(overlapping).every(stacks), name);
  // So does one still running, which has no end.
  assert.ok(draw(touching.concat([span(name, '09:10:00')])).every(stacks), name);
}
// One row's overlap does not stack another, and other rows stack as before.
const mixed = draw([
  span('claude', '09:00:00', '11:00:00'), span('claude', '10:00:00', '10:30:00'),
  span('kimai', '09:00:00', '10:00:00'), span('activitywatch', '09:00:00', '10:00:00'),
]);
assert.deepStrictEqual(mixed.map(stacks), [true, true, false, true]);
""")


@needs_node
def test_moving_a_kimai_entry_onto_another_restacks_the_row():
    run(r"""
function move(start, end, entries) {
  let redrawn = null;
  const day = Object.assign({}, ctx, {
    entries,
    modules: {kimai: {capabilities: ['entries', 'updateentry'], color: {}}},
    $emit() {}, $store: {commit() {}}, $nextTick(f) { f(); },
  });
  Object.defineProperty(day, 'items', {set(items) { redrawn = items; }});
  methods.onMove.call(day, {
    id: 0, group: 'kimai', start: new Date(at(start)), end: new Date(at(end)),
  }, () => {});
  return redrawn && Array.from(redrawn).map(stacks);
}
const pair = () => [span('kimai', '09:00:00', '10:00:00'), span('kimai', '12:00:00', '13:00:00')];
assert.deepStrictEqual(move('12:00:00', '13:00:01', pair()), [true, true]);
assert.strictEqual(move('10:00:00', '11:00:01', pair()), null);
const onTop = [span('kimai', '12:00:00', '13:00:00'), span('kimai', '12:00:00', '13:00:00')];
assert.deepStrictEqual(move('09:00:00', '10:00:01', onTop), [false, false]);
""")


@needs_node
def test_commits_are_one_bar_per_client_in_each_ten_minutes():
    run(r"""
const entries = [
  span('kimai', '09:00:00', '10:00:00'),
  commit('09:09:59', 'metso', 'late\nfile.py'),
  commit('09:01:00', 'metso', 'a <b> & c'),
  commit('09:05:00', 'tilhi', 'other'),
  commit('09:10:00', 'metso', 'next slot'),
  commit('09:20:00', null, null),
];
const bars = draw(entries).filter((item) => item.group === 'gitmodule');
const time = (date) => new Date(date).toTimeString().slice(0, 8);
assert.deepStrictEqual(bars.map((b) => [time(b.start), time(b.end)]), [
  ['09:00:00', '09:05:00'],
  ['09:05:00', '09:10:00'],
  ['09:10:00', '09:20:00'],
  ['09:20:00', '09:30:00'],
]);
assert.strictEqual(
  bars[0].title, 'metso 2 commits<br />09:01 a &lt;b&gt; &amp; c<br />09:09 late');
assert.strictEqual(bars[3].title, '1 commit<br />09:20 ');
assert.ok(bars[0].style.includes('blue') && bars[1].style.includes('gray'));
assert.ok(bars.every((bar) => !stacks(bar) && bar.editable.updateTime === false));
// Clicking a bar picks one of its commits.
assert.strictEqual(entries[bars[1].id].text, 'other');
assert.strictEqual(new Set(draw(entries).map((i) => i.id)).size, 5);

const many = draw(Array.from({length: 40}, (_, i) =>
  commit(`09:00:${String(i).padStart(2, '0')}`, 'metso', 'x')));
assert.strictEqual(many.length, 1);
assert.ok(many[0].title.endsWith('... and 25 more'));
""")
