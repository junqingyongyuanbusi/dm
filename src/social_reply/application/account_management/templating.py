from functools import lru_cache

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup


@lru_cache(maxsize=1)
def _template_environment() -> Environment:
    """Return the shared, fail-fast environment for server-rendered product surfaces."""
    return Environment(
        loader=PackageLoader("social_reply", "templates"),
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
        auto_reload=False,
        enable_async=False,
    )


def trusted_html(value: str) -> Markup:
    """Mark HTML assembled by escaping-aware view helpers as safe at the template boundary."""
    return Markup(value)


def render_template(template_name: str, /, **context: object) -> str:
    return _template_environment().get_template(template_name).render(**context)
