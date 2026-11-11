from __future__ import annotations

import json

import pytest

from keelchat.cli import main
from keelchat.policy.compiler import build, build_file
from keelchat.policy.errors import PolicyValidationError
from keelchat.policy.memory_types import type_to_schema, validate_value
from keelchat.policy.schema import policy_json_schema
from tests.conftest import EXAMPLE, ROOT
from tests.helpers import mutate


def paths(exc: PolicyValidationError) -> list[str]:
    return [e.path for e in exc.errors]


def test_example_compiles_with_defaults(policy_yaml):
    c = build(policy_yaml)
    assert c.agent == "hiring-agent" and c.mode == "adaptive" and len(c.hash) == 16
    assert [s.name for s in c.steps] == ["greet_consent", "job_search", "form_fill"]
    assert c.step("form_fill").requires == ["job_search"]
    assert c.allowed_tools == ["job_search", "knowledge_search"]  # submit_application is an action, never a tool
    fields = c.step("form_fill").settings["fields"]
    assert fields[0]["max_reasks"] == 3 and fields[1]["max_reasks"] == 2
    mem = {m.name: m for m in c.memory}
    assert mem["location"].half_life_s == 30 * 86400 and mem["location"].schema_ == {"type": "string"}
    assert mem["job_preferences"].schema_["properties"]["shift"]["enum"] == ["day", "night", "any"]
    assert mem["address"].from_ == "form_fill.address" and mem["address"].half_life_s == 180 * 86400 and mem["address"].confirm_on_reuse
    assert not any(m.sensitive for m in c.memory)
    assert c.defaults.exit_message == "Come back anytime to finish." and c.defaults.planner_step_budget == 6
    assert "node:form_fill" in c.manifest_versions and "tool:job_search" in c.manifest_versions


def test_career_coach_compiles_to_planner():
    c = build_file(str(ROOT / "examples" / "career_coach.yaml"))
    assert c.mode == "planner" and not c.has_flow and c.allowed_tools == ["skills_search", "role_search"]
    assert [m.name for m in c.memory] == ["career_goal"] and c.memory[0].extract and c.memory[0].half_life_s == 180 * 86400


def test_minimal_policy_compiles():
    c = build("agent: mini\ncharter: Help.\nflow: [greet_consent]\n")
    assert c.mode == "flow" and c.languages == ["en"] and c.allowed_tools == [] and c.defaults.reasks == 3  # flow is the default with steps


def test_memory_types():
    assert type_to_schema("list[string]") == {"type": "array", "items": {"type": "string"}}
    assert validate_value(
        type_to_schema({"roles": "list[string]", "shift": "day|night|any", "min_pay": "number"}), {"roles": ["nurse"], "shift": "night", "min_pay": 20}
    )
    assert not validate_value(type_to_schema("number"), "twenty")
    with pytest.raises(ValueError):
        type_to_schema("blob")


def test_sensitive_flag_and_overrides(policy_yaml):
    c = build(mutate(policy_yaml, lambda d: d["memory"].append("gender")))
    assert c.memory_item("gender").sensitive
    c = build(mutate(policy_yaml, lambda d: d["memory"].append({"field": "pronouns", "extract": "pronouns", "sensitive": True, "confirm_on_reuse": False})))
    assert c.memory_item("pronouns").sensitive and not c.memory_item("pronouns").confirm_on_reuse


def test_hash_stable_and_sensitive_to_change(policy_yaml):
    assert build(policy_yaml).hash == build(policy_yaml).hash
    assert build(mutate(policy_yaml, lambda d: d.update(charter="other"))).hash != build(policy_yaml).hash


def test_unknown_key(policy_yaml):
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d.update(persona="direct")))
    assert "persona" in paths(ei.value)


def test_unregistered_step_and_tool(policy_yaml):
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d["flow"].append("payment")))
    assert "flow[3]" in paths(ei.value)
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d["tools"].append("crm_lookup")))
    assert "tools[1]" in paths(ei.value)
    with pytest.raises(PolicyValidationError) as ei:  # actions are not tools
        build(mutate(policy_yaml, lambda d: d["tools"].append("submit_application")))
    assert "tools[1]" in paths(ei.value)


def test_requires_missing_and_cycle(policy_yaml):
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d["flow"][2]["form_fill"].update(requires=["nowhere"])))
    assert "flow[2].form_fill.requires[0]" in paths(ei.value)

    def cyc(d):
        d["flow"][0] = {"greet_consent": {"requires": ["form_fill"]}}

    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, cyc))
    assert any(e.path == "flow" and "cycle" in e.message for e in ei.value.errors)


def test_bad_inline_settings(policy_yaml):
    def fn(d):
        d["flow"][2]["form_fill"]["fields"][1]["colour"] = "red"

    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, fn))
    assert "flow[2].form_fill.fields[1].colour" in paths(ei.value)

    def fn2(d):
        del d["flow"][2]["form_fill"]["fields"][1]["expect"]

    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, fn2))
    assert "flow[2].form_fill.fields[1].expect" in paths(ei.value)


def test_memory_validation(policy_yaml):
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d["memory"].append("location")))
    assert "memory[3].field" in paths(ei.value)
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d["memory"].append({"field": "x", "from": "form_fill.nope"})))
    assert "memory[3].from" in paths(ei.value)
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d["memory"].append({"field": "y", "extract": "y", "half_life": "soon"})))
    assert any(p.startswith("memory") for p in paths(ei.value))
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d["memory"].append({"field": "z", "extract": "z", "type": "blob"})))
    assert any(p.startswith("memory") for p in paths(ei.value))


def test_cli(capsys, tmp_path, policy_yaml):
    assert main(["build", str(EXAMPLE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agent"] == "hiring-agent"
    bad = tmp_path / "bad.yaml"
    bad.write_text(mutate(policy_yaml, lambda d: d.update(mode="turbo")))
    assert main(["build", str(bad)]) == 1
    assert "mode" in capsys.readouterr().err
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "form_fill" in schema["properties"]["flow"]["items"]["oneOf"][0]["enum"]
    assert main(["registry"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    kinds = {(e["kind"], e["name"]) for e in manifest["entries"]}
    assert {("action", "submit_application"), ("preprocessor", "shorthand"), ("tool", "job_search"), ("node", "form_fill"), ("guardrail", "pii")} <= kinds


def test_schema_from_registry():
    s = policy_json_schema()
    assert "salary_lookup" in s["properties"]["tools"]["items"]["enum"] and "submit_application" not in s["properties"]["tools"]["items"]["enum"]
    assert any(e["name"] == "shorthand" and e["properties"]["order"] == 30 for e in s["x-registry"]["entries"])


@pytest.mark.parametrize("name", ["hiring_agent", "support_assistant", "career_coach", "sales_analyst"])
def test_every_example_is_valid_against_the_editor_schema(name):
    import jsonschema
    import yaml

    s = policy_json_schema()
    jsonschema.Draft202012Validator.check_schema(s)
    jsonschema.validate(yaml.safe_load((ROOT / "examples" / f"{name}.yaml").read_text()), s)  # the editor must not flag valid policies


def test_editor_schema_covers_what_the_validator_accepts():
    import jsonschema

    s = policy_json_schema()
    ok = {
        "agent": "a",
        "charter": "c",
        "flow": [{"analytics": {"dataset": "sales", "repairs": {"sql": 1}}}, {"knowledge": {"repairs": 1}}, "exit"],
        "limits": {"reasks": 2, "reopens": 0, "planner_calls": 3, "on_step_enquiries": 2, "repairs": 1},
        "memory": ["location", {"field": "goal", "extract": "Their goal", "half_life": "30d", "sensitive": True}],
    }
    jsonschema.validate(ok, s)
    build(__import__("yaml").safe_dump(ok), datasets={"sales": "structured"})  # and the validator agrees
    for bad in ({"flow": ["nope"]}, {"limits": {"reasks": 0}}, {"memory": [{"field": "x", "half_life": "soon"}]}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**ok, **bad}, s)
    analytics = next(v for v in s["properties"]["flow"]["items"]["oneOf"] if v.get("title") == "analytics")
    assert analytics["properties"]["analytics"]["properties"]["dataset"]["x-dataset"] == "structured"  # for the Add step menu


def test_guardrails_block_and_base_set(policy_yaml):
    c = build(policy_yaml)
    assert c.defaults.input_guardrails == ["prompt_injection", "sql_injection"] and c.defaults.output_guardrails == ["pii", "profanity", "no_legal_advice"]
    c = build(mutate(policy_yaml, lambda d: d.update(guardrails={"output": ["profanity"]})))
    assert c.defaults.output_guardrails == ["pii", "profanity"] and c.defaults.input_guardrails == ["prompt_injection", "sql_injection"]
    c = build(mutate(policy_yaml, lambda d: d.update(guardrails={"output": ["profanity"], "input": []})))
    assert c.defaults.input_guardrails == ["prompt_injection"]  # base set always runs even when the list is empty
    cc = build_file(str(ROOT / "examples" / "career_coach.yaml"))
    assert cc.defaults.output_guardrails == ["pii", "no_medical_advice"] and cc.memory_item("career_goal").half_life_s == 180 * 86400
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d.update(guardrails={"output": ["toxicity"]})))
    assert "guardrails.output[0]" in paths(ei.value)
    with pytest.raises(PolicyValidationError) as ei:
        build(mutate(policy_yaml, lambda d: d.update(guardrails={"input": ["pii"]})))
    assert "guardrails.input[0]" in paths(ei.value)


def test_intent_examples_file_loads():
    from keelchat.runtime.decide.intent_detector import few_shot, load_examples

    ex = load_examples()
    assert {"new_answer", "edit", "enquiry", "off_topic"} <= set(ex["en"]) and "es" in ex
    assert "La segunda" in few_shot("es") and "Dallas" in few_shot("en")


def test_v6_limits_mode_tenant_and_pins(policy_yaml):
    from keelchat.policy.errors import PolicyValidationError
    from tests.helpers import mutate

    c = build(policy_yaml, tenant="acme")
    assert c.tenant == "acme" and set(c.models) == {"small", "large"} and c.mode == "adaptive"
    assert c.defaults.max_reopens == 2 and c.defaults.planner_step_budget == 6 and c.defaults.on_step_enquiries == 3
    c2 = build(mutate(policy_yaml, lambda d: d.update(limits={"reasks": 2, "reopens": 1, "planner_calls": 4})))
    assert c2.defaults.max_reopens == 1 and c2.defaults.planner_step_budget == 4 and c2.defaults.digressions == 3 and c2.hash != c.hash
    assert build(mutate(policy_yaml, lambda d: d.pop("mode"))).mode == "flow"  # flow is the default when steps exist
    with pytest.raises(PolicyValidationError) as e:
        build(mutate(policy_yaml, lambda d: (d.pop("flow"), d.pop("memory"), d.update(mode="adaptive"))))
    assert e.value.errors[0].path == "mode"
    with pytest.raises(PolicyValidationError) as e:
        build(mutate(policy_yaml, lambda d: d.update(tools=["knowledge_serch"])))
    assert "did you mean 'knowledge_search'" in e.value.errors[0].message


@pytest.mark.parametrize("name", ["support_assistant", "hiring_agent", "career_coach"])
def test_every_example_builds(name):
    from tests.conftest import ROOT

    c = build_file(str(ROOT / "examples" / f"{name}.yaml"))
    assert c.hash and c.mode in ("flow", "adaptive", "planner") and c.defaults.input_guardrails[0] == "prompt_injection"


def test_readme_first_bot_builds():
    readme = (ROOT / "README.md").read_text()
    snippet = readme.split("```yaml\n", 1)[1].split("```", 1)[0]
    assert build(snippet).agent == "support-assistant"


def test_unparseable_yaml_is_a_policy_error_with_its_line():
    with pytest.raises(PolicyValidationError) as e:
        build("agent: a\ncharter: c\nflow:\n  - x: {a: 1}}\n")
    assert e.value.errors[0].path.startswith("line 4") and "invalid YAML" in e.value.errors[0].message


def test_yes_and_no_are_words_not_booleans():
    """YAML 1.1 reads bare yes/no/on/off as true/false (the "Norway problem"); a survey option labelled Yes must stay
    the word Yes. true/false are still booleans."""
    text = (ROOT / "examples" / "nps_survey.yaml").read_text()
    text = text.replace("options: [Search engine, Social media, A friend, Other]", "options: [Yes, No, On, Off]")
    text = text.replace("ask_why: false", "ask_why: no")  # a real boolean field still accepts the word
    survey = build(text).step("survey").settings["questions"]
    channel = next(q for q in survey if q["key"] == "channel")
    assert [o if isinstance(o, str) else o["label"] for o in channel["options"]] == ["Yes", "No", "On", "Off"]


def test_a_handoff_trigger_listed_twice_is_rejected_with_its_path() -> None:
    from keelchat.policy.compiler import build
    from keelchat.policy.errors import PolicyValidationError
    from tests.conftest import ROOT

    support = (ROOT / "examples" / "support_assistant.yaml").read_text()
    with pytest.raises(PolicyValidationError) as e:
        build(support.replace("when: [user_request, max_reasks]", "when: [user_request, user_request]"))
    assert [(x.path, x.message) for x in e.value.errors] == [("handoff.when", "listed twice: user_request")]


def test_the_language_reference_names_every_key_the_build_accepts() -> None:
    """docs/POLICY_LANGUAGE.md is the definition of the policy language: a key the build accepts but the page does not
    name is a gap in it."""
    from keelchat.policy.schema import policy_json_schema
    from keelchat.registry.core import REGISTRY
    from tests.conftest import ROOT

    doc = (ROOT / "docs" / "POLICY_LANGUAGE.md").read_text()
    s = policy_json_schema()
    keys = set(s["properties"])
    for k in ("guardrails", "limits", "handoff"):
        keys |= set(s["properties"][k]["properties"])
    for alt in s["properties"]["flow"]["items"]["oneOf"][1:]:
        ((name, body),) = alt["properties"].items()
        if not REGISTRY.node(name).cls.__module__.startswith("keelchat."):
            continue  # a package's own step (e.g. booking_caps, registered by another test) documents itself
        keys.add(name)
        for part in [body, *body.get("anyOf", [])]:
            keys |= set(part.get("properties") or {})
    keys |= set(s["properties"]["memory"]["items"]["oneOf"][1]["properties"])
    keys |= set(s["properties"]["rules"]["items"]["properties"])
    for d in ("FieldSpec", "SurveyQuestion", "Objective", "AskIf", "ChoiceOption"):
        keys |= set(s["$defs"][d]["properties"])
    keys |= set(s["$defs"]["QuestionKind"]["enum"]) | set(s["$defs"]["ConditionOp"]["enum"])
    missing = sorted(k for k in keys if f"`{k}`" not in doc)
    assert not missing, f"not defined in docs/POLICY_LANGUAGE.md: {missing}"
