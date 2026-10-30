import json
import appdirs
import os
import shutil
from typing import Any

import logging
logger = logging.getLogger(__name__)

DIRECTORY = os.path.dirname(os.path.realpath(__file__))
user_config_path = os.path.join(appdirs.user_config_dir(), 'tracklater.json')


class Dummy(object):
    pass


settings_wrapper: Any = Dummy()

if not os.path.exists(user_config_path):
    try:
        os.mkdir(appdirs.user_config_dir())
    except FileExistsError:
        pass
    shutil.copy(os.path.join(DIRECTORY, 'example_settings.json'), user_config_path)
    logger.error("No user settings file! Modify the example settings created (path: %s)",
                 user_config_path)

try:
    with open(user_config_path, 'rb') as f:
        for key, value in json.load(f).items():
            setattr(settings_wrapper, key, value)
except json.JSONDecodeError as e:
    logger.exception("Error reading settings file: %s", e)


def helper(module, key, group='global', default=None):
    # Try given group first
    if group in getattr(settings_wrapper, module):
        _tmp_dict = getattr(settings_wrapper, module)[group]
        if key in _tmp_dict:
            return _tmp_dict[key]
    # Try with 'global' group
    if 'global' in getattr(settings_wrapper, module):
        _tmp_dict = getattr(settings_wrapper, module)['global']
        if key in _tmp_dict:
            return _tmp_dict[key]
    if default is not None:
        return default

    raise KeyError('No setting "{}" for module {} found'.format(key, module))


settings_wrapper.helper = helper
