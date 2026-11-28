"""Catalogue reclassify/clear is the CLI domain act, including lock refusals; N7 type marks."""

import json
from typing import Any
from urllib.parse import urlencode

import pytest

from hullgate.admin.base import TextJSONResponse
from hullgate.admin.policies import _change_doc
from hullgate.pinning import Change, Field
from tests.admin_support import Console
from tests.policy_support import catalogue, gitea
from tests.relay_bed import A, Bed
from tests.support import Gateway


def test_br_211_catalogue_supports_reclassify_and_clear(gw: Gateway) -> None:
    bed = Bed(gw)
    gitea(bed, "gt", A)
    catalogue(gw, "mpx/gt", A)
    gw.ok("tool", "classify", "sgt", "issue_write", "write")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)

        def read() -> list[dict[str, Any]]:
            response = console.call(client, "GET", "tools/catalogue?service=sgt")
            assert response.status_code == 200, response.text
            value: list[dict[str, Any]] = response.json()["catalogue"]
            return value

        assert any(t["name"] == "issue_write" and t["class"] == "write" for t in read())
        for cls in ("read", "clear"):
            response = console.call(
                client,
                "POST",
                "tools/classify",
                {"service": "sgt", "tool": "issue_write", "class": cls},
            )
            assert response.status_code == 200, response.text
            assert response.json()["source"] == ("manual" if cls == "read" else "default")
        gw.ok("tree", "lock", A)
        cli = gw.cli("tool", "classify", "sgt", "issue_write", "read")
        api = console.call(
            client,
            "POST",
            "tools/classify",
            {"service": "sgt", "tool": "issue_write", "class": "read"},
        )
        assert api.status_code == 409
        assert cli.err.strip() == "refused: " + api.json()["message"]
        assert console.call(client, "GET", "tools/catalogue?service=").status_code == 409


def test_br_211_catalogue_pages_without_skipping_names(gw: Gateway) -> None:
    bed = Bed(gw)
    gitea(bed, "gt", A)
    catalogue(gw, "mpx/gt", A)
    for name in ("issue_write", "pull_request_write", "list_issues"):
        gw.ok("tool", "classify", "sgt", name, "write")
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        all_rows = console.call(client, "GET", "tools/catalogue").json()["catalogue"]
        assert len(all_rows) > 2
        seen = []
        query = {"limit": "2"}
        while True:
            response = console.call(client, "GET", "tools/catalogue?" + urlencode(query))
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page["catalogue"]) <= 2
            seen.extend(page["catalogue"])
            if page["next"] is None:
                break
            query = {"limit": "2", **page["next"]}
        assert seen == all_rows
        for bad in ("limit=0", "limit=501", "after_service=sgt", "after_name=x"):
            assert console.call(client, "GET", "tools/catalogue?" + bad).status_code == 422


@pytest.mark.parametrize(
    ("number", "spelling"),
    [(float("nan"), "NaN"), (float("inf"), "Infinity"), (float("-inf"), "-Infinity")],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_br_182_diff_keeps_nonfinite_type_changes_visible(
    number: float, spelling: str, reverse: bool
) -> None:
    old, new = (number, spelling) if not reverse else (spelling, number)
    change = Change(
        "mpx/gt",
        A,
        "tool",
        "changed",
        "a",
        "b",
        (Field("inputSchema", {"a/b": [old]}, {"a/b": [new]}),),
    )
    response = TextJSONResponse(_change_doc(change))
    field = json.loads(bytes(response.body))["fields"][0]
    assert field["old"] == field["new"]  # JSON-safe text is equal, types are not
    assert field["type_marks"] == [
        {
            "path": "/a~1b/0",
            "old": "string" if reverse else "number",
            "new": "number" if reverse else "string",
        }
    ]


def test_br_182_http_diff_marks_string_to_nonfinite(gw: Gateway) -> None:
    from tests.fake_upstream import READ, tool

    bed = Bed(gw)
    up = bed.connection("gt", A)
    up.tools = [tool("read", {"type": "object", "default": "NaN"}, annotations=READ)]
    gw.ok("policy", "pin", "mpx/gt", "--role", A, "on")
    catalogue(gw, "mpx/gt", A)
    up.tools = [tool("read", {"type": "object", "default": float("nan")}, annotations=READ)]
    console = Console(gw)
    with console.client() as client:
        console.admin(client)
        answer = console.call(client, "POST", "tools/diff", {"path": A})
        assert answer.status_code == 200, answer.text
        fields = answer.json()["changes"][0]["fields"]
        schema = next(f for f in fields if f["field"] == "inputSchema")
        assert schema["type_marks"] == [{"path": "/default", "old": "string", "new": "number"}]
