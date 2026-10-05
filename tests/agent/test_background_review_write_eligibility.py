"""The review fork is told up front which consulted skills it may write."""

import json
from unittest.mock import patch

from agent.background_review import _consulted_skill_names, _skill_write_eligibility_block

def _call(name):
    return {"id": name, "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": name})}}


def _snapshot():
    return [
        {"role": "user", "content": '[IMPORTANT: The user has invoked the "pinned-skill" skill, indicating '
                                    "they want you to follow its instructions.]\n\nfix the thing"},
        {"role": "user", "content": '[IMPORTANT: The user has invoked the "/clean /work" stacked skill bundle, '
                                    "loading 2 skills together. Treat every skill below as active guidance for this turn.]\n\n"
                                    "Skills loaded: clean, work\nUser instruction: go"},
        {"role": "user", "content": '[IMPORTANT: The "channel-skill" skill is auto-loaded. Follow its instructions for this session.]\n\nhi'},
        {"role": "user", "content": '[IMPORTANT: The "cfg-skill" skill is auto-loaded via config (skills.auto_load). Treat its '
                                    "instructions as active guidance for the duration of this session unless the user overrides them.]"},
        {"role": "assistant", "content": None, "tool_calls": [
            _call("managed-skill"), _call("pinned-skill"),
            {"id": "c3", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "x"})}},
        ]},
        {"role": "tool", "tool_call_id": "managed-skill", "content": "..."},
    ]


def test_consulted_skill_names_cover_every_scaffold_shape_and_skill_view_calls():
    assert _consulted_skill_names(_snapshot()) == [
        "pinned-skill", "clean", "work", "channel-skill", "cfg-skill", "managed-skill"]


def test_eligibility_block_labels_by_structured_reason_not_by_message_text(tmp_path):
    # ``bundled-cookbook`` is a user-owned skill whose NAME contains the word "bundled": the
    # verdict must still read "not curator-managed", the one with a way in (`hermes curator adopt`).
    for name in ("pinned-skill", "managed-skill", "bundled-cookbook"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n# {name}\n")
    usage = {
        "pinned-skill": {"pinned": True, "created_by": "agent"},
        "managed-skill": {"pinned": False, "created_by": "agent"},
        "bundled-cookbook": {"pinned": False, "created_by": None},
    }
    snapshot = [{"role": "assistant", "content": None,
                 "tool_calls": [_call("pinned-skill"), _call("managed-skill"), _call("bundled-cookbook")]}]
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]), \
         patch("tools.skill_usage.get_record", side_effect=lambda n: usage.get(n, {})), \
         patch("tools.skill_usage.load_usage", return_value=usage), \
         patch("tools.skill_usage.is_protected_builtin", return_value=False), \
         patch("tools.skill_usage.is_hub_installed", return_value=False), \
         patch("tools.skill_usage.is_bundled", return_value=False):
        block = _skill_write_eligibility_block(snapshot)
        memory_only = _skill_write_eligibility_block(snapshot, review_skills=False)

    assert "pinned-skill — PROTECTED (pinned)" in block
    assert "bundled-cookbook — PROTECTED (not curator-managed" in block
    assert "managed-skill — writable" in block
    # A memory-only review has no skill options to send the fork to.
    assert memory_only == ""
    assert _skill_write_eligibility_block([{"role": "user", "content": "hi"}]) == ""
