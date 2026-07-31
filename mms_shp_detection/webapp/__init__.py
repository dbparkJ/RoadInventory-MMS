"""Server-side web application for browsing and processing MMS datasets.

The web package deliberately keeps filesystem paths behind opaque API IDs.  Use
``create_app`` in tests or an ASGI server and pass explicit storage roots when
the server must expose locations outside the project data directory.
"""

from .app import WebAppConfig, create_app

__all__ = ["WebAppConfig", "create_app"]
