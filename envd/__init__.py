"""odoo-envd — CD engine for Odoo branch→database environments.

Design contract: the engine translates git push events and REST calls into
native ``odoo-bin`` commands (``db init/dump/load/duplicate/drop``,
``module install/upgrade/list``, ``neutralize``) executed inside each
environment's own compose project. It owns state (SQLite), jobs, backups and
events; the bash kit under ``kit/`` performs the operations and is also
runnable by hand.
"""

__version__ = "0.1.0"
