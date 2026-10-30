import json
import os
from datetime import datetime, timedelta

import pytest

from tracklater.timemodules.claude import (
    Parser,
    Provider,
    chat_hint,
    listed_prompt,
    group_dirs,
    group_for_cwd,
    prompt_text,
    read_chat,
    split_spans,
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


def test_lone_message_gets_min_duration(monkeypatch):
    monkeypatch.setattr('tracklater.settings.CLAUDE', {'global': {'IDLE': 60}}, raising=False)
    entries = Parser(datetime(1970, 1, 10), datetime(1970, 1, 20)).get_entries()
    # 08:00 and 08:02 are now two spans; the lone 08:00 one still lasts a minute.
    first = sorted(entries, key=lambda e: e.start_time)[0]
    assert first.end_time - first.start_time == timedelta(seconds=60)


def test_split_spans():
    t = datetime(2026, 1, 1, 9)
    spans = split_spans([(t, 'a'), (t + timedelta(minutes=5), None),
                         (t + timedelta(minutes=30), 'b')], idle=900)
    assert [len(span) for span in spans] == [2, 1]


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
        (datetime(2026, 1, 1, 9, 1), None),
        (datetime(2026, 1, 1, 9, 3), None),
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


def _chat(session_id, cwd, times):
    return {'session_id': session_id, 'cwd': cwd, 'first_prompt': 'p',
            'messages': [(t, 'p') for t in times]}


def test_two_chats_at_once_and_range_cut(monkeypatch):
    t = datetime(1970, 1, 15, 9)
    chats = [
        _chat('a', 'path1', [t, t + timedelta(minutes=5)]),
        _chat('b', 'path3', [t + timedelta(minutes=1), t + timedelta(minutes=3)]),
        # Only the part inside the range is kept.
        _chat('c', 'path1', [datetime(1970, 1, 9, 23, 59), datetime(1970, 1, 10, 0, 5)]),
    ]
    monkeypatch.setattr(Provider, 'test_get_chats', lambda self, s, e: chats)
    entries = Parser(datetime(1970, 1, 10), datetime(1970, 1, 20)).get_entries()
    by_chat = {e.extra_data['session_id']: e for e in entries}
    assert len(entries) == 3
    assert (by_chat['a'].group, by_chat['b'].group) == ('group1', 'group2')
    assert by_chat['c'].start_time == datetime(1970, 1, 10, 0, 5)


def test_refetch_gives_same_ids():
    def ids():
        return sorted(e.id for e in Parser(
            datetime(1970, 1, 10), datetime(1970, 1, 20)).get_entries())
    assert ids() == ids()


def test_read_chat_sorts_lines_written_out_of_order(tmp_path):
    lines = [
        {'type': 'assistant', 'timestamp': '2026-01-01T09:05:00Z'},
        {'type': 'user', 'timestamp': '2026-01-01T09:00:00Z', 'message': {'content': 'a'}},
    ]
    path = tmp_path / 's.jsonl'
    path.write_text('\n'.join(json.dumps(line, separators=(',', ':')) for line in lines))
    times = [time for time, _prompt in read_chat(str(path))['messages']]
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
