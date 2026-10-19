class rollCallAlreadyStarted(Exception):
    pass

class rollCallNotStarted(Exception):
    pass

class rollCallClosed(rollCallNotStarted):
    """The specific rollcall an action was aimed at has ended or been
    cancelled since the action was prepared (a panel tap, a confirmation
    button, a web page loaded earlier).

    A subclass of rollCallNotStarted so every existing handler and the
    bot's reply_error treat it as curated text; the REST layer maps it to 409
    rather than 404 because the request was valid when it was built.
    """
    pass

class insufficientPermissions(Exception):
    pass

class duplicateProxy(Exception):
    pass

class alreadyInList(Exception):
    pass

class repeatlyName(Exception):
    pass

class parameterMissing(Exception):
    pass

class timeError(Exception):
    pass

class incorrectParameter(Exception):
    pass

class amountOfRollCallsReached(Exception):
    pass

class duesGameAlreadyClosed(Exception):
    pass

class duesNothingToClose(Exception):
    pass

class rateLimited(Exception):
    pass

class databaseError(Exception):
    """A write that was asked for did not land.

    Raised by the db writers whose result callers historically discarded, so
    a failed write surfaces instead of the handler going on to announce
    success. The message must stay generic — the underlying driver error is
    logged at the raise site and must never be echoed into a chat.
    """
    pass