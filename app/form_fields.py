"""
Helpers for optional numeric multipart/Form fields.

Swagger UI (and some HTML form clients) send an optional text field as an
empty string "" when the user leaves it blank, rather than omitting the
field entirely. FastAPI/Pydantic then tries to parse "" as the field's
declared type (e.g. float/int) and fails with a 422 "Input should be a
valid number/integer, unable to parse string as a ..." error, even though
the field is declared optional.

`OptionalFloatForm(...)` / `OptionalIntForm(...)` are drop-in replacements
for a `float | None` / `int | None` parameter typed with `Form(...)`: they
run a BeforeValidator that treats an empty/whitespace-only string as if
the field were omitted (-> None), while any real value (numeric, or a
numeric string — multipart fields always arrive as strings) is still
parsed and validated exactly as before. They only affect the
empty-string case — they never widen what counts as a valid number, and
the OpenAPI schema still shows the field as optional/nullable, same as a
plain `float | None = Form(default=None)` would.

Usage — call it in the annotation position, with the actual default
value supplied via `=` (a `Form(...)` default can't be set *inside*
`Annotated`; FastAPI raises an AssertionError if it is):

    location_latitude: OptionalFloatForm() = None
    location_id: OptionalIntForm(description="...") = None
"""

from typing import Annotated

from fastapi import Form
from pydantic import BeforeValidator


def _blank_to_none(value):
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def OptionalFloatForm(*, description: str | None = None):
    """Annotation for a `float | None` Form field that treats "" the
    same as an omitted field, instead of a 422 parse error."""
    return Annotated[
        float | None,
        BeforeValidator(_blank_to_none),
        Form(description=description),
    ]


def OptionalIntForm(*, description: str | None = None):
    """Same as OptionalFloatForm, but for an `int | None` Form field
    (e.g. an id)."""
    return Annotated[
        int | None,
        BeforeValidator(_blank_to_none),
        Form(description=description),
    ]