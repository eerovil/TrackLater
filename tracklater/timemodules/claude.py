"""Claude Code chats as timeline hints.

Reads the JSONL transcripts Claude Code writes under ~/.claude/projects and
turns each chat's activity into entries on a `claude` row: messages closer than
IDLE seconds form one entry. The chat's working directory picks the group, the
same way GIT.<group>.REPOS does. Hints only: nothing here bills, and transcript
text never leaves this machine (it is only stored in the local database).
"""
import calendar
import glob
import json
import os
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from tracklater import settings
from tracklater.utils import parse_time
from tracklater.timemodules.interfaces import EntryMixin, AbstractParser, AbstractProvider
from tracklater.models import Entry

import logging
logger = logging.getLogger(__name__)


DEFAULTS = {
    'DIR': '~/.claude/projects',
    'IDLE': 900,
    # A lone message still gets a visible block on the timeline.
    'MIN_DURATION': 60,
    'MAX_PROMPTS': 5,
    # Chats outside every mapped directory (tooling, side projects) are noise
    # for billing; set True to show them ungrouped.
    'INCLUDE_UNGROUPED': False,
}
PROMPT_CHARS = 120
MESSAGE_TYPES = ('user', 'assistant')
# Messages AgentDeck sends a chat on its own (briefs, wake-ons, review events,
# job results): never listed as typed prompts. Only a brief can be the hint.
DECK_PREFIXES = (
    'Context:', 'AgentDeck', 'Continue the work from this AgentDeck chat',
    'Your queued job', 'You are AgentDeck',
)
# AgentDeck's wrapper around an answer the person typed; the answer is kept.
ANSWER_PREFIX = 'The person answered from the question box:'


def get_setting(key, default=None, group='global') -> Any:
    if not hasattr(settings, 'CLAUDE'):
        return default
    return settings.helper('CLAUDE', key, group=group, default=default)


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path)).rstrip('/')


def group_dirs() -> List[Tuple[str, str]]:
    """(directory, group) pairs, longest directory first so the closest wins.

    CLAUDE.<group>.DIRS when any group sets it, else GIT.<group>.REPOS, so a
    setup that already maps repositories needs no new settings."""
    pairs: List[Tuple[str, str]] = []
    claude = getattr(settings, 'CLAUDE', {}) or {}
    for group, data in claude.items():
        if group == 'global':
            continue
        for path in (data or {}).get('DIRS', []):
            pairs.append((_real(path), group))
    if not pairs:
        git = getattr(settings, 'GIT', {}) or {}
        for group, data in git.items():
            if group == 'global':
                continue
            for path in (data or {}).get('REPOS', []):
                pairs.append((_real(path), group))
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def group_for_cwd(cwd: Optional[str], pairs: List[Tuple[str, str]]) -> Optional[str]:
    if not cwd:
        return None
    cwd = _real(cwd)
    for path, group in pairs:
        if cwd == path or cwd.startswith(path + '/'):
            return group
    return None


def prompt_text(message: dict) -> Optional[str]:
    """The typed text of a user message, or None for tool results and
    injected context (system reminders, slash-command output)."""
    if message.get('isMeta'):
        return None
    content = (message.get('message') or {}).get('content')
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [
            block.get('text', '') for block in content
            if isinstance(block, dict) and block.get('type') == 'text'
        ]
    else:
        return None
    parts = [part.strip() for part in parts if part and not part.lstrip().startswith('<')]
    text = ' '.join(' '.join(parts).split())
    return text or None


def listed_prompt(prompt: Optional[str]) -> Optional[str]:
    """A prompt as listed in an entry, or None for AgentDeck's own messages."""
    if not prompt or prompt.startswith(DECK_PREFIXES):
        return None
    if prompt.startswith(ANSWER_PREFIX):
        prompt = prompt[len(ANSWER_PREFIX):].lstrip(' -«').rstrip(' »')
    return _short(prompt) if prompt else None


def chat_hint(first_prompt: Optional[str]) -> str:
    """What the chat is about, from its first prompt. An AgentDeck brief opens
    "Context: Work for owner/repo#1. This chat is ..."; keep its first sentence."""
    if not first_prompt:
        return '(no prompt)'
    if first_prompt.startswith('Context:'):
        first_prompt = first_prompt[len('Context:'):].strip().split('. ', 1)[0]
    return _short(first_prompt)


def _short(text: str, limit: int = PROMPT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit - 1] + '…'


def read_chat(path: str) -> Optional[dict]:
    """One transcript as {session_id, cwd, first_prompt, messages}, where
    messages is a time-sorted list of (time, prompt or None)."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    cwd = None
    first_prompt = None
    messages: List[Tuple[datetime, Optional[str]]] = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                # Most lines are API bookkeeping; skip them before parsing.
                if '"type":"user"' not in line and '"type":"assistant"' not in line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get('type') not in MESSAGE_TYPES or data.get('isSidechain'):
                    continue
                timestamp = data.get('timestamp')
                if not timestamp:
                    continue
                cwd = cwd or data.get('cwd')
                prompt = prompt_text(data) if data['type'] == 'user' else None
                if (prompt and first_prompt is None and (
                        prompt.startswith('Context:')
                        or not prompt.startswith(DECK_PREFIXES))):
                    first_prompt = prompt
                try:
                    messages.append((parse_time(timestamp), prompt))
                except Exception:
                    continue
    except OSError:
        logger.warning('Could not read Claude transcript %s', path)
        return None
    if not messages:
        return None
    messages.sort(key=lambda item: item[0])
    return {
        'session_id': session_id,
        'cwd': cwd,
        'first_prompt': first_prompt,
        'messages': messages,
    }


def split_spans(
    messages: List[Tuple[datetime, Optional[str]]], idle: float,
) -> List[List[Tuple[datetime, Optional[str]]]]:
    spans: List[List[Tuple[datetime, Optional[str]]]] = []
    for item in messages:
        if spans and (item[0] - spans[-1][-1][0]).total_seconds() < idle:
            spans[-1].append(item)
        else:
            spans.append([item])
    return spans


def _entry_id(session_id: str, start: datetime) -> str:
    return '{}-{}'.format(session_id[:36], calendar.timegm(start.timetuple()))


class Parser(EntryMixin, AbstractParser):
    def get_entries(self) -> List[Entry]:
        idle = get_setting('IDLE', DEFAULTS['IDLE'])
        min_duration = get_setting('MIN_DURATION', DEFAULTS['MIN_DURATION'])
        max_prompts = get_setting('MAX_PROMPTS', DEFAULTS['MAX_PROMPTS'])
        include_ungrouped = get_setting('INCLUDE_UNGROUPED', DEFAULTS['INCLUDE_UNGROUPED'])
        pairs = group_dirs()
        entries: List[Entry] = []
        for chat in Provider().get_chats(self.start_date, self.end_date):
            messages = [
                item for item in chat['messages']
                if self.start_date <= item[0] <= self.end_date
            ]
            group = group_for_cwd(chat['cwd'], pairs)
            if group is None and not include_ungrouped:
                continue
            folder = os.path.basename((chat['cwd'] or '').rstrip('/')) or 'claude'
            hint = chat_hint(chat['first_prompt'])
            for span in split_spans(messages, idle):
                start = span[0][0]
                end = max(span[-1][0], start + timedelta(seconds=min_duration))
                prompts = [
                    listed for listed in (listed_prompt(prompt) for _time, prompt in span)
                    if listed
                ]
                lines = ['{}: {}'.format(folder, hint)]
                lines.extend('> ' + prompt for prompt in prompts[:max_prompts])
                if len(prompts) > max_prompts:
                    lines.append('... and {} more prompts'.format(len(prompts) - max_prompts))
                entries.append(Entry(
                    id=_entry_id(chat['session_id'], start),
                    start_time=start,
                    end_time=end,
                    group=group,
                    title='',
                    text='\n'.join(lines),
                    extra_data={'session_id': chat['session_id'], 'cwd': chat['cwd']},
                ))
        return entries


class Provider(AbstractProvider):
    def get_chats(self, start_date, end_date) -> List[dict]:
        root = os.path.expanduser(get_setting('DIR', DEFAULTS['DIR']))
        # A transcript last written before the range holds nothing in it.
        since = calendar.timegm(start_date.timetuple()) if start_date else 0
        chats = []
        for path in glob.glob(os.path.join(root, '*', '*.jsonl')):
            try:
                if os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
            chat = read_chat(path)
            if chat:
                chats.append(chat)
        return chats

    def test_get_chats(self, start_date, end_date) -> List[dict]:
        base = datetime(1970, 1, 15, 8, 0)
        return [{
            'session_id': '00000000-0000-0000-0000-000000000001',
            'cwd': 'path1',
            'first_prompt': 'Fix the order list',
            'messages': [
                (base, 'Fix the order list'),
                (base + timedelta(minutes=2), None),
                (base + timedelta(minutes=40), 'Now add a test'),
                (base + timedelta(minutes=41), None),
            ],
        }]
