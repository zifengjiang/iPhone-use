"""The one error type every tool boundary raises.

`uncertain` means the phone may have acted before the call failed, so a retry could
repeat the action. Tool results carry it as `action_executed=false, replay_action=false`.
"""


class PUAError(Exception):
    def __init__(self, code, message, uncertain=False, details=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.uncertain, self.details = uncertain, details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "uncertain": self.uncertain, **self.details}


def fail(code, message, **details):
    raise PUAError(code, message, details=details)


def unsupported(message):
    raise PUAError("unsupported", message, details={"action_executed": False})
