"""Behavioral contract for the shipped Steel Mission page.

The legacy page and its later replacement must both satisfy these outcomes. Keep
this file independent of JavaScript function names and internal DOM layout so the
contract survives the interface flip.
"""
from __future__ import annotations

import copy
import json
import re
import runpy
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


WORKER_DIR = Path(__file__).resolve().parent.parent


class ContractPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: dict[str, dict[str, Any]] = {}
        self.groups: list[dict[str, str]] = []
        self._active: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        attrs = {name: value or "" for name, value in attributes}
        if attrs.get("role") == "group":
            self.groups.append(attrs)
        node_id = attrs.get("id")
        if node_id:
            self.nodes[node_id] = {"tag": tag, "attrs": attrs, "text": []}
            self._active.append((tag, node_id))

    def handle_data(self, data: str) -> None:
        for _tag, node_id in self._active:
            self.nodes[node_id]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._active) - 1, -1, -1):
            if self._active[index][0] == tag:
                self._active.pop(index)
                return


def route_response(chat: dict[str, Any], path: str) -> tuple[int, Any]:
    responses: list[tuple[int, Any]] = []
    globals_ = chat["Handler"].do_GET.__globals__
    globals_["html_response"] = lambda _handler, status, body: responses.append((status, body))
    globals_["json_response"] = lambda _handler, status, body: responses.append((status, body))
    handler = object.__new__(chat["Handler"])
    handler.path = path
    handler.authenticate = lambda _path, _method: {"actorId": "contract-user", "role": "user"}
    handler.do_GET()
    assert len(responses) == 1
    return responses[0]


def ui_contract_errors(
    page_status: int,
    html: str,
    vocabulary_status: int,
    vocabulary: dict[str, Any],
    knowledge: dict[str, Any],
) -> list[str]:
    errors = []
    if page_status != 200 or "<main>" not in html:
        errors.append("the work route must answer with the application page")
    if vocabulary_status != 200 or vocabulary.get("ok") is not True:
        errors.append("the vocabulary route must answer for a signed-in user")

    served = [
        (item.get("capabilityKey"), item.get("displayName"))
        for item in vocabulary.get("capabilities", [])
        if isinstance(item, dict)
    ]
    canonical = [
        (item.get("capabilityKey"), item.get("displayName"))
        for item in knowledge.get("capabilities", [])
        if isinstance(item, dict)
    ]
    if served != canonical:
        errors.append("the capability list must be derived from the knowledge registry")
    for key, display_name in served:
        label = f"{key} · {display_name}"
        if key and label.split(" · ").count(key) != 1:
            errors.append(f"the {key} label must print its key once")

    parser = ContractPageParser()
    parser.feed(html)
    definition = parser.nodes.get("domainCapabilityDefinition")
    definition_term = next(
        (
            item
            for item in vocabulary.get("terms", [])
            if isinstance(item, dict) and item.get("conceptKey") == "domain-capability"
        ),
        {},
    )
    definition_text = str(definition_term.get("description") or "")
    if (
        not definition
        or definition["attrs"].get("data-vocabulary-term") != "domain-capability"
        or not all(term in definition_text.lower() for term in ("assignable", "role", "workflow"))
    ):
        errors.append("the Domain Capability definition must be visible to non-manager users")

    work_mode_group = next(
        (
            group
            for group in parser.groups
            if group.get("aria-labelledby") == "workModeLabel"
        ),
        None,
    )
    description_ids = (
        work_mode_group.get("aria-describedby", "").split() if work_mode_group else []
    )
    description_text = " ".join(
        " ".join(parser.nodes[node_id]["text"]).strip()
        for node_id in description_ids
        if node_id in parser.nodes
    )
    if not all(term in description_text for term in ("Normal chat", "Domain Capabilities")):
        errors.append("the work-mode control must describe what its modes change")
    return errors


def ui_lexicon_errors(html: str) -> list[str]:
    """Return user-visible vocabulary collisions in the shipped page source."""
    errors = []
    capability_labels = re.findall(
        r'<label[^>]*>([^<${}]+?)\s+\$\{renderCapabilityChecks\(',
        html,
        flags=re.IGNORECASE,
    )
    if any(re.search(r"\bProfiles?\b", label, flags=re.IGNORECASE) for label in capability_labels):
        errors.append("Profile must not be adjacent to a capability list")
    if any(re.search(r"\bRole\b", label, flags=re.IGNORECASE) for label in capability_labels):
        errors.append("Role must not label a capability control")

    string_literals = [literal for _quote, literal in re.findall(r'(["`])([^\n]*?)\1', html)]
    static_text = re.findall(r">\s*([^<>{}\n]+?)\s*<", html)
    bare_keys_in_prose = [
        literal
        for literal in [*string_literals, *static_text]
        if re.search(r"\bDC\d{2}\b", literal)
        and re.search(r"\s", literal)
        and re.search(r"[.!?]", literal)
    ]
    if bare_keys_in_prose:
        errors.append("bare capability keys must not appear in prose")

    profile_labels = re.findall(
        r">\s*([^<>{}\n]*\bProfiles?\b[^<>{}\n]*)\s*<",
        html,
        flags=re.IGNORECASE,
    )
    profile_concepts = {
        "snapshot" if re.search(r"\bsnapshot profiles?\b", label, flags=re.IGNORECASE) else "runtime"
        for label in profile_labels
    }
    if len(profile_concepts) > 1:
        errors.append("only one Profile concept may be user-visible")
    return errors


def test_shipped_page_satisfies_the_ui_behavior_contract():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    page_status, html = route_response(chat, "/")
    vocabulary_status, vocabulary = route_response(chat, "/api/vocabulary")

    errors = ui_contract_errors(
        page_status,
        html,
        vocabulary_status,
        vocabulary,
        chat["knowledge_registry"](),
    )

    assert errors == [], errors


def test_ui_behavior_contract_rejects_each_required_regression():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    page_status, html = route_response(chat, "/")
    vocabulary_status, vocabulary = route_response(chat, "/api/vocabulary")
    knowledge = chat["knowledge_registry"]()

    def errors(
        *,
        candidate_page_status: int = page_status,
        candidate_html: str = html,
        candidate_vocabulary_status: int = vocabulary_status,
        candidate_vocabulary: dict[str, Any] = vocabulary,
    ) -> list[str]:
        return ui_contract_errors(
            candidate_page_status,
            candidate_html,
            candidate_vocabulary_status,
            candidate_vocabulary,
            knowledge,
        )

    assert "the work route must answer with the application page" in errors(
        candidate_page_status=404
    )
    assert "the vocabulary route must answer for a signed-in user" in errors(
        candidate_vocabulary_status=503
    )

    missing_capability = copy.deepcopy(vocabulary)
    missing_capability["capabilities"].pop()
    assert "the capability list must be derived from the knowledge registry" in errors(
        candidate_vocabulary=missing_capability
    )

    duplicated_key = copy.deepcopy(vocabulary)
    duplicated_key["capabilities"][0]["displayName"] = duplicated_key["capabilities"][0][
        "capabilityKey"
    ]
    capability_key = duplicated_key["capabilities"][0]["capabilityKey"]
    assert f"the {capability_key} label must print its key once" in errors(
        candidate_vocabulary=duplicated_key
    )

    assert "the Domain Capability definition must be visible to non-manager users" in errors(
        candidate_html=html.replace('id="domainCapabilityDefinition"', 'id="removedDefinition"')
    )
    assert "the work-mode control must describe what its modes change" in errors(
        candidate_html=html.replace(
            'aria-describedby="normalModeDescription domainCapabilityModeDescription"',
            "",
        )
    )


def test_capability_checkbox_labels_are_registry_derived_and_print_each_key_once():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    html = chat["chat_index"]()
    capabilities = chat["ui_vocabulary"]()["capabilities"]
    formatter = re.search(
        r"^    function capabilityLabel\(item\) \{.*?^    \}",
        html,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert formatter, "the page must expose one behavior for every capability label"

    script = (
        formatter.group(0).strip()
        + "; process.stdout.write(JSON.stringify("
        + json.dumps(capabilities)
        + ".map(capabilityLabel)));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    labels = json.loads(result.stdout)
    assert len(labels) == len(capabilities)
    for capability, label in zip(capabilities, labels, strict=True):
        key = capability["capabilityKey"]
        assert label.split(" · ").count(key) == 1
        assert capability["displayName"] in label

    assert 'const capabilityRoles = ["DC13"' not in html
    assert 'new URL("/api/vocabulary", window.location.href)' in html
    assert 'renderCapabilityChecks("visibilityRoleKeys", allCapabilities()' in html


def test_coordinator_model_picker_says_what_it_selects():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    parser = ContractPageParser()
    parser.feed(chat["chat_index"]())

    label = parser.nodes.get("coordinatorModelLabel")
    label_text = " ".join(label["text"]).strip() if label else ""
    picker = parser.nodes.get("profileSelect")
    description_id = picker["attrs"].get("aria-describedby") if picker else None
    description = parser.nodes.get(description_id or "")
    description_text = " ".join(description["text"]).strip() if description else ""

    assert "Coordinator model" in label_text
    assert "Profile" not in label_text
    assert description
    assert "model configuration" in description_text
    assert "executes the Delivery Coordinator" in description_text


def test_domain_capability_definition_is_registry_backed_for_every_access_level():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    html = chat["chat_index"]()
    parser = ContractPageParser()
    parser.feed(html)

    definition = parser.nodes.get("domainCapabilityDefinition")
    assert definition
    assert definition["attrs"].get("data-vocabulary-term") == "domain-capability"
    term = next(
        item
        for item in chat["ui_vocabulary"]()["terms"]
        if item["conceptKey"] == "domain-capability"
    )
    assert all(word in term["description"].lower() for word in ("assignable", "role", "workflow"))
    assert term["description"] not in html
    assert "renderVocabularyTerms()" in html


def test_work_mode_has_a_visible_label_and_accessible_description_per_mode():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    parser = ContractPageParser()
    parser.feed(chat["chat_index"]())

    label = parser.nodes.get("workModeLabel")
    assert label and " ".join(label["text"]).strip() == "Work mode"
    group = next(
        (item for item in parser.groups if item.get("aria-labelledby") == "workModeLabel"),
        None,
    )
    assert group
    assert group.get("aria-describedby", "").split() == [
        "normalModeDescription",
        "domainCapabilityModeDescription",
    ]

    normal_button = parser.nodes["normalMode"]
    capability_button = parser.nodes["domainCapabilityMode"]
    assert normal_button["attrs"].get("aria-describedby") == "normalModeDescription"
    assert capability_button["attrs"].get("aria-describedby") == "domainCapabilityModeDescription"
    normal_text = " ".join(parser.nodes["normalModeDescription"]["text"]).strip()
    capability_text = " ".join(
        parser.nodes["domainCapabilityModeDescription"]["text"]
    ).strip()
    assert "direct prompts and answers" in normal_text
    assert "assigned role and governed knowledge lens" in capability_text
    assert 'setAttribute("aria-pressed"' in chat["chat_index"]()


def test_shipped_page_obeys_the_ui_lexicon():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))

    assert ui_lexicon_errors(chat["chat_index"]()) == []


def test_ui_lexicon_rejects_each_banned_collocation():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    html = chat["chat_index"]()

    profile_capability = html.replace(
        "Visible Domain Capabilities ${renderCapabilityChecks(",
        "Profile ${renderCapabilityChecks(",
        1,
    )
    assert "Profile must not be adjacent to a capability list" in ui_lexicon_errors(
        profile_capability
    )

    role_capability = html.replace(
        "Visible Domain Capabilities ${renderCapabilityChecks(",
        "Role ${renderCapabilityChecks(",
        1,
    )
    assert "Role must not label a capability control" in ui_lexicon_errors(role_capability)

    bare_key = html.replace(
        "Selects the model configuration that executes the Delivery Coordinator.",
        "Selects the model configuration that executes DC13.",
        1,
    )
    assert "bare capability keys must not appear in prose" in ui_lexicon_errors(bare_key)

    competing_profile = html.replace(
        "Coordinator Role",
        "Snapshot Profile",
        1,
    )
    assert "only one Profile concept may be user-visible" in ui_lexicon_errors(
        competing_profile
    )
