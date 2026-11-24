"""Claude Code chats as timeline hints.

Reads the JSONL transcripts Claude Code writes under ~/.claude/projects and
turns the messages the person wrote, all chats in one time order, into short
blocks on a `claude` row that never overlap; how long an agent worked does not
matter. The chat's working directory picks the group, the same way
GIT.<group>.REPOS does. Hints only: nothing here bills, and transcript
text never leaves this machine (it is only stored in the local database).
"""
import calendar
import glob
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from tracklater import settings
from tracklater.utils import parse_time
from tracklater.timemodules.interfaces import EntryMixin, AbstractParser, AbstractProvider
from tracklater.models import Entry

import logging
logger = logging.getLogger(__name__)


DEFAULTS = {
    # One folder or a list; each may be a glob. AgentDeck runs a second Claude
    # account from ~/.claude2, so the default reads every ~/.claude*/projects.
    'DIR': '~/.claude*/projects',
    # A gap this long (seconds) between messages starts a new block.
    'IDLE': 300,
    # A block lasts this long after its last message, so a lone one is visible.
    'TAIL': 120,
    'MAX_DURATION': 1800,
    'MAX_PROMPTS': 5,
    # Chats outside every mapped directory (tooling, side projects) are noise
    # for billing; set True to show them ungrouped.
    'INCLUDE_UNGROUPED': False,
}
PROMPT_CHARS = 120
# Blocks are built from messages this much outside the asked range too, so a
# block starts at the same time however the range is cut.
MARGIN = timedelta(days=1)
# Messages AgentDeck (or Claude Code) puts in a chat on its own (briefs, wake-ons,
# review events, job results): never the person's. Only a brief can be the hint.
DECK_PREFIXES = (
    'Context:', 'AgentDeck',
    'Your queued job', 'You are AgentDeck', 'Routine notice:',
    'This session is being continued', 'Your Mac command', 'I approve the spec',
    'The previous account', 'Your previous turn failed',
    'The AgentDeck runtime',
    'You are a side chat', '[Request interrupted',
    'Continue the work from', 'Write a concise self-contained handoff', 'The boss ',
    'Without calling any tool', 'Base directory for this skill',
)
# AgentDeck's wrappers around the person's own words (group 1), which are kept.
WRAPPERS = (
    re.compile(r'Deck notes that came .*--- The message itself follows\. ---\s*(.*)'),
    re.compile(r'\(From the deck: .*?`\.\)\s*(.*)'),
    re.compile(r'Start new work from here: .*?The work, in my words: (.*?)'
               r'(?: - Write the issue from this .*)?$'),
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

    Each group uses its CLAUDE.<group>.DIRS when set, else its GIT.<group>.REPOS,
    so a setup that already maps repositories needs no new settings and setting
    DIRS for one group leaves the others' repositories in place."""
    claude = getattr(settings, 'CLAUDE', {}) or {}
    git = getattr(settings, 'GIT', {}) or {}
    pairs: List[Tuple[str, str]] = []
    for group in dict.fromkeys(list(claude) + list(git)):
        if group == 'global':
            continue
        paths = (claude.get(group) or {}).get('DIRS')
        if paths is None:
            paths = (git.get(group) or {}).get('REPOS', [])
        pairs.extend((_real(path), group) for path in paths)
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
    if message.get('isMeta') or message.get('isCompactSummary'):
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
    if not text and isinstance(content, list) and any(
            isinstance(block, dict) and block.get('type') == 'image' for block in content):
        return '(image)'
    return text or None


def line_prompt(data: dict) -> Optional[str]:
    """The prompt in one transcript line: a user message, or a message sent
    while Claude was working, which is stored as a queued_command attachment."""
    if data.get('isSidechain'):
        return None
    if data.get('type') == 'user':
        return prompt_text(data)
    attachment = data.get('attachment')
    if (data.get('type') == 'attachment' and isinstance(attachment, dict)
            and attachment.get('type') == 'queued_command'
            and attachment.get('commandMode') == 'prompt'):
        return prompt_text({
            'isMeta': data.get('isMeta') or attachment.get('isMeta'),
            'message': {'content': attachment.get('prompt')},
        })
    return None


def unwrap(prompt: str) -> str:
    for pattern in WRAPPERS:
        match = pattern.match(prompt)
        if match:
            prompt = match.group(1)
    return prompt


def listed_prompt(prompt: Optional[str]) -> Optional[str]:
    """A prompt as listed in an entry, or None for AgentDeck's own messages."""
    if not prompt:
        return None
    prompt = unwrap(prompt)
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
    messages is a time-sorted list of (time, prompt)."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    cwd = None
    first_prompt = None
    messages: List[Tuple[datetime, str]] = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                # Most lines are API bookkeeping; skip them before parsing.
                if '"type":"user"' not in line and '"queued_command"' not in line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if not data.get('isSidechain'):
                    cwd = cwd or data.get('cwd')
                prompt = line_prompt(data)
                timestamp = data.get('timestamp')
                if not prompt or not timestamp:
                    continue
                words = unwrap(prompt)
                if (words and first_prompt is None and (
                        words.startswith('Context:')
                        or not words.startswith(DECK_PREFIXES))):
                    first_prompt = words
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


class Message(NamedTuple):
    time: datetime
    group: Optional[str]
    session_id: str
    label: str  # "folder: what the chat is about"
    prompt: str


class Block(NamedTuple):
    start: datetime
    end: datetime
    group: Optional[str]
    messages: List[Message]


def build_blocks(
    messages: List[Message], idle: float, tail: float, max_duration: float,
) -> List[Block]:
    """Blocks of messages, in time order and never overlapping."""
    limit = timedelta(seconds=max_duration)
    runs: List[List[Message]] = []
    for message in sorted(messages, key=lambda m: (m.time, m.session_id)):
        if runs:
            first, last = runs[-1][0], runs[-1][-1]
            # Two blocks in one second would leave the first one empty.
            same_second = message.time.replace(microsecond=0) == first.time.replace(microsecond=0)
            if same_second or (
                    message.group == first.group
                    and message.time - last.time < timedelta(seconds=idle)
                    and message.time - first.time < limit):
                runs[-1].append(message)
                continue
        runs.append([message])
    blocks = []
    for index, run in enumerate(runs):
        start = run[0].time
        end = min(run[-1].time + timedelta(seconds=tail), start + limit)
        if index + 1 < len(runs):
            end = min(end, runs[index + 1][0].time)
        blocks.append(Block(start, end, run[0].group, run))
    return blocks


def block_text(block: Block, max_prompts: int) -> str:
    """Each chat in the block, followed by what was written in it."""
    chats: Dict[str, List[Message]] = {}
    for message in block.messages:
        chats.setdefault(message.session_id, []).append(message)
    lines = []
    left = max_prompts
    for messages in chats.values():
        lines.append(messages[0].label)
        lines.extend('> ' + message.prompt for message in messages[:left])
        left -= min(left, len(messages))
    hidden = len(block.messages) - max_prompts
    if hidden > 0:
        lines.append('... and {} more prompts'.format(hidden))
    return '\n'.join(lines)


def _entry_id(block: Block) -> str:
    return 'claude-{}-{}'.format(block.group, calendar.timegm(block.start.timetuple()))


class Parser(EntryMixin, AbstractParser):
    def get_messages(self) -> List[Message]:
        include_ungrouped = get_setting('INCLUDE_UNGROUPED', DEFAULTS['INCLUDE_UNGROUPED'])
        pairs = group_dirs()
        since, until = self.start_date - MARGIN, self.end_date + MARGIN
        messages: List[Message] = []
        # A branched or resumed chat copies the earlier lines into a new file.
        seen: Set[Tuple[datetime, str]] = set()
        chats = Provider().get_chats(since, until)
        for chat in sorted(chats, key=lambda chat: chat['session_id']):
            group = group_for_cwd(chat['cwd'], pairs)
            if group is None and not include_ungrouped:
                continue
            folder = os.path.basename((chat['cwd'] or '').rstrip('/')) or 'claude'
            label = '{}: {}'.format(folder, chat_hint(chat['first_prompt']))
            for time, prompt in chat['messages']:
                listed = listed_prompt(prompt)
                if not listed or not since <= time <= until or (time, prompt) in seen:
                    continue
                seen.add((time, prompt))
                messages.append(Message(time, group, chat['session_id'], label, listed))
        return messages

    def get_entries(self) -> List[Entry]:
        max_prompts = get_setting('MAX_PROMPTS', DEFAULTS['MAX_PROMPTS'])
        blocks = build_blocks(
            self.get_messages(),
            idle=get_setting('IDLE', DEFAULTS['IDLE']),
            tail=get_setting('TAIL', DEFAULTS['TAIL']),
            max_duration=get_setting('MAX_DURATION', DEFAULTS['MAX_DURATION']),
        )
        return [
            Entry(
                id=_entry_id(block),
                start_time=block.start,
                end_time=block.end,
                group=block.group,
                title='',
                text=block_text(block, max_prompts),
                extra_data={
                    'session_ids': sorted({m.session_id for m in block.messages}),
                },
            )
            for block in blocks if self.start_date <= block.start <= self.end_date
        ]


class Provider(AbstractProvider):
    def get_chats(self, start_date, end_date) -> List[dict]:
        setting = get_setting('DIR', DEFAULTS['DIR'])
        patterns = [setting] if isinstance(setting, str) else list(setting)
        roots = sorted({
            root for pattern in patterns
            for root in glob.glob(os.path.expanduser(pattern))
        })
        # A transcript last written before the range holds nothing in it.
        since = calendar.timegm(start_date.timetuple()) if start_date else 0
        chats = []
        paths = [
            path for root in roots
            for path in glob.glob(os.path.join(root, '*', '*.jsonl'))
        ]
        for path in paths:
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
                (base + timedelta(minutes=40), 'Now add a test'),
            ],
        }]
