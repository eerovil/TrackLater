from typing import List, Optional
from datetime import datetime
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass

from tracklater import settings
from tracklater.models import Entry, Issue, Project


@dataclass
class CachingData:
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    issue_count: Optional[int] = None
    entry_count: Optional[int] = None
    project_count: Optional[int] = None


def testing_decorator(func):
    def _func(*args, **kwargs):
        self = args[0]

        if not getattr(settings, 'TESTING', False):
            return func(*args, **kwargs)

        if func.__name__.startswith("test_") or func.__name__.startswith("__"):
            return func(*args, **kwargs)

        if not hasattr(self, "test_{}".format(func.__name__)):
            raise NotImplementedError("No test method for {}.{}".format(self, func.__name__))

        return getattr(self, "test_{}".format(func.__name__))(*args[1:], **kwargs)

    return _func


class ProviderMetaclass(type):
    def __new__(cls, name, bases, local):
        for attr in local:
            value = local[attr]
            # Static and class methods became callable in Python 3.10; wrapping
            # them would drop the descriptor and pass the first argument as self.
            if isinstance(value, (staticmethod, classmethod)):
                continue
            if callable(value):
                local[attr] = testing_decorator(value)
        return type.__new__(cls, name, bases, local)


class AbstractProvider(metaclass=ProviderMetaclass):
    """
    Will run methods prefixed "test_" when TESTING == True
    """
    pass


class AbstractParser(metaclass=ABCMeta):
    def __init__(self, start_date: datetime, end_date: datetime) -> None:
        self.start_date: datetime = start_date
        self.end_date: datetime = end_date
        self.entries: List[Entry] = []
        self.projects: List[Project] = []
        self.issues: List[Issue] = []
        self.caching: CachingData = CachingData()

    def set_database_values(self, start_date=None, end_date=None, issue_count=None,
                            entry_count=None, project_count=None) -> None:
        self.caching.start_date = start_date
        self.caching.end_date = end_date
        self.caching.issue_count = issue_count
        self.caching.entry_count = entry_count
        self.caching.project_count = project_count

    def get_offset_dates(self):
        """
        Use cached api call start and end date to get a smart timeframe to use
        e.g. We already have an api call for (Tue-Fri), and we try to get data for
        (Mon-Wed). In this case this method returns (Mon-Tue).
        """
        if not self.caching.start_date or not self.caching.end_date:
            return (self.start_date, self.end_date)

        # ---a---c---a---c------
        #    (   )
        # ---a---c-------x------
        #    (   )
        # -a---a-c-------c------
        #  (     )
        if (self.caching.start_date > self.start_date
                and self.caching.end_date >= self.end_date):
            return (self.start_date, self.caching.start_date)

        # ------c-a--a---c------
        #
        # ------x--------x------
        #
        if (self.caching.start_date <= self.start_date
                and self.caching.end_date >= self.end_date):
            return (None, None)

        # ------c---a----c--a---
        #                (  )
        # ------c--------c-a--a-
        #                (    )
        # ------x--------c---a-
        #                (   )
        if (self.caching.start_date <= self.start_date
                and self.caching.end_date < self.end_date):
            return (self.caching.end_date, self.end_date)

        # Other cases, just skip caching
        return (self.start_date, self.end_date)

    @property
    def capabilities(self) -> List[str]:
        return []

    def parse(self) -> None:
        # End of the mixins' super().parse() chain.
        pass


class EntryMixin(AbstractParser):
    def parse(self) -> None:
        self.entries = self.get_entries()
        super(EntryMixin, self).parse()

    @property
    def capabilities(self) -> List[str]:
        _ret = super(EntryMixin, self).capabilities
        return _ret + ['entries']

    @abstractmethod
    def get_entries(self) -> List[Entry]:
        raise NotImplementedError()


class ProjectMixin(AbstractParser):
    def parse(self) -> None:
        self.projects = self.get_projects()
        super(ProjectMixin, self).parse()

    @property
    def capabilities(self) -> List[str]:
        _ret = super(ProjectMixin, self).capabilities
        return _ret + ['projects']

    @abstractmethod
    def get_projects(self) -> List[Project]:
        raise NotImplementedError()


class IssueMixin(AbstractParser):
    def parse(self) -> None:
        self.issues = self.get_issues()
        super(IssueMixin, self).parse()

    @property
    def capabilities(self) -> List[str]:
        _ret = super(IssueMixin, self).capabilities
        return _ret + ['issues']

    @abstractmethod
    def get_issues(self) -> List[Issue]:
        raise NotImplementedError()

    def find_issue(self, uuid) -> Optional[Issue]:
        for issue in self.issues:
            if issue.uuid == uuid:
                return issue
        return None


class AddEntryMixin(AbstractParser):
    @abstractmethod
    def create_entry(self, new_entry: Entry, issue: Issue) -> Entry:
        raise NotImplementedError()

    @property
    def capabilities(self) -> List[str]:
        _ret = super(AddEntryMixin, self).capabilities
        return _ret + ['addentry']


class DeleteEntryMixin(AbstractParser):
    @abstractmethod
    def delete_entry(self, entry_id: str) -> None:
        raise NotImplementedError()

    @property
    def capabilities(self) -> List[str]:
        _ret = super(DeleteEntryMixin, self).capabilities
        return _ret + ['deleteentry']


class UpdateEntryMixin(AbstractParser):
    @abstractmethod
    def update_entry(self, entry_id: str, new_entry: Entry, issue: Issue) -> Entry:
        raise NotImplementedError()

    @property
    def capabilities(self) -> List[str]:
        _ret = super(UpdateEntryMixin, self).capabilities
        return _ret + ['updateentry']
