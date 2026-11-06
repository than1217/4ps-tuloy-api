"""Partner contract: tuloy_id is the person identifier; placeholders document only what they can return."""
import pytest

import main
from partner.router import PREFIX
from conftest import bearer
from test_partner import VERIFY

HISTORY = f"{PREFIX}/members/{{}}/enrollment-history"
BAD_IDS = ["x" * 51, "a%20b", "%C3%A4", "a_b", "a.b"]


def spec():
    return main.app.openapi()


def test_old_student_history_path_is_gone(client, fake_db):
    fake_db.scopes[1] = [("tuloy:history:read", "CHED")]
    r = client.get(f"{PREFIX}/students/stu-1/enrollment-history", headers=bearer())
    assert r.status_code == 404 and r.json()["error_code"] == "NOT_FOUND"
    assert f"{PREFIX}/students/{{student_id}}/enrollment-history" not in spec()["paths"]


def test_history_by_tuloy_id(client, fake_db):
    url = HISTORY.format("4ps-1234")
    assert client.get(url).status_code == 401
    assert client.get(url, headers=bearer()).status_code == 403
    fake_db.scopes[1] = [("tuloy:history:read", "CHED")]
    r = client.get(url, headers=bearer())
    assert r.status_code == 501 and r.json()["error_code"] == "NOT_IMPLEMENTED"


@pytest.mark.parametrize("bad", BAD_IDS)
def test_history_rejects_bad_tuloy_id(client, fake_db, bad):
    fake_db.scopes[1] = [("tuloy:history:read", "CHED")]
    r = client.get(HISTORY.format(bad), headers=bearer())
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["path", "tuloy_id"]
    assert bad not in r.text


def test_history_path_traversal_does_not_reach_route(client, fake_db):
    fake_db.scopes[1] = [("tuloy:history:read", "CHED")]
    assert client.get(f"{PREFIX}/members/../x/enrollment-history", headers=bearer()).status_code == 404


@pytest.mark.parametrize("value,status", [("4ps-1234", 501), ("x" * 50, 501), ("", 422), ("x" * 51, 422),
                                          ("a b", 422), ("ä", 422), ("../x", 422)])
def test_list_enrollments_tuloy_id_filter(client, fake_db, value, status):
    fake_db.scopes[1] = [("tuloy:enrollment:read", "CHED")]
    r = client.get(f"{PREFIX}/enrollments", params={"tuloy_id": value}, headers=bearer())
    assert r.status_code == status
    if status == 422:
        assert r.json()["detail"][0]["loc"] == ["query", "tuloy_id"]


@pytest.mark.parametrize("row,expected", [
    ({"tuloy_id": "4ps-1234", "member_id": 123}, {"tuloy_id": "4ps-1234", "member_id": "123"}),
    ({"tuloy_id": 77, "member_id": "m-1"}, {"tuloy_id": "77", "member_id": "m-1"}),
    ({"tuloy_id": None, "member_id": None}, {"tuloy_id": None, "member_id": None}),
])
def test_verify_ids_are_text(client, fake_db, row, expected):
    fake_db.scopes[1] = [("tuloy:member:verify", "CHED")]
    fake_db.on(r"FROM vw_verify_member_bene", lambda sql, p: [row])
    r = client.post(f"{PREFIX}/members/verify", headers=bearer(), json=VERIFY)
    assert r.status_code == 200 and r.json() == {"status": "Matched", **expected}


def test_verify_response_schema_is_string_only():
    s = spec()["components"]["schemas"]["MemberVerifyResponse"]
    assert s["properties"]["status"]["enum"] == ["Matched", "Not Found", "Multiple Match Found"]
    for name in ("tuloy_id", "member_id"):
        types = {b.get("type") for b in s["properties"][name]["anyOf"]}
        assert types == {"string", "null"}
    examples = spec()["paths"][f"{PREFIX}/members/verify"]["post"]["responses"]["200"]["content"]["application/json"]["examples"]
    assert set(examples) == {"Matched", "Not Found", "Multiple Match Found"}
    assert isinstance(examples["Matched"]["value"]["member_id"], str)


def test_suffix_documented_as_unsupported():
    prop = spec()["components"]["schemas"]["MemberVerifyRequest"]["properties"]["suffix"]
    assert "Not supported" in prop["description"] and "examples" not in prop


def test_nullable_fields_have_one_title():
    schemas = spec()["components"]["schemas"]
    for name in ("MemberVerifyRequest", "MemberVerifyResponse"):
        for prop in schemas[name]["properties"].values():
            if "anyOf" in prop:
                assert "title" not in prop and all("title" not in b for b in prop["anyOf"])


PLACEHOLDERS = [("post", f"{PREFIX}/enrollments"), ("get", f"{PREFIX}/enrollments"),
                ("get", f"{PREFIX}/enrollments/{{transaction_id}}"),
                ("post", f"{PREFIX}/enrollments/{{transaction_id}}/events"),
                ("get", f"{PREFIX}/members/{{tuloy_id}}/enrollment-history")]


@pytest.mark.parametrize("method,path", PLACEHOLDERS)
def test_placeholders_document_only_what_they_return(method, path):
    op = spec()["paths"][path][method]
    codes = set(op["responses"])
    assert not codes & {"200", "201", "404", "409"}
    assert {"401", "403", "422", "429", "500", "501", "503"} <= codes
    table = [line for line in op["description"].splitlines() if line.startswith("| ")]
    assert not any(line.startswith(("| 200 ", "| 404 ", "| 409 ")) for line in table)


@pytest.mark.parametrize("method,path", PLACEHOLDERS + [("post", f"{PREFIX}/members/verify")])
def test_error_examples_match_the_app(method, path):
    op = spec()["paths"][path][method]
    r = op["responses"]
    assert r["403"]["content"]["application/json"]["example"]["detail"] == "Not permitted for this operation"
    assert set(r["500"]["content"]["application/json"]["examples"]) == {"DB_ERROR", "INTERNAL_ERROR"}
    assert set(r["503"]["content"]["application/json"]["examples"]) == {"DB_UNAVAILABLE", "DB_TIMEOUT"}
    loc = r["422"]["content"]["application/json"]["example"]["detail"][0]["loc"][0]
    assert loc == ("query" if path.endswith("/enrollments") and method == "get" else
                   "path" if method == "get" or path.endswith("/events") else "body")


@pytest.mark.parametrize("path", [f"{PREFIX}/members/verify", f"{PREFIX}/enrollments/{{transaction_id}}/events"])
def test_body_routes_document_413(path):
    assert "413" in spec()["paths"][path]["post"]["responses"]


def test_403_example_matches_real_denial(client, fake_db):
    r = client.get(f"{PREFIX}/enrollments", headers=bearer())
    assert r.status_code == 403 and r.json()["detail"] == "Not permitted for this operation"


@pytest.mark.parametrize("path,name", [(f"{PREFIX}/enrollments", "after"), (f"{PREFIX}/enrollments", "tuloy_id"),
                                       (f"{PREFIX}/members/{{tuloy_id}}/enrollment-history", "after")])
def test_optional_query_params_are_plain_strings(path, name):
    param = next(p for p in spec()["paths"][path]["get"]["parameters"] if p["name"] == name)
    assert param["schema"]["type"] == "string" and "anyOf" not in param["schema"]


def test_empty_after_is_still_rejected(client, fake_db):
    fake_db.scopes[1] = [("tuloy:enrollment:read", "CHED")]
    assert client.get(f"{PREFIX}/enrollments?after=", headers=bearer()).status_code == 422
    assert client.get(f"{PREFIX}/enrollments", headers=bearer()).status_code == 501


def _served(client, fake_db, internal):
    if not internal:
        return client.get("/tuloy/openapi.json").json()
    fake_db.scopes[4] = [("tuloy:docs:internal", "DSWD")]
    return client.get("/tuloydswd/openapi.json", headers=bearer("no-grants")).json()


@pytest.mark.parametrize("internal", [False, True])
def test_served_spec_keeps_null_ids_in_verify_examples(client, fake_db, internal):
    ex = (_served(client, fake_db, internal)["paths"][f"{PREFIX}/members/verify"]["post"]["responses"]["200"]
          ["content"]["application/json"]["examples"])
    for name in ("Not Found", "Multiple Match Found"):
        assert ex[name]["value"] == {"status": name, "tuloy_id": None, "member_id": None}
    assert ex["Matched"]["value"] == {"status": "Matched", "tuloy_id": "4ps-1234", "member_id": "example-member-0001"}


@pytest.mark.parametrize("internal", [False, True])
def test_served_spec_restores_every_null_example(client, fake_db, internal):
    from routers.docs_portal import _has_none, api_routes
    paths = _served(client, fake_db, internal)["paths"]
    checked = 0
    for route in api_routes(main.app.routes):
        if route.path_format not in paths:
            continue
        for method in route.methods:
            op = paths[route.path_format].get(method.lower())
            for code, src in (route.responses or {}).items():
                for media, content in ((src or {}).get("content") or {}).items():
                    for key in ("example", "examples"):
                        if op and key in content and _has_none(content[key]):
                            assert op["responses"][str(code)]["content"][media][key] == content[key]
                            checked += 1
    assert checked >= 1


def test_null_example_restore_does_not_touch_runtime(client, fake_db):
    fake_db.scopes[1] = [("tuloy:member:verify", "CHED")]
    fake_db.on(r"FROM vw_verify_member_bene", lambda sql, p: [])
    r = client.post(f"{PREFIX}/members/verify", headers=bearer(), json=VERIFY)
    assert r.json() == {"status": "Not Found", "tuloy_id": None, "member_id": None}
