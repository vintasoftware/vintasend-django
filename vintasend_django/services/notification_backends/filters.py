"""Translate a ``NotificationFilter`` into a Django ``Q``.

Every leaf is dispatched on the *shape of its value*, checked by one of the type guards
``vintasend.services.notification_backends.filters`` exports before the value is read, instead
of probing for dict keys inline. Sharing the guards with the reference in-memory evaluator is
the point: this module and that evaluator accept and reject exactly the same values, so a
filter cannot mean one thing against the fakes and another against SQL.

A value that does not fit the shape its field declares translates to the match-nothing ``Q``
rather than raising, which keeps the rule the evaluator follows: a filter the backend cannot
honour matches no row -- and, since negation is pushed to the leaves, matches every row under
``not``.

Choice fields (``status``, ``notification_type``) resolve through ``to_choice_member``, so a
member is used as-is, a wire value is accepted only when it names a real member of that field's
own ``Enum``, and anything else matches nothing. That replaces a generic
``hasattr(value, "value")`` normalization, which happily accepted an unrelated enum or a
misspelled status and pushed it into SQL as a silently empty result.
"""

import functools
from enum import Enum
from typing import TypeAlias

from django.db.models import Q

from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.services.notification_backends.filters import (
    is_date_range,
    is_membership_value,
    is_sequence_filter,
    is_string_filter_lookup,
    is_template_version_value,
    to_choice_member,
)


# Filter-field name -> notification model field, split by how each field is matched. Mirrors the
# maps in ``vintasend.services.notification_backends.filters`` so this SQL translation stays
# faithful to the reference in-memory evaluator.
ChoiceFieldSpec: TypeAlias = tuple[str, type[Enum]]

CHOICE_FIELDS: dict[str, ChoiceFieldSpec] = {
    "status": ("status", NotificationStatus),
    "notification_type": ("notification_type", NotificationTypes),
}
MEMBERSHIP_FIELDS: dict[str, str] = {
    "adapter_used": "adapter_used",
    "user_id": "user_id",
    "tenant": "tenant",
}
VERSION_FIELDS: dict[str, str] = {
    "requested_template_version": "requested_template_version",
    "used_template_version": "used_template_version",
}
STRING_LOOKUP_FIELDS: dict[str, str] = {
    "body_template": "body_template",
    "subject_template": "subject_template",
    "context_name": "context_name",
}
RANGE_FIELDS: dict[str, str] = {
    "send_after_range": "send_after",
    "created_at_range": "created",
    "sent_at_range": "sent_at",
    "read_at_range": "read_at",
}
# order_by field name -> model field. ``created_at`` maps to ``created`` and ``updated_at`` to
# ``modified``, matching the model's ``AutoCreatedField`` / ``AutoLastModifiedField``.
ORDER_FIELD_TO_ATTR: dict[str, str] = {
    "send_after": "send_after",
    "sent_at": "sent_at",
    "read_at": "read_at",
    "created_at": "created",
    "updated_at": "modified",
}
# Django lookup suffixes for each string lookup, case-sensitive first / case-insensitive second.
STRING_LOOKUP_SUFFIX: dict[str, tuple[str, str]] = {
    "exact": ("exact", "iexact"),
    "starts_with": ("startswith", "istartswith"),
    "ends_with": ("endswith", "iendswith"),
    "includes": ("contains", "icontains"),
}
# A Q that matches no row, used for an unknown filter field, for a value that does not fit its
# field, and for ``not {}`` (negating the match-everything empty filter).
MATCH_NOTHING = Q(pk__in=[])


def string_lookup_q(model_field: str, spec: object) -> Q | None:
    """``Q`` for a string field, or ``None`` if ``spec`` is neither a ``str`` nor a valid lookup.

    A bare ``str`` means a case-sensitive ``exact`` match, as the filter vocabulary documents.
    """
    if isinstance(spec, str):
        return Q(**{f"{model_field}__exact": spec})
    if not is_string_filter_lookup(spec):
        return None
    sensitive_suffix, insensitive_suffix = STRING_LOOKUP_SUFFIX[spec.get("lookup", "exact")]
    suffix = sensitive_suffix if spec.get("case_sensitive", True) else insensitive_suffix
    return Q(**{f"{model_field}__{suffix}": spec["value"]})


def date_range_q(model_field: str, spec: object) -> Q | None:
    """``Q`` for a date range, or ``None`` if ``spec`` is not a ``DateRange``.

    Both bounds are inclusive. A range with no bounds still excludes NULL rows: the reference
    evaluator never matches a ``None`` with a range filter, and neither does a bounded SQL
    comparison, so an unbounded range must not become the match-everything ``Q``.
    """
    if not is_date_range(spec):
        return None
    lower = spec.get("from")
    upper = spec.get("to")
    if lower is None and upper is None:
        return Q(**{f"{model_field}__isnull": False})
    query = Q()
    if lower is not None:
        query &= Q(**{f"{model_field}__gte": lower})
    if upper is not None:
        query &= Q(**{f"{model_field}__lte": upper})
    return query


def choice_q(model_field: str, enum_cls: type[Enum], spec: object) -> Q | None:
    """``Q`` for an enum-backed field, or ``None`` if a candidate is not a member of ``enum_cls``.

    The column stores the member's ``.value``, so every candidate is resolved to a member first
    and only then unwrapped. One bad candidate rejects the whole leaf, matching the evaluator.
    """
    candidates = list(spec) if is_sequence_filter(spec) else [spec]
    members = [to_choice_member(candidate, enum_cls) for candidate in candidates]
    if any(member is None for member in members):
        return None
    return Q(**{f"{model_field}__in": [member.value for member in members if member is not None]})


def version_q(model_field: str, spec: object) -> Q | None:
    """``Q`` for an integer template-version field, or ``None`` if a candidate is not an ``int``.

    Kept apart from ``membership_q``, which stringifies its candidates: these columns are
    ``IntegerField``s, so a stringified candidate would have to be cast back and a non-numeric
    one would raise ``ValueError`` out of Django's field preparation rather than matching no
    row. Validating with the vocabulary's own guard rejects it here instead, which is what the
    reference evaluator does.
    """
    candidates = list(spec) if is_sequence_filter(spec) else [spec]
    if not all(is_template_version_value(candidate) for candidate in candidates):
        return None
    return Q(**{f"{model_field}__in": candidates})


def membership_q(model_field: str, spec: object) -> Q | None:
    """``Q`` for a scalar-or-list field, or ``None`` if any candidate is not a scalar value.

    Candidates are compared as strings, matching the reference evaluator, which normalizes both
    sides with ``str`` so a ``uuid.UUID`` and its textual form are the same id.
    """
    candidates = list(spec) if is_sequence_filter(spec) else [spec]
    if not all(is_membership_value(candidate) for candidate in candidates):
        return None
    return Q(**{f"{model_field}__in": [str(candidate) for candidate in candidates]})


def _leaf(query: Q | None, model_field: str) -> tuple[Q, str | None]:
    if query is None:
        # A value that does not fit its field matches no row, and negating it must therefore
        # match every row -- so there is no nullable column left to widen the negation with.
        return MATCH_NOTHING, None
    return query, model_field


def field_leaf(field: str, value: object) -> tuple[Q, str | None]:
    """Positive ``Q`` for one field filter, plus the model field to OR ``__isnull`` on when this
    leaf is negated.

    Returns the match-nothing ``Q`` (and no null field) for an unknown field or a value that does
    not fit the field, mirroring the reference evaluator's "an unknown field never matches".
    """
    if field in RANGE_FIELDS:
        model_field = RANGE_FIELDS[field]
        return _leaf(date_range_q(model_field, value), model_field)
    if field in STRING_LOOKUP_FIELDS:
        model_field = STRING_LOOKUP_FIELDS[field]
        return _leaf(string_lookup_q(model_field, value), model_field)
    if field in CHOICE_FIELDS:
        model_field, enum_cls = CHOICE_FIELDS[field]
        return _leaf(choice_q(model_field, enum_cls, value), model_field)
    if field in VERSION_FIELDS:
        model_field = VERSION_FIELDS[field]
        return _leaf(version_q(model_field, value), model_field)
    if field in MEMBERSHIP_FIELDS:
        model_field = MEMBERSHIP_FIELDS[field]
        return _leaf(membership_q(model_field, value), model_field)
    return MATCH_NOTHING, None


def and_all(queries: list[Q]) -> Q:
    if not queries:
        return Q()
    return functools.reduce(lambda left, right: left & right, queries)


def or_all(queries: list[Q]) -> Q:
    if not queries:
        return MATCH_NOTHING
    return functools.reduce(lambda left, right: left | right, queries)
