from flask import request, Blueprint, Response, stream_with_context
from tracklater.utils import _str
from datetime import datetime, timedelta, date
import json
import pytz
from typing import Dict, Any, List, Optional
from time import sleep

from tracklater.database import db
from tracklater.main import Parser
from tracklater import settings
from tracklater.models import Entry, Issue, Project, ApiCall  # noqa
from tracklater.timemodules.interfaces import AddEntryMixin, UpdateEntryMixin, ProjectMixin
from tracklater.ai_local import populate_local_entries
from tracklater.billing import BILLING_MODULE

import logging
logger = logging.getLogger(__name__)


bp = Blueprint("main", __name__, static_folder="static", static_url_path="/static/views")


@bp.route('/', methods=['GET'])
def index():
    return bp.send_static_file('index.html')


def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, (datetime, date)):
        if obj.tzinfo and obj.tzinfo is not pytz.utc:
            logger.warning("Trying to serialize timezone aware date %s", obj)
        obj.replace(tzinfo=None)
        # No idea, but for some reason using isoformat() here did *not* work.
        # Just add utc timezone manually then... (This will make js convert it automatically)
        return obj.isoformat() + "+00:00"
    raise TypeError("Type %s not serializable" % type(obj))


class MyEncoder(json.JSONEncoder):
    def default(self, o):
        return {k.lstrip('_'): v for k, v in vars(o).items()}


@bp.route('/listmodules', methods=['GET'])
def listmodules() -> Any:
    if request.method == 'GET':
        data = {}
        parser = Parser(None, None)
        for module_name in settings.ENABLED_MODULES:
            data[module_name] = {
                'color': settings.UI_SETTINGS.get(module_name, {}),
                'capabilities': parser.modules[module_name].capabilities,
            }
        return json.dumps(data, default=json_serial)
    return None


@bp.route('/getsettings', methods=['GET'])
def getsettings() -> Any:
    return json.dumps(settings, cls=MyEncoder)


@bp.route('/fetchdata', methods=['GET'])
def fetchdata() -> Any:
    if request.method == 'GET':
        keys: Optional[List[str]] = request.values.getlist('keys[]')
        parse = request.values.get('parse', '1')
        now = datetime.utcnow()
        if 'from' in request.values:
            from_date = parseTimestamp(request.values['from'])
        else:
            from_date = now - timedelta(days=41)
        if 'to' in request.values:
            to_date = parseTimestamp(request.values['to'])
        else:
            to_date = now

        if getattr(settings, 'OVERRIDE_START', None):
            from_date = settings.OVERRIDE_START
        if getattr(settings, 'OVERRIDE_END', None):
            to_date = settings.OVERRIDE_END

        if keys and keys[0] == "all":
            keys = None
        parser = Parser(from_date, to_date, modules=keys)
        if parse == '1':
            parser.parse()
        data: Dict[str, Dict] = {}
        for key in settings.ENABLED_MODULES:
            if not keys or key in keys:
                data[key] = {}
                billing_parser = parser.modules.get(key)
                if key == BILLING_MODULE and isinstance(billing_parser, ProjectMixin):
                    # Synthetic group:name projects need no API call; ensure they
                    # exist in the DB so the project dropdown works without a parse.
                    for project in billing_parser.get_projects():
                        project.module = key
                        db.session.merge(project)
                    db.session.commit()
                data[key]['entries'] = [entry.to_dict()
                                        for entry in Entry.query.filter(
                                            Entry.module == key,
                                            Entry.start_time >= from_date,
                                            Entry.start_time <= to_date
                                        )]
                data[key]['projects'] = [project.to_dict()
                                         for project in Project.query.filter(
                                            Project.module == key
                                        )]
                data[key]['issues'] = [issue.to_dict()
                                       for issue in Issue.query.filter(
                                            Issue.module == key
                                        )]
                data[key]['capabilities'] = parser.modules[key].capabilities
                data[key]['color'] = settings.UI_SETTINGS.get(key, {})
        return json.dumps(data, default=json_serial)
    return None


def parseTimestamp(stamp):
    if not stamp:
        return None
    date = datetime.fromtimestamp(int(stamp) / 1e3)
    return date


def parseUtcTimestamp(stamp):
    """Epoch milliseconds as a naive UTC datetime, independent of server TZ."""
    if not stamp:
        return None
    return datetime.utcfromtimestamp(int(stamp) / 1e3)


@bp.route('/updateentry', methods=['POST'])
def updateentry() -> Any:
    if request.method == 'POST':
        data = request.get_json()
        module = data.get('module')
        entry_id = _str(data.get('entry_id', None))
        project = data.get('project_id', None)
        # The frontend sends "0"/"null"/"" to mean "no project"; normalise so an
        # unassigned entry reaches the module as project=None.
        if project in ("null", "0", "", None):
            project = None
        project_to_group = {project.pid: project.group for project in Project.query.all()}
        # Entries are naive UTC; the frontend sends epoch ms.
        new_entry = Entry(
            start_time=parseUtcTimestamp(data['start_time']),
            end_time=parseUtcTimestamp(data.get('end_time', None)),
            id=entry_id,
            issue=data.get('issue_id', None),
            project=project,
            title=data.get('title', ''),
            text=data.get('text', ""),
            extra_data=data.get('extra_data', {}),
            group=project_to_group.get(str(project), None)
        )
        issue = None
        if new_entry.issue:
            issue = Issue.query.filter(Issue.uuid == new_entry.issue).first()

        parser = Parser(None, None)

        try:
            if not entry_id:
                # Check that create is allowed
                assert isinstance(parser.modules[module], AddEntryMixin)
                new_entry = parser.modules[module].create_entry(  # type: ignore
                    new_entry=new_entry,
                    issue=issue
                )
            else:
                # Check that update is allowed
                assert isinstance(parser.modules[module], UpdateEntryMixin)
                new_entry = parser.modules[module].update_entry(  # type: ignore
                    entry_id=new_entry.id,
                    new_entry=new_entry,
                    issue=issue
                )
        except ValueError as e:
            # e.g. Kimai refuses an entry with no project: say so instead of a 500.
            return json.dumps({"error": str(e)}), 400
        data = "error"

        while True:
            try:
                if new_entry:
                    # Scope by module: Entry's PK is (module, id, start_time), so
                    # filtering on id alone could delete a same-id row in another module.
                    Entry.query.filter(
                        Entry.module == module, Entry.id == new_entry.id
                    ).delete()
                    new_entry.module = module
                    db.session.merge(new_entry)
                    db.session.commit()
                    data = new_entry.to_dict()
                break
            except Exception as e:
                logger.exception("Error updating entry")
                db.session.rollback()
                sleep(1)

        return json.dumps(data, default=json_serial)
    return None


@bp.route('/populatelocal', methods=['POST'])
def populatelocal() -> Any:
    if request.method != 'POST':
        return None
    data = request.get_json() or {}
    from_date = parseTimestamp(data.get('from'))
    to_date = parseTimestamp(data.get('to'))
    if not from_date or not to_date:
        return json.dumps(
            {"error": "from and to timestamps (ms) are required"},
            default=json_serial,
        ), 400
    replace_existing = data.get('replace_existing', True)
    engine = (data.get('engine') or 'rules').lower()
    try:
        if engine == 'codex':
            from tracklater.ai_local_codex import populate_local_entries_ai
            created = populate_local_entries_ai(
                from_date, to_date, replace_existing=replace_existing
            )
        elif engine == 'rules':
            created = populate_local_entries(
                from_date, to_date, replace_existing=replace_existing
            )
        else:
            raise ValueError("Unsupported populate engine: {}".format(engine))
        return json.dumps({
            "entries": [e.to_dict() for e in created],
            "count": len(created),
        }, default=json_serial)
    except ValueError as e:
        return json.dumps({"error": str(e)}, default=json_serial), 400
    except Exception as e:
        logger.exception("populate local failed")
        return json.dumps({"error": str(e)}, default=json_serial), 500


@bp.route('/populatelocalstream', methods=['POST'])
def populatelocalstream() -> Any:
    """Streaming week/day fill, emitting one ndjson line per completed day.

    A request with ``single_day: true`` must use exact local-midnight bounds and
    is pinned to that calendar date. Closing the stream keeps completed days.
    """
    data = request.get_json() or {}
    single_day = data.get('single_day') is True
    timestamp_parser = parseUtcTimestamp if single_day else parseTimestamp
    from_date = timestamp_parser(data.get('from'))
    to_date = timestamp_parser(data.get('to'))
    replace_existing = data.get('replace_existing', True)
    skip_filled_days = data.get('skip_filled_days') is True
    if not from_date or not to_date:
        return json.dumps({"error": "from and to timestamps (ms) are required"}), 400

    from tracklater.ai_local_codex import (
        stream_populate_local_entries_ai,
        validate_single_local_day_range,
    )

    only_day = None
    if single_day:
        try:
            only_day = validate_single_local_day_range(from_date, to_date)
        except ValueError as e:
            return json.dumps({"error": str(e)}), 400

    def generate():
        try:
            for event in stream_populate_local_entries_ai(
                from_date, to_date, replace_existing=replace_existing,
                only_day=only_day, skip_filled_days=skip_filled_days,
            ):
                yield json.dumps(event, default=json_serial) + "\n"
        except GeneratorExit:
            # Client disconnected (cancel). The in-flight day already persisted;
            # just stop. Re-raise so Flask tears the generator down cleanly.
            logger.info("populatelocalstream cancelled by client")
            raise
        except Exception as e:  # noqa
            logger.exception("populatelocalstream failed")
            yield json.dumps({"type": "error", "error": str(e)}) + "\n"

    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')


@bp.route('/suggestions', methods=['GET'])
def suggestions() -> Any:
    """Codex-precomputed project/title hints in a window, for the editor dropdown."""
    from tracklater.models import EntrySuggestion
    from_date = parseTimestamp(request.args.get('from'))
    to_date = parseTimestamp(request.args.get('to'))
    q = EntrySuggestion.query
    if from_date and to_date:
        q = q.filter(
            EntrySuggestion.start_time >= from_date,
            EntrySuggestion.start_time <= to_date,
        )
    rows = q.order_by(EntrySuggestion.start_time).all()
    return json.dumps([r.to_dict() for r in rows], default=json_serial)


@bp.route('/populateentry', methods=['POST'])
def populateentry() -> Any:
    """Create a single local entry grown from a double-click at `click` (ms),
    bounded by the neighbouring entries `prev_end`/`next_start` (ms, optional)."""
    data = request.get_json() or {}
    click = parseUtcTimestamp(data.get('click'))
    if not click:
        return json.dumps({"error": "click timestamp (ms) is required"}), 400
    prev_end = parseUtcTimestamp(data.get('prev_end')) if data.get('prev_end') else None
    next_start = parseUtcTimestamp(data.get('next_start')) if data.get('next_start') else None
    try:
        from tracklater.ai_local_codex import populate_entry_at
        created = populate_entry_at(click, prev_end=prev_end, next_start=next_start)
        return json.dumps({
            "entries": [e.to_dict() for e in created],
            "count": len(created),
        }, default=json_serial)
    except ValueError as e:
        return json.dumps({"error": str(e)}, default=json_serial), 400
    except Exception as e:
        logger.exception("populate entry failed")
        return json.dumps({"error": str(e)}, default=json_serial), 500


@bp.route('/deleteentry', methods=['POST'])
def deleteentry() -> Any:
    if request.method == 'POST':
        data = request.get_json()
        module = data.get('module')
        entry_id = data.get('entry_id')

        parser = Parser(None, None)
        # Check that delete is allowed
        assert isinstance(parser.modules[module], AddEntryMixin)
        ret = parser.modules[module].delete_entry(  # type: ignore
            entry_id=entry_id
        )

        # Scope by module: id alone is not the full primary key.
        Entry.query.filter(Entry.module == module, Entry.id == entry_id).delete()
        db.session.commit()

        return json.dumps(ret, default=json_serial)
    return None
