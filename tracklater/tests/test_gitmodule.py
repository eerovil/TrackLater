from tracklater.timemodules.gitmodule import (
    BranchResolver, Parser, _changed_files_map, _exclusive_rev,
)

import pytest
import os
from datetime import datetime, timedelta

DIRECTORY = os.path.dirname(os.path.realpath(__file__))


@pytest.fixture(autouse=True)
def mock_git(monkeypatch):
    monkeypatch.setattr('tracklater.timemodules.gitmodule.git_time_to_datetime',
                        lambda x: datetime.utcnow() - timedelta(days=4))


@pytest.fixture()
def parser():
    _parser = Parser(datetime.utcnow() - timedelta(days=7), datetime.utcnow())
    _parser.credentials = ('', '')
    return _parser


def test_get_entries(parser):
    data = parser.get_entries()
    assert len(data) == 24
    assert data[0].title == ''


def test_exclusive_rev_excludes_other_heads():
    assert _exclusive_rev('feature', ['main']) == ['feature', '^main']
    assert _exclusive_rev('main', ['feature', 'dev']) == ['main', '^feature', '^dev']
    assert _exclusive_rev('only', []) == ['only']


def test_get_entries_hover_text_only(parser):
    data = parser.get_entries()
    assert data[0].text.startswith('path1 [branch1] - ')
    assert 'src/main.py' in data[0].text
    assert 'README.md' in data[0].text


@pytest.fixture()
def real_repo(tmp_path):
    """A small real repo: main, a merged branch, and an unmerged branch."""
    import git as gitpy
    repo = gitpy.Repo.init(tmp_path, initial_branch='main')
    repo.config_writer().set_value('user', 'name', 'T').release()
    repo.config_writer().set_value('user', 'email', 't@example.com').release()

    def commit(name, message):
        (tmp_path / name).write_text(name)
        repo.index.add([name])
        return repo.index.commit(message)

    base = commit('base.txt', 'base')
    repo.create_head('feature', base).checkout()
    feature = commit('feature.txt', 'feature work')
    repo.heads.main.checkout()
    merged = commit('main.txt', 'main work')
    repo.create_head('unmerged', merged).checkout()
    unmerged = commit('unmerged.txt', 'unmerged work')
    repo.heads.main.checkout()
    return repo, base, feature, merged, unmerged


def test_changed_files_map_matches_commit_stats(real_repo):
    repo, base, feature, merged, unmerged = real_repo
    files_map = _changed_files_map(repo)
    for commit in (feature, merged, unmerged):
        assert files_map[commit.hexsha] == list(commit.stats.files.keys())
    assert files_map[base.hexsha] == ['base.txt']


def test_branch_resolver_names_tips_and_shared_history(real_repo):
    repo, base, feature, merged, unmerged = real_repo
    resolver = BranchResolver(repo)
    resolver.prefetch([c.hexsha for c in (base, feature, merged, unmerged)])

    # Tips resolve to their own branch, non-tip shared history to main.
    assert resolver.branch_for(feature) == 'feature'
    assert resolver.branch_for(unmerged) == 'unmerged'
    assert resolver.branch_for(merged) == 'main'
    assert resolver.branch_for(base) == 'main'


def test_branch_resolver_caches_unnameable_commits(real_repo):
    """A commit git can't name must be asked about once, not on every lookup."""
    repo, base, feature, merged, unmerged = real_repo
    repo.git.checkout('--detach', 'main')
    with open(repo.working_dir + '/loose.txt', 'w') as f:
        f.write('loose')
    repo.index.add(['loose.txt'])
    loose = repo.index.commit('unreachable work')
    repo.git.checkout('main')

    resolver = BranchResolver(repo)
    calls = []
    original = resolver.prefetch
    resolver.prefetch = lambda shas: calls.append(list(shas)) or original(shas)

    names = {resolver.branch_for(loose) for _ in range(3)}
    assert names == {'main'}  # falls back to the checked-out branch
    assert len(calls) == 1
