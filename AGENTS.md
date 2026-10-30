# Agent guide — TrackLater

A Flask app that shows a day's clues (ActivityWatch, git, Kimai, …) on a timeline and bills time
from them. Settings live in `~/.config/tracklater.json`, never in the repo; never commit API keys.

## Running the tests

Python 3.12 with uv, from the repository root:

    uv run --no-project --python 3.12 --with-requirements requirements-testing.txt pytest -q -p no:cacheprovider

Prove a pull request's head with that command as an `agentdeck job --verification`.

As of the activitywatch merge (f9df2d6) the suite is not green: 6 tests fail and 6 error
(`test_app.py` registers SQLAlchemy twice, the Kimai provider has no `_test_fixtures`, some
populate tests call ActivityWatch on 127.0.0.1:5600). Do not call those failures yours; fixing
them is its own Work.

## The running copy

bazzite runs TrackLater as the user service `tracklater.service` from
`~/.local/share/tracklater/src` (https://bazzite.taile8d16e.ts.net:5012). A merge does not update
it; pull that checkout and `systemctl --user restart tracklater` when asked.
