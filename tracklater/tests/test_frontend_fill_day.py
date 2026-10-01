import json
import os
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_fill_day_bounds_payload_and_global_loading_state():
    home_path = os.path.join(ROOT, 'tracklater', 'static', 'home.vue.js')
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
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);

const bounds = sandbox.home.methods.dayBounds('2026-03-29');
assert.strictEqual((bounds.to - bounds.from) / 3600000, 23);
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(sandbox.home.methods.populatePayload(
    bounds.from, bounds.to, true,
  ))),
  {from: bounds.from, to: bounds.to, replace_existing: true, single_day: true,
   skip_filled_days: false},
);
assert.strictEqual(
  sandbox.home.methods.populatePayload(bounds.from, bounds.to, false).skip_filled_days,
  true,
);
assert.strictEqual(
  sandbox.daytimeline.computed.populateLoading.call({
    $store: {state: {loading: {populatelocal: false}}},
  }), false,
);
assert.strictEqual(
  sandbox.daytimeline.computed.populateLoading.call({
    $store: {state: {loading: {populatelocal: true}}},
  }), true,
);
'''
    env = dict(os.environ, TZ='Europe/Helsinki')
    subprocess.run(
        ['node', '-e', script, home_path, day_path],
        check=True,
        env=env,
    )
