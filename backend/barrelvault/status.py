"""Task status values. Todo and in progress are open; completed and dropped are closed."""

TODO = 0
IN_PROGRESS = 1
COMPLETED = 2
DROPPED = 3

STATUSES = (TODO, IN_PROGRESS, COMPLETED, DROPPED)
OPEN = (TODO, IN_PROGRESS)
CLOSED = (COMPLETED, DROPPED)
