import pytest

from typing import Any
from datetime import datetime, timedelta

from tracklater import create_app
from tracklater.models import Entry, Issue, Project


app = create_app()


@pytest.fixture
def client(db):
    # create_app() already registered db on this app, bound to the throwaway
    # database conftest.py chose; start each test from empty tables.
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app.test_client()


def test_app_root(client):
    response = client.get('/')
    assert response.status_code == 200


def test_app_listmodules(client):
    response = client.get('/listmodules')
    assert response.status_code == 200


def test_app_fetchdata(client):
    response = client.get('/fetchdata')
    assert response.status_code == 200


def test_database_entries(client, db: Any):
    from tracklater.timemodules.toggl import Parser
    from tracklater.main import store_parser_to_database
    start_date = datetime.utcnow() - timedelta(days=7)
    end_date = datetime.utcnow()
    parser = Parser(start_date, end_date)
    with app.app_context():
        parser.entries = parser.get_entries()
        store_parser_to_database(parser, 'toggl', start_date, end_date)

        db_entries = Entry.query.all()
        assert len(parser.entries) == len(db_entries)


def test_database_projects(client, db: Any):
    from tracklater.timemodules.toggl import Parser
    from tracklater.main import store_parser_to_database
    start_date = datetime.utcnow() - timedelta(days=7)
    end_date = datetime.utcnow()
    parser = Parser(start_date, end_date)
    with app.app_context():
        parser.projects = parser.get_projects()
        store_parser_to_database(parser, 'toggl', start_date, end_date)

        db_projects = Project.query.all()
        assert len(parser.projects) == len(db_projects)


def test_database_issues(client, db: Any):
    from tracklater.timemodules.jira import Parser
    from tracklater.main import store_parser_to_database
    start_date = datetime.utcnow() - timedelta(days=7)
    end_date = datetime.utcnow()
    parser = Parser(start_date, end_date)
    with app.app_context():
        parser.issues = parser.get_issues()
        store_parser_to_database(parser, 'jira', start_date, end_date)

        db_issues = Issue.query.all()
        assert len(parser.issues) == len(db_issues)


@pytest.mark.skip('Broken from commit 27b780c7d23261669e8e5a997403c6a80d6bbaeb')
def test_database_jira_caching(client, db: Any):
    from tracklater.timemodules.jira import Parser
    from tracklater.main import store_parser_to_database, set_parser_caching_data
    start_date = datetime.utcnow() - timedelta(days=7)
    end_date = datetime.utcnow()
    parser = Parser(start_date, end_date)
    parser.issues = parser.get_issues()
    with app.app_context():
        store_parser_to_database(parser, 'jira', start_date, end_date)

    parser = Parser(start_date, end_date)
    # Skip caching first
    parser.issues = parser.get_issues()
    # We get 6 issues again
    assert len(parser.issues) == 6

    parser = Parser(start_date, end_date)
    with app.app_context():
        set_parser_caching_data(parser, 'jira')  # Fetch caching values
    parser.issues = parser.get_issues()
    # Since all 6 are already in database, get_issues should just run once (check jira.py)
    # Hence, wwith testing data we get the 3 last issues as a response
    assert len(parser.issues) == 3


@pytest.mark.skip('Broken from commit be24bce7a5c3e472c49c1a9e5712712501453ba5')
def test_database_slack_caching(client, db: Any):
    from tracklater.timemodules.slack import Parser
    from tracklater.main import store_parser_to_database, set_parser_caching_data
    start_date = datetime.utcnow() - timedelta(days=7)
    end_date = datetime.utcnow()
    parser = Parser(start_date, end_date)
    parser.entries = parser.get_entries()
    with app.app_context():
        store_parser_to_database(parser, 'slack', start_date, end_date)

    parser = Parser(start_date, end_date)
    # Skip caching first
    parser.entries = parser.get_entries()
    # We get 2 entries again
    assert len(parser.entries) == 2

    parser = Parser(start_date, end_date)
    with app.app_context():
        set_parser_caching_data(parser, 'slack')  # Fetch caching values
    parser.entries = parser.get_entries()
    # Since we already fetched entries for there dates we don't get any new ones
    assert len(parser.entries) == 0

    parser = Parser(start_date, end_date + timedelta(hours=1))
    with app.app_context():
        set_parser_caching_data(parser, 'slack')  # Fetch caching values
    parser.entries = parser.get_entries()
    assert len(parser.entries) == 2
