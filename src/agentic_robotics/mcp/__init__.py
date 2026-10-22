"""MCP server for AIR skills (W6.3). Tools are proposals that go through ``Runtime.submit``;
a human approver can always deny. Standard library only; see :mod:`.server`."""

from .server import (
    LEGACY,
    MODERN,
    SUPPORTED,
    Approver,
    Err,
    McpSkillServer,
    Proposal,
    allow_skills,
    deny_all,
)

__requirements__ = ("REQ05", "REQ17")

__all__ = ["LEGACY", "MODERN", "SUPPORTED", "Approver", "Err", "McpSkillServer", "Proposal",
           "allow_skills", "deny_all"]
