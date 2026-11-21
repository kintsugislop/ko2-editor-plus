#!/bin/sh
# Double-click in Finder to start the EP-133 Station (it opens the browser by itself).
# Close this Terminal window, or press Ctrl+C in it, to stop the station.
cd "$(dirname "$0")" || exit 1
exec /bin/sh ./station "$@"
