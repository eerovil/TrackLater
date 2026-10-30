from typing import Dict, List, Optional, Set
import git
import pytz
import os
import json
import subprocess
from datetime import datetime

from tracklater.utils import obj_from_dict
from tracklater import settings
from tracklater.timemodules.interfaces import EntryMixin, AbstractParser, AbstractProvider
from tracklater.models import Entry

import logging
logger = logging.getLogger(__name__)


def get_setting(key, default=None, group='global'):
    return settings.helper('GIT', key, group=group, default=default)


FIXTURE_DIR = os.path.dirname(os.path.realpath(__file__)) + "/fixture"
FETCH_TIMEOUT = 60  # seconds, per repo


def _sync_repo(repo_path: str) -> None:
    """Refresh a clone before reading so commits pushed elsewhere are seen.

    Runs `git fetch --all` (or the configured GIT.FETCH_COMMAND) — fetch only,
    never a merge or working-tree change, so there is no conflict risk. Runs
    non-interactively (GIT_TERMINAL_PROMPT=0) so a missing credential can't hang
    the parse, and any failure is logged and ignored so one unreachable remote
    doesn't sink the whole run. Gate with GIT.global.FETCH (default off; needs the
    container to have a usable SSH credential — e.g. a forwarded agent)."""
    command = get_setting('FETCH_COMMAND', default='')
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    # Use the mounted key and trust-on-first-use for host keys, so a container
    # without a pre-seeded known_hosts can still fetch over SSH. Overridable.
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o StrictHostKeyChecking=accept-new')
    try:
        if command:
            proc = subprocess.run(
                command, cwd=repo_path, env=env, shell=True,
                timeout=FETCH_TIMEOUT, capture_output=True,
            )
        else:
            proc = subprocess.run(
                ['git', 'fetch', '--all', '--quiet'], cwd=repo_path, env=env,
                timeout=FETCH_TIMEOUT, capture_output=True,
            )
        if proc.returncode != 0:
            logger.warning(
                "git sync for %s exited %s: %s", repo_path, proc.returncode,
                (proc.stderr or b'')[:300],
            )
    except Exception as e:  # noqa: BLE001 - sync is best-effort
        logger.warning("git sync failed for %s: %s", repo_path, e)


def git_time_to_datetime(_datetime):
    # Git has timezone-aware unix timestamps, convert that to a UTC datetime
    return _datetime.astimezone(pytz.utc).replace(tzinfo=None)


def _commit_id(commit) -> str:
    hexsha = getattr(commit, 'hexsha', None)
    if hexsha:
        return hexsha
    message = (getattr(commit, 'message', None) or '').strip()
    subject = message.split('\n', 1)[0] if message else ''
    authored = getattr(commit, 'authored_datetime', None)
    if authored is not None:
        return '{}|{}'.format(git_time_to_datetime(authored).isoformat(), subject)
    return subject or 'unknown'


def _all_changed_files(commit, files_map: Optional[Dict[str, List[str]]] = None) -> List[str]:
    if getattr(commit, 'changed_files', None):
        return list(commit.changed_files)
    if files_map is not None:
        hexsha = getattr(commit, 'hexsha', None)
        if hexsha in files_map:
            return list(files_map[hexsha])
    try:
        return list(commit.stats.files.keys())
    except Exception:
        logger.debug("Could not read changed files for commit", exc_info=True)
        return []


def _changed_files_map(repo: git.Repo, start_date=None) -> Dict[str, List[str]]:
    """Changed files for every commit in range, in one `git log` instead of a
    `git diff` per commit (which dominated parse time on large repos)."""
    args = ['--all', '--format=%x00%H', '--name-only', '--diff-merges=first-parent']
    if start_date:
        args.append('--since={}'.format(start_date.isoformat()))
    try:
        out = repo.git.log(*args)
    except Exception:
        logger.debug("Could not batch changed files", exc_info=True)
        return {}
    files_map: Dict[str, List[str]] = {}
    for block in out.split('\x00'):
        lines = block.splitlines()
        if not lines:
            continue
        files_map[lines[0].strip()] = [line for line in lines[1:] if line.strip()]
    return files_map


def _exclusive_rev(head_name: str, other_head_names: List[str]) -> List[str]:
    """Revision list: commits reachable from head but not from other heads."""
    rev: List[str] = [head_name]
    rev.extend('^{}'.format(name) for name in other_head_names)
    return rev


NAME_REV_CHUNK = 200


class BranchResolver:
    """Resolve display branches for many commits with a handful of git calls.

    Same answer as a per-commit lookup, but the main/master ancestry test
    becomes one `rev-list` and the fallback becomes one `name-rev` per chunk of
    commits, instead of two subprocesses for every commit.
    """

    def __init__(self, repo: git.Repo) -> None:
        self.repo = repo
        self.tips: Dict[str, List[str]] = {}
        names = []
        for head in repo.heads:
            names.append(head.name)
            try:
                self.tips.setdefault(head.commit.hexsha, []).append(head.name)
            except Exception:
                logger.debug('Could not read tip of %s', head.name, exc_info=True)
        self.preferred = next((n for n in ('main', 'master') if n in names), None)
        self._preferred_commits: Optional[Set[str]] = None
        self._names: Dict[str, str] = {}
        self._attempted: Set[str] = set()

    def preferred_commits(self) -> Set[str]:
        if self._preferred_commits is None:
            commits: Set[str] = set()
            if self.preferred:
                try:
                    commits = set(self.repo.git.rev_list(self.preferred).split())
                except Exception:
                    logger.debug('rev-list %s failed', self.preferred, exc_info=True)
            self._preferred_commits = commits
        return self._preferred_commits

    def prefetch(self, hexshas: List[str]) -> None:
        """Resolve names for the commits the cheap lookups won't cover."""
        known = self.preferred_commits()
        todo = [
            sha for sha in dict.fromkeys(hexshas)
            if sha not in self.tips and sha not in known and sha not in self._attempted
        ]
        for start in range(0, len(todo), NAME_REV_CHUNK):
            chunk = todo[start:start + NAME_REV_CHUNK]
            try:
                out = self.repo.git.name_rev(
                    '--name-only', '--refs=refs/heads/*', *chunk
                )
            except Exception:
                logger.debug('name-rev failed for %s commits', len(chunk), exc_info=True)
                continue
            # Remember the attempt either way: a commit git can't name (one
            # reachable only from a remote ref) must not be re-asked per call.
            self._attempted.update(chunk)
            lines = out.splitlines()
            if len(lines) != len(chunk):
                logger.debug('name-rev returned %s names for %s commits',
                             len(lines), len(chunk))
                continue
            for sha, name in zip(chunk, lines):
                name = name.strip()
                if name and name != 'undefined':
                    self._names[sha] = name.split('~')[0].split('^')[0]

    def branch_for(self, commit) -> str:
        hexsha = commit.hexsha
        tip_branches = self.tips.get(hexsha, [])
        if len(tip_branches) == 1:
            return tip_branches[0]
        if tip_branches:
            for name in tip_branches:
                if name not in ('main', 'master'):
                    return name
            return tip_branches[0]
        if self.preferred and hexsha in self.preferred_commits():
            return self.preferred
        if hexsha not in self._attempted:
            self.prefetch([hexsha])
        resolved = self._names.get(hexsha)
        if resolved:
            return resolved
        if not self.repo.head.is_detached:
            return self.repo.active_branch.name
        return 'HEAD'


def format_commit_entry(
    repo_name: str, branch: str, commit, max_files: int = 12,
    files_map: Optional[Dict[str, List[str]]] = None,
) -> str:
    message = commit.message.strip()
    subject = message.split('\n')[0] if message else ''
    header = "{} [{}] - {}".format(repo_name, branch, subject)

    lines = [header]
    all_files = _all_changed_files(commit, files_map)
    shown = all_files[:max_files]
    if shown:
        lines.extend(shown)
        if len(all_files) > len(shown):
            lines.append("... and {} more".format(len(all_files) - len(shown)))

    return "\n".join(lines)


class Parser(EntryMixin, AbstractParser):
    def get_entries(self) -> List[Entry]:
        start_date = self.start_date
        end_date = self.end_date
        log = []
        provider = Provider()
        for group, data in settings.GIT.items():
            for repo_path in data.get('REPOS', []):
                files_map = provider.get_changed_files(repo_path, start_date=start_date)
                for log_entry, branch in provider.get_log_entries(
                        repo_path, start_date=start_date):
                    if log_entry.author.email not in settings.GIT['global']['EMAILS']:
                        continue
                    time = git_time_to_datetime(log_entry.authored_datetime)
                    if time < start_date or time > end_date:
                        continue

                    repo_name = repo_path.split('/')[-1]
                    log.append(Entry(
                        id=_commit_id(log_entry),
                        title="",
                        text=format_commit_entry(
                            repo_name, branch, log_entry, files_map=files_map),
                        start_time=time,
                        group=group,
                    ))
        return log


class Provider(AbstractProvider):
    def get_changed_files(self, repo_path, start_date=None):
        try:
            repo = git.Repo(repo_path)
        except Exception:
            logger.warning(f"Error opening repo {repo_path}")
            return {}
        return _changed_files_map(repo, start_date=start_date)

    def test_get_changed_files(self, repo_path, start_date=None):
        return {}

    def get_log_entries(self, repo_path, start_date=None):
        if get_setting('FETCH', default=False):
            _sync_repo(repo_path)
        try:
            repo = git.Repo(repo_path)
        except Exception:
            logger.warning(f"Error opening repo {repo_path}")
            return

        heads = list(repo.heads)
        if not heads:
            return

        # Walking every head costs a rev-list each, and a repo can carry
        # hundreds of stale ones. A head whose tip predates the window has no
        # commit inside it, so skip it -- both as a walk and as an exclusion.
        if start_date:
            fresh = []
            for head in heads:
                try:
                    if git_time_to_datetime(head.commit.authored_datetime) < start_date:
                        continue
                except Exception:
                    logger.debug('Could not read tip of %s', head.name, exc_info=True)
                fresh.append(head)
            heads = fresh

        seen = set()
        head_names = [head.name for head in heads]

        for head in heads:
            others = [name for name in head_names if name != head.name]
            rev = _exclusive_rev(head.name, others)
            iterator = repo.iter_commits(rev)
            for commit in iterator:
                try:
                    if start_date and git_time_to_datetime(
                        commit.authored_datetime,
                    ) < start_date:
                        break
                except Exception as e:
                    logger.warning(e)
                    continue
                if commit.hexsha in seen:
                    continue
                seen.add(commit.hexsha)
                yield commit, head.name

        iter_kwargs = {}
        if start_date:
            iter_kwargs['since'] = start_date.isoformat()
        remaining = []
        for commit in repo.iter_commits('--all', **iter_kwargs):
            if commit.hexsha in seen:
                continue
            try:
                if start_date and git_time_to_datetime(
                    commit.authored_datetime,
                ) < start_date:
                    continue
            except Exception as e:
                logger.warning(e)
                continue
            seen.add(commit.hexsha)
            remaining.append(commit)

        # Resolve every remaining branch name in one batch rather than two git
        # subprocesses per commit -- this dominated the parse on big repos.
        resolver = BranchResolver(repo)
        resolver.prefetch([commit.hexsha for commit in remaining])
        for commit in remaining:
            yield commit, resolver.branch_for(commit)

    def test_get_log_entries(self, repo_path, start_date=None):
        with open(FIXTURE_DIR + '/git_test_data.json', 'r') as f:
            _git = obj_from_dict(json.load(f))

        for commit in _git.commits:
            branch = 'main'
            if 'Branch 1' in commit.message:
                branch = 'branch1'
            elif 'Branch 2' in commit.message:
                branch = 'branch2'
            yield commit, branch
