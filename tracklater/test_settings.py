
TESTING = True

ENABLED_MODULES = ['jira', 'gitmodule', 'slack', 'taiga', 'kimai', 'thyme']

from datetime import datetime, timedelta

OVERRIDE_START = datetime.fromtimestamp(
    1234560
) - timedelta(days=4)
OVERRIDE_END = datetime.fromtimestamp(
    1234560
) + timedelta(days=4)

UI_SETTINGS = {
    'kimai': {
        'global': '#FF7043'
    },
    'toggl': {
        'global': '#E01A22'
    },
    'thyme': {
        'global': '#1aef65'
    },
    'gitmodule': {
        'global': '#F44D27'
    },
    'slack': {
        'global': '#4A154B'
    }
}

# Test settings for Jira
JIRA = {
    'group1': {
        'CREDENTIALS': ('', ''),
        'URL': 'mock://jira.test',
        'PROJECT_KEY': 'TEST',
    }
}

# Test settings for Git
GIT = {
    'global': {
        'EMAILS': ['test.person@email.com'],
    },
    'group1': {
        'REPOS': ['path1', 'path2']
    },
    'group2': {
        'REPOS': ['path3']
    },
}

# Test settings for Slack
SLACK = {
    'global': {
        'API_KEY': '',
        'USER_ID': '1',
    }
}

# Test settings for Slack
TAIGA = {
    'global': {
        'CREDENTIALS': 'test'
    },
    'group2': {
        'project_slug': 'test'
    }
}

LOCAL = {
    'group1': {
        'NAME': 'First Group',
        'PROJECTS': {
            'Development': 'default',
            'Bug fixing': 'bug',
        }
    },
    'group2': {
        'NAME': 'Second Group',
        'PROJECTS': {
            'Development': 'default',
        }
    },
}

TOGGL = {
    'global': {
        'API_KEY': 'your-api-key'
    },
    'group1': {
        'NAME': 'First Client',
        'PROJECTS': {
            'Development': 'default',
            'Bug fixing': 'bug',
        }
    },
    'group2': {
        'NAME': 'Second Client',
        'PROJECTS': {
            'Development': 'default',
            'Bug fixing': 'default',
        }
    }
}

THYME = {  # type: ignore
    'global': {
    }
}


KIMAI = {
    'global': {
        'URL': 'https://kimai.example.com',
        'API_KEY': 'test-token',
    },
    'group1': {
        'NAME': 'First Client',
        'PROJECTS': {
            'Development': 'default',
            'Bug fixing': 'bug',
        },
        'ACTIVITY': 7,
    },
    'group2': {
        'NAME': 'Second Client',
        'PROJECTS': {
            'Development': 'default',
        },
    },
}
TIMEZONE = 'Europe/Helsinki'
