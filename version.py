"""Single source of truth for the application version.

`release.ps1` rewrites the __version__ line when cutting a new release, and the
in-app updater compares this value against the latest GitHub Release tag.
"""

__version__ = "1.0.0"
