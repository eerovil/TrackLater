#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from time import sleep

import importlib
from typing import Dict
from types import ModuleType

from tracklater import settings
from tracklater.timemodules.interfaces import AbstractParser
from tracklater.models import ApiCall, Entry, Issue, Project
from tracklater.database import db

import logging
logger = logging.getLogger(__name__)


def store_parser_to_database(parser, module_name, start_date, end_date):
    # Drafts are a toggl-only concept (see tracklater.billing); a fetch refreshes
    # synced rows but must not clobber toggl drafts that were never pushed. Every
    # other module's rows have is_draft False, so they refresh normally.
    Entry.query.filter(
        Entry.module == module_name, Entry.start_time >= start_date,
        Entry.start_time <= end_date, Entry.is_draft == False  # noqa: E712
    ).delete()
    for entry in parser.entries:
        entry.module = module_name
        db.session.merge(entry)
    for issue in parser.issues:
        issue.module = module_name
        db.session.merge(issue)

    # Delete old projects
    Project.query.filter(
        Project.module == module_name
    ).delete()

    for project in parser.projects:
        project.module = module_name
        db.session.merge(project)
    db.session.add(ApiCall(
        start_date=start_date,
        end_date=end_date,
        module=module_name
    ))
    db.session.commit()


def set_parser_caching_data(parser, module_name):
    apicall = ApiCall.query.filter_by(module=module_name).order_by('created').first()
    if apicall:
        parser.set_database_values(
            start_date=apicall.start_date,
            end_date=apicall.end_date,
            issue_count=Issue.query.filter_by(module=module_name).count(),
            entry_count=Entry.query.filter_by(module=module_name).count(),
            project_count=Project.query.filter_by(module=module_name).count(),
        )


class Parser(object):
    def __init__(self, start_date, end_date, modules=None) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.modules: Dict[str, AbstractParser] = {}

        for module_name in settings.ENABLED_MODULES:
            if modules and module_name not in modules:
                continue
            module: ModuleType = importlib.import_module(
                'tracklater.timemodules.{}'.format(module_name)
            )
            if getattr(module, 'Parser', None) is None:
                logger.warning('Module %s has no Parser class', module_name)
            try:
                parser = module.Parser(self.start_date, self.end_date)  # type: ignore
                self.modules[module_name] = parser
            except Exception as e:
                logger.exception("Error initializing parser for module %s", module_name)
                continue

    def parse(self) -> None:
        parsers = []
        group_to_project = {project.group: project.pid for project in Project.query.all()}
        project_to_group = {project.pid: project.group for project in Project.query.all()}
        for module_name, parser in self.modules.items():
            set_parser_caching_data(parser, module_name)
            logger.warning("Parsing %s", module_name)
            try:
                parser.parse()
            except Exception as e:
                logger.exception("Error parsing module %s", module_name)
                continue
            parsers.append((module_name, parser))
            module_g2p = {project.group: project.pid for project in parser.projects}
            module_p2g = {str(project.pid): project.group for project in parser.projects}
            for entry in parser.entries:
                if not entry.project and entry.group:
                    entry.project = module_g2p.get(
                        entry.group, group_to_project.get(entry.group, None)
                    )
                if not entry.group and entry.project:
                    entry.group = module_p2g.get(
                        str(entry.project), project_to_group.get(str(entry.project), None)
                    )
            index = 0
            while True:
                index += 1
                if index > 10:
                    logger.error("Task failed %s", module_name)
                    break
                try:
                    store_parser_to_database(self.modules[module_name], module_name,
                                            start_date=self.start_date, end_date=self.end_date)
                    logger.warning("Task done %s", module_name)
                    break
                except Exception as e:
                    logger.exception(e)
                    db.session.rollback()
                    logger.warning("Task retry %s", module_name)
                    sleep(1)

