# Agent guide — TrackLater

A Flask app that shows a day's clues (ActivityWatch, git, Kimai, …) on a timeline and bills time
from them. Settings live in `~/.config/tracklater.json`, never in the repo; never commit API keys.

## Running the tests

Python 3.12 with uv, from the repository root:

    uv run --no-project --python 3.12 --with-requirements requirements-testing.txt pytest -q -p no:cacheprovider

Prove a pull request's head with that command as an `agentdeck job --verification`.

The suite is green, and so is `mypy tracklater`. The `Python application` GitHub workflow runs
flake8, mypy and pytest on Python 3.12 on every push; keep all three passing.

## The running copy

bazzite runs TrackLater as the user service `tracklater.service` from
`~/.local/share/tracklater/src` (https://bazzite.taile8d16e.ts.net:5012). A merge does not update
it; pull that checkout and `systemctl --user restart tracklater` when asked.
