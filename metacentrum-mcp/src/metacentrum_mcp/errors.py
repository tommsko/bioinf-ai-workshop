"""Error types. Every message is written for the agent: say what failed and what to do next."""


class McpGridError(Exception):
    """Base class; the server turns these into tool errors with the message verbatim."""


class ConfigError(McpGridError):
    pass


class NotConnected(McpGridError):
    pass


class SSHError(McpGridError):
    pass


class RemoteCommandError(McpGridError):
    pass


class PolicyError(McpGridError):
    """A guardrail refused the request. The message names the limit and how to change it."""


class NotFound(McpGridError):
    pass
