"""What goes out in writing: the recap email and the manager's WhatsApp.

MAY IMPORT:  domain, config.
IMPORTED BY: tools.

Implements domain.ports.Notifier. A send failure returns DeliveryResult(status=FAILED) and
never raises: a failed recap means there was no commitment, and an exception here would turn
that state into a crash.

OWNER: Track D.
"""
