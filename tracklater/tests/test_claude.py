import json
import os
from datetime import datetime, timedelta

import pytest

from tracklater.timemodules.claude import (
    DECK_PREFIXES,
    Message,
    Parser,
    Provider,
    build_blocks,
    chat_hint,
    listed_prompt,
    group_dirs,
    group_for_cwd,
    prompt_text,
    read_chat,
)


@pytest.fixture()
def parser():
    return Parser(datetime(1970, 1, 10), datetime(1970, 1, 20))


def test_get_entries_splits_on_idle_and_maps_group(parser):
    entries = parser.get_entries()
    assert len(entries) == 2
    first, second = sorted(entries, key=lambda e: e.start_time)
    assert first.group == 'group1'
    assert first.start_time == datetime(1970, 1, 15, 8, 0)
    assert first.end_time == datetime(1970, 1, 15, 8, 2)
    assert first.text == 'path1: Fix the order list\n> Fix the order list'
    assert second.text == 'path1: Fix the order list\n> Now add a test'
    assert first.id != second.id


T = datetime(1970, 1, 15, 9)


def _at(minutes, group='a', session='s'):
    return Message(T + timedelta(minutes=minutes), group, session, 'x: y', 'p')


def _spans(messages):
    return [
        ((b.start - T).total_seconds() / 60, (b.end - T).total_seconds() / 60, b.group)
        for b in build_blocks(messages, idle=300, tail=120, max_duration=1800)
    ]


def test_blocks_lone_message_and_gap():
    assert _spans([_at(0), _at(4.9), _at(9.9)]) == [(0, 6.9, 'a'), (9.9, 11.9, 'a')]


def test_blocks_join_chats_of_one_client():
    assert _spans([_at(0), _at(1, session='t'), _at(2)]) == [(0, 4, 'a')]


def test_blocks_of_two_clients_touch_but_never_overlap():
    assert _spans([_at(2, 'a'), _at(1, 'b'), _at(0, 'a')]) == [
        (0, 1, 'a'), (1, 2, 'b'), (2, 4, 'a')]


def test_long_run_is_cut_at_max_duration():
    spans = _spans([_at(m) for m in range(0, 74)])
    assert spans == [(0, 30, 'a'), (30, 60, 'a'), (60, 75, 'a')]
    # The tail does not stretch a block past the limit either.
    assert _spans([_at(m) for m in range(0, 30)]) == [(0, 30, 'a')]


def test_messages_in_one_second_make_one_block():
    assert _spans([_at(0, 'a'), _at(0, 'b', 't'), _at(0.01, 'a')]) == [(0, 2.01, 'a')]


def test_group_for_cwd_prefers_closest_dir(monkeypatch):
    monkeypatch.setattr('tracklater.settings.CLAUDE', {
        'outdoor': {'DIRS': ['/srv/outdoor']},
        'tilhi': {'DIRS': ['/srv/outdoor/tilhi']},
    }, raising=False)
    pairs = group_dirs()
    assert group_for_cwd('/srv/outdoor/tilhi/app', pairs) == 'tilhi'
    assert group_for_cwd('/srv/outdoor/sos/.worktrees/issue-1', pairs) == 'outdoor'
    assert group_for_cwd('/srv/outdoorsy', pairs) is None
    assert group_for_cwd(None, pairs) is None


def test_group_dirs_mix_claude_dirs_and_git_repos_per_group(monkeypatch):
    # group1 sets its own DIRS; group2 still maps through GIT REPOS.
    monkeypatch.setattr('tracklater.settings.CLAUDE',
                        {'group1': {'DIRS': ['/srv/one']}}, raising=False)
    pairs = group_dirs()
    assert group_for_cwd('/srv/one/x', pairs) == 'group1'
    assert group_for_cwd(os.path.realpath('path3'), pairs) == 'group2'
    # DIRS replaces group1's REPOS rather than adding to them.
    assert group_for_cwd(os.path.realpath('path1'), pairs) is None


def test_group_dirs_fall_back_to_git_repos():
    pairs = group_dirs()
    assert group_for_cwd(os.path.realpath('path3'), pairs) == 'group2'


def test_prompt_text_skips_tool_results_and_injected_context():
    assert prompt_text({'message': {'content': 'hello  there'}}) == 'hello there'
    assert prompt_text({'message': {'content': [
        {'type': 'text', 'text': '<system-reminder>x</system-reminder>'},
        {'type': 'text', 'text': 'do it'},
    ]}}) == 'do it'
    assert prompt_text({'message': {'content': [
        {'type': 'tool_result', 'content': 'ok'},
    ]}}) is None
    assert prompt_text({'isMeta': True, 'message': {'content': 'x'}}) is None


def test_read_chat(tmp_path):
    lines = [
        {'type': 'user', 'timestamp': '2026-01-01T08:58:00Z', 'cwd': '/srv/x',
         'message': {'content': 'Continue the work from this AgentDeck chat in the new session'}},
        {'type': 'queue-operation', 'timestamp': '2026-01-01T08:59:00Z'},
        {'type': 'user', 'timestamp': '2026-01-01T09:00:00Z', 'cwd': '/srv/x',
         'message': {'role': 'user', 'content': 'first ask'}},
        {'type': 'assistant', 'timestamp': '2026-01-01T09:01:00Z', 'cwd': '/srv/x',
         'message': {'role': 'assistant', 'content': []}},
        {'type': 'assistant', 'timestamp': '2026-01-01T09:02:00Z', 'isSidechain': True},
        {'type': 'user', 'timestamp': '2026-01-01T09:03:00Z',
         'message': {'content': [{'type': 'tool_result', 'content': 'x'}]}},
        {'type': 'user', 'timestamp': '2026-01-01T09:04:00Z', 'isCompactSummary': True,
         'message': {'content': 'a summary'}},
        {'type': 'attachment', 'timestamp': '2026-01-01T09:05:00Z', 'attachment': {
            'type': 'queued_command', 'commandMode': 'prompt',
            'prompt': [{'type': 'text', 'text': 'sent mid-turn'}]}},
        {'type': 'attachment', 'timestamp': '2026-01-01T09:05:30Z', 'attachment': {
            'type': 'queued_command', 'commandMode': 'task-notification', 'prompt': 'job done'}},
        {'type': 'user', 'timestamp': '2026-01-01T09:06:00Z',
         'message': {'content': [{'type': 'image', 'source': {}}]}},
        {'type': 'user', 'timestamp': '2026-01-01T09:07:00Z', 'isSidechain': True,
         'message': {'content': 'subagent task'}},
    ]
    path = tmp_path / 'abc.jsonl'
    path.write_text('\n'.join(json.dumps(line, separators=(',', ':')) for line in lines)
                    + '\nnot json\n')
    chat = read_chat(str(path))
    assert chat['session_id'] == 'abc'
    assert chat['cwd'] == '/srv/x'
    assert chat['first_prompt'] == 'first ask'
    assert chat['messages'] == [
        (datetime(2026, 1, 1, 8, 58),
         'Continue the work from this AgentDeck chat in the new session'),
        (datetime(2026, 1, 1, 9, 0), 'first ask'),
        (datetime(2026, 1, 1, 9, 5), 'sent mid-turn'),
        (datetime(2026, 1, 1, 9, 6), '(image)'),
    ]


def test_chats_outside_mapped_dirs_are_skipped_unless_asked(monkeypatch):
    chat = {
        'session_id': 'x', 'cwd': '/elsewhere', 'first_prompt': 'tooling',
        'messages': [(datetime(1970, 1, 15, 9), 'tooling')],
    }
    monkeypatch.setattr(Provider, 'test_get_chats', lambda self, s, e: [chat])
    assert Parser(datetime(1970, 1, 10), datetime(1970, 1, 20)).get_entries() == []
    monkeypatch.setattr('tracklater.settings.CLAUDE',
                        {'global': {'INCLUDE_UNGROUPED': True}}, raising=False)
    entries = Parser(datetime(1970, 1, 10), datetime(1970, 1, 20)).get_entries()
    assert [e.group for e in entries] == [None]


def test_chat_hint_shortens_agentdeck_brief():
    assert chat_hint('Context: Work for owner/repo#7. This chat is the Work.') == \
        'Work for owner/repo#7'
    assert chat_hint(None) == '(no prompt)'
    assert chat_hint('x' * 200).endswith('…')


def test_deck_messages_are_not_listed_as_prompts(monkeypatch):
    t = datetime(1970, 1, 15, 9)
    chat = {
        'session_id': 'x', 'cwd': 'path1',
        'first_prompt': 'Context: Work for o/r#1. This chat is the Work.',
        'messages': [
            (t, 'Context: Work for o/r#1. This chat is the Work.'),
            (t + timedelta(minutes=1), 'AgentDeck wake-on: commit status finished'),
            (t + timedelta(minutes=2), 'Make the button blue'),
        ],
    }
    monkeypatch.setattr(Provider, 'test_get_chats', lambda self, s, e: [chat])
    entries = Parser(datetime(1970, 1, 10), datetime(1970, 1, 20)).get_entries()
    assert entries[0].text == 'path1: Work for o/r#1\n> Make the button blue'


def test_listed_prompt_unwraps_question_box_answers():
    assert listed_prompt('The person answered from the question box: - «Yes, ship it»') == \
        'Yes, ship it'
    assert listed_prompt('AgentDeck event: review passed') is None
    assert listed_prompt(None) is None


def test_listed_prompt_keeps_the_persons_words_inside_deck_wrappers():
    notes = ('Deck notes that came while you were not woken (#1), oldest first. '
             '1. AgentDeck: x. --- The message itself follows. --- ')
    assert listed_prompt(notes + 'Ajatko sen?') == 'Ajatko sen?'
    assert listed_prompt(notes + 'AgentDeck wake-on: merged') is None
    assert listed_prompt('(From the deck: read them with `chat-read x --after 1`.) Miksi?') == \
        'Miksi?'
    assert listed_prompt('Start new work from here: file it. The work, in my words: kieli '
                         '- Write the issue from this conversation.') == 'kieli'


@pytest.mark.parametrize('prompt', [
    'Deck notes that came while you were not woken: 1. AgentDeck: merged.',
    '(From the deck: 2 new messages.)',
    'Start new work from here: file it.',
])
def test_wrapper_without_the_persons_words_is_skipped(prompt):
    assert listed_prompt(prompt) is None


@pytest.mark.parametrize('prompt', [
    'The boss wants a report', 'Continue the work from the old branch',
    'I approve the spec but change the colour',
])
def test_own_words_that_look_like_a_deck_message_are_kept(prompt):
    assert listed_prompt(prompt) == prompt


def _chat(session_id, cwd, times):
    return {'session_id': session_id, 'cwd': cwd, 'first_prompt': 'p',
            'messages': [(t, 'p') for t in times]}


def _entries(monkeypatch, chats, start, end):
    monkeypatch.setattr(Provider, 'test_get_chats', lambda self, s, e: chats)
    return Parser(start, end).get_entries()


def test_agent_only_chat_gives_no_entry(monkeypatch):
    chat = {'session_id': 'x', 'cwd': 'path1', 'first_prompt': None, 'messages': [
        (T, 'Context: Work for o/r#1. This chat is the Work.'),
        (T + timedelta(minutes=1), 'Routine notice: do what it asks'),
    ]}
    assert _entries(monkeypatch, [chat], T - timedelta(days=1), T + timedelta(days=1)) == []


@pytest.mark.parametrize('prefix', DECK_PREFIXES)
def test_every_deck_prefix_is_skipped(prefix):
    assert listed_prompt(prefix + ' something') is None


def test_text_lists_each_chat_and_caps_prompts(monkeypatch):
    chats = [
        _chat('a', 'path1', [T + timedelta(seconds=s) for s in range(4)]),
        _chat('b', 'path1', [T + timedelta(minutes=1, seconds=s) for s in range(3)]),
    ]
    entry, = _entries(monkeypatch, chats, T - timedelta(days=1), T + timedelta(days=1))
    assert entry.text == (
        'path1: p\n> p\n> p\n> p\n> p\npath1: p\n> p\n... and 2 more prompts')
    assert entry.extra_data == {'session_ids': ['a', 'b']}


def test_message_copied_to_a_branched_chat_counts_once(monkeypatch):
    chats = [_chat('a', 'path1', [T]), _chat('b', 'path1', [T])]
    entry, = _entries(monkeypatch, chats, T - timedelta(days=1), T + timedelta(days=1))
    assert entry.text == 'path1: p\n> p'


def test_range_cut_inside_a_block_gives_the_same_blocks(monkeypatch):
    chats = [_chat('a', 'path1', [T, T + timedelta(minutes=4), T + timedelta(minutes=8)])]
    a, b, c = T - timedelta(days=2), T + timedelta(minutes=6), T + timedelta(days=2)

    def spans(start, end):
        return [(e.id, e.start_time, e.end_time)
                for e in _entries(monkeypatch, chats, start, end)]
    assert len(spans(a, c)) == 1
    assert spans(a, b) + spans(b, c) == spans(a, c)


def test_refetch_gives_same_ids():
    def ids():
        return sorted(e.id for e in Parser(
            datetime(1970, 1, 10), datetime(1970, 1, 20)).get_entries())
    assert ids() == ids()


def test_read_chat_sorts_lines_written_out_of_order(tmp_path):
    lines = [
        {'type': 'user', 'timestamp': '2026-01-01T09:05:00Z', 'message': {'content': 'b'}},
        {'type': 'user', 'timestamp': '2026-01-01T09:00:00Z', 'message': {'content': 'a'}},
    ]
    path = tmp_path / 's.jsonl'
    path.write_text('\n'.join(json.dumps(line, separators=(',', ':')) for line in lines))
    times = [time for time, _prompt in read_chat(str(path))['messages']]
    assert len(times) == 2
    assert times == sorted(times)


def test_provider_skips_files_not_written_in_range(tmp_path, monkeypatch):
    monkeypatch.setattr('tracklater.settings.TESTING', False)
    monkeypatch.setattr('tracklater.settings.CLAUDE',
                        {'global': {'DIR': str(tmp_path)}}, raising=False)
    line = json.dumps({'type': 'user', 'timestamp': '2026-01-01T09:00:00Z', 'cwd': '/x',
                       'message': {'content': 'hi'}}, separators=(',', ':'))
    for name, mtime in (('old', 1_000_000), ('new', 2_000_000_000)):
        (tmp_path / 'proj').mkdir(exist_ok=True)
        path = tmp_path / 'proj' / '{}.jsonl'.format(name)
        path.write_text(line)
        os.utime(path, (mtime, mtime))
    chats = Provider().get_chats(datetime(2020, 1, 1), datetime(2030, 1, 1))
    assert [chat['session_id'] for chat in chats] == ['new']


def test_provider_reads_every_account_folder(tmp_path, monkeypatch):
    monkeypatch.setattr('tracklater.settings.TESTING', False)
    # No DIR setting at all: the default must find both accounts under $HOME.
    monkeypatch.setattr('tracklater.settings.CLAUDE', {}, raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    line = json.dumps({'type': 'user', 'timestamp': '2026-01-01T09:00:00Z', 'cwd': '/x',
                       'message': {'content': 'hi'}}, separators=(',', ':'))
    for account, session in (('.claude', 'main'), ('.claude2', 'alt')):
        folder = tmp_path / account / 'projects' / 'proj'
        folder.mkdir(parents=True)
        (folder / '{}.jsonl'.format(session)).write_text(line)
    chats = Provider().get_chats(datetime(2020, 1, 1), datetime(2030, 1, 1))
    assert sorted(chat['session_id'] for chat in chats) == ['alt', 'main']
    monkeypatch.setattr('tracklater.settings.CLAUDE',
                        {'global': {'DIR': [str(tmp_path / '.claude2' / 'projects')]}},
                        raising=False)
    chats = Provider().get_chats(datetime(2020, 1, 1), datetime(2030, 1, 1))
    assert [chat['session_id'] for chat in chats] == ['alt']
