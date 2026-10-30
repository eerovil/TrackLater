"""
The timemodule that manual/billing entries are written to.

Everything downstream -- the AI/rule-based populate paths, work inference, the
views and the frontend -- bills through one module. Re-exporting it from here
keeps that choice in a single place instead of scattering the module's name
across the codebase; swapping providers is a change to the import below.

Kimai writes straight through to its API: create/update/delete each hit the
remote immediately, so there is no draft state and no push queue. (The toggl
module, which batched writes as local drafts behind a rate-limited sync worker,
is still importable but is no longer wired to anything.)
"""
from tracklater.timemodules.kimai import (  # noqa: F401
    MODULE_NAME,
    Parser,
    default_project_pid,
    resolve_entry_group_project,
)

BILLING_MODULE = MODULE_NAME
