# TrackLater

![](https://i.imgur.com/PgBb20C.png)

<sub>Screenshot has green/red colors for my current two projects/clients. Commits, thyme entries and slack messages are automatically colored.</sub>

Forgot to track your time for work? TrackLater helps you track time after-the-fact by combining clues and showing your day on a simple timeline view.

The initial version supports fetching clues from
* [Thyme](https://github.com/sourcegraph/thyme)
* [ActivityWatch](https://github.com/ActivityWatch)
* Git
* Slack

Time entries can be exported to
* Kimai
* Toggl
* Clockify

Issues and projects/clients can be fetched from
* Jira
* Taiga
* GitHub
* Toggl (projects/clients)
* Kimai (customers/projects)

# Background

Everyone who uses time-tracking for their work knows that it can be a real pain, especially if you're a forgetful person like me. I pretty much never remember to start my timers, and when I do, I for sure will not remember to turn them off.

When working with multiple clients, it can be crucial (billing-wise) to track your time correctly and to be able to differentiate all tasks by client. For people that work 9-5 in-office for the same client and without need to track each task separately this app is probably overkill.

With this in mind, I built a basic app to use Thyme for passive time-tracking, and Toggl-api for exporting. I quickly found that my workflow was substantially improved by only having to think about time-tracking 1-2 times per week. I've now used this app for about a year, building a new timemodule every now and then.

TrackLater offers a basic set of features to help all time-trackers get their timesheets in order:
* A timeline overview, which is usually missing from tracking software
* Easily add time entries, with automatically detected projects and responsive UI
* Get all your breadcrumbs, tracks, clues, footsteps in one place

# Implementation notes

Every module separates their issues, time-entries and projects by *group*. This makes inter-module communication simple: e.g. commits made in the git repository for *group* x will be attributed to the corresponding Toggl project for *group* x.

*Groups* are arbitrary and decided by the user when creating their settings file. A good way to choose your amount of *group*s
is to create a *group* for each client/work project.

While all modules are optional, an ~important backbone for TimeLater is [thyme](https://github.com/sourcegraph/thyme)~.
UPDATE: I couldn't get thyme working on my new mac, so the new "backbone" will be ActivityWatch.
The thyme module assumes an implementation where every day is stored in a file named `YYYY-MM-DD.json`. It's recommended to set up an automatic thyme tracking script for this.

I'm using a basic script to run thyme. It has evolved a bit after about a year of tracking: Sometimes thyme fails tracking and corrupts the whole file,
so I definitely recommend using this script. https://gist.github.com/Eerovil/36d109d531659d24bfafea7111b12e90

To run thyme automatically every 20 seconds you can add this to your crontab. Windows users can probably use services (don't quote me on this).
```
* * * * * DISPLAY=:0 /home/eero/Documents/thyme/track-thyme-log.sh
* * * * * ( sleep 20; DISPLAY=:0 /home/eero/Documents/thyme/track-thyme-log.sh )
* * * * * ( sleep 40; DISPLAY=:0 /home/eero/Documents/thyme/track-thyme-log.sh )
```

# Running

Install inside a virtualenv from PyPI. After first run & page load the example configuration should
be created at `~/.config/tracklater.json` (Windows and Mac configs found somewhere else, check [here](https://github.com/ActiveState/appdirs)).

```
mkvirtualenv tracklater -p python3.7
pip install tracklater
tracklater
```

or

Clone the repository, install inside a virtualenv and run:
```
git clone git@github.com:Eerovil/TrackLater.git
cd TrackLater
mkvirtualenv tracklater -p python3.7 -a .
pip install .
tracklater
```

Additional example command to start the server. Must be run in the root directory.
```
FLASK_APP=tracklater python -m flask run
```

# Usage

Select time entries from thyme and click export.

You can also double click on the timeline to create entries. Edit by selecting, dragging etc.

# Contributing

Building and running the project is easy, as you can simply clone the repo and start making PRs.

If your workflow is not exactly like mine and you need additional functionality, please create an issue and we can start working on supporting your required modules.

Ideas for future support:
* Jira time tracking
* Maybe a Chrome page history parser?

# Settings guide

Create a file called `user_settings.py` to the root folder (containing `app.py`)

To load test settings you can add `from test_settings import *` to the end of the file. This will use test data and no actual API calls will be made.

Each module has their own settings dict, containing a settings dict for each group. There is also
a `global` key for non-group specific settings.

This example settings file contains two groups: `group1` and `group2`.

In the example workers workflow, `group1`'s issues are fetched from Jira while `group2`'s issues are from Taiga.io,
so you will find that the JIRA settings have no `group2` key and TAIGA settings has no `group1` key.

Time tracking (for billing) is done through Toggl. Also, both groups happen to have their own workspaces on slack, and obviously their own git repositories.

```

# edit to your liking and save as ~/.config/tracklater.json. Remove the comments

{
    "TESTING": false,
    "ENABLED_MODULES": [
        "activitywatch",
        "claude",
        "thyme",
        "gitmodule",
        "toggl",
        "clockify",
        "taiga",
        "jira",
        "slack"
    ],

    "UI_SETTINGS": {
        "toggl": {
            "global": "#E01A22"
        },
        "thyme": {
            "global": "#1aef65"
        },
        "gitmodule": {
            "global": "#F44D27"
        },
        "slack": {
            "global": "#4A154B"
        }
    },
    "TOGGL": {
        "global": {
            "API_KEY": "your-api-key"
        },
        "group1": {
            "NAME": "First Group",
            "PROJECTS": {
                "Development": "default",
                "Bug fixing": "bug"
            }
        },
        "group2": {
            "NAME": "Second Group",
            "PROJECTS": {
                "Development": "default",
                "Bug fixing": "default"
            }
        }
    },

    "CLOCKIFY": {
        "global": {
            "API_KEY": "",
            "WORKSPACE": "workspace-id" # This is optional. (Only one workspace is supported!)
        },
        "group1": {
            "NAME": "Red",
            "PROJECTS": {
                "Red": "default"
            }
        },
        "group2": {
            "NAME": "Blue",
            "PROJECTS": {
                "Blue": "default"
            }
        }
    },


    "GIT": {
        "global": {
            # Only commits made by users with EMAILS will be shown
            "EMAILS": ["firstname.lastname@email.com"]
        },
        "group1": {
            # Full path to the git repo
            "REPOS": ["/full/path/to/group1/repo"]
        },
        "group2": {
            "REPOS": ["/full/path/to/group2/repo"]
        }
    },

    "JIRA": {
        "group1": {
            # Each group must have these settings
            "CREDENTIALS": ["username", "password"],
            "URL": "https://group1.atlassian.net",
            "PROJECT_KEY": "DEV"
        }
    },

    "TAIGA": {
        "global": {
            "CREDENTIALS": ["username", "password"]
        },
        "group2": {
            # project_slug can be found in the URL
            "project_slug": "username-group2"
        }
    },

    "THYME": {
        "global": {
            # Directory containing the json files generated by thyme
            "DIR": "/full/path/to/thyme/dir"
        }
    },

    "CLAUDE": {
        # Claude Code chats as hints, one entry per burst of messages.
        # Transcripts are read from DIR (default ~/.claude/projects) and never sent anywhere.
        "global": {
            "IDLE": 900,  # a gap this long (seconds) starts a new entry
            "INCLUDE_UNGROUPED": false  # true also shows chats outside every group's DIRS
        },
        # A chat run in (or under) one of these folders belongs to the group.
        # Without any DIRS, the GIT groups' REPOS are used instead.
        "group1": {
            "DIRS": ["/full/path/to/group1/repo"]
        }
    },

    "ACTIVITYWATCH": {
        "global": {
            "EVENTS_URL": "http://host.docker.internal:5600/api/0/buckets/aw-watcher-window_Eeros-MacBook-Air.local/events",
            "IDLE": 900,
            "CUTOFF": 300
        },
    },

    "GITHUB": {
        "global": {
            "TOKEN": "token" # needs permissions specified here: https://developer.github.com/v4/guides/forming-calls/#authenticating-with-graphql
        },
        "group1": {
            "repo": ["owner", "repo1"]
        },
        "group2": {
            "repo": ["owner", "repo2"]
        }
    },

    "SLACK": {
        # Each group should contain a workspace to match all messager to a group
        "global": {
            # Global catch-all workspace for all groups
            "API_KEY": "legacy-slack-api-key-global",
            "USER_ID": "your-user-id"
        },
        "group2": {
            # Messages in this workspace will be matched to group2
            "API_KEY": "legacy-slack-api-key-group2",
            "USER_ID": "your-user-id"
        }
    }
}

```
