#!/usr/bin/env python3
"""FlyMerge: a deterministic PR joke judge backed by the local fly connectome.

The connectome is never replaced with random or synthetic data. Missing data is
an error. The diff-to-stimulus vocabulary is intentionally a small heuristic;
the neural part reads the same binary format as FlyBrain's sim-worker.js.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import os
import re
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


GROUPS = (
    "VIS_R1R6", "VIS_R7R8", "VIS_ME", "VIS_LO", "VIS_LC", "VIS_LPTC",
    "OLF_ORN_FOOD", "OLF_ORN_DANGER", "OLF_LN", "OLF_PN", "MECH_BRISTLE",
    "MECH_JO", "MECH_CHORD", "ANTENNAL_MECH", "THERMO_WARM", "THERMO_COOL",
    "NOCI", "MB_KC", "MB_APL", "MB_MBON_APP", "MB_MBON_AV", "MB_DAN_REW",
    "MB_DAN_PUN", "LH_APP", "LH_AV", "CX_EPG", "CX_PFN", "CX_FC",
    "CX_HDELTA", "SEZ_FEED", "SEZ_GROOM", "SEZ_WATER", "GUS_GRN_SWEET",
    "GUS_GRN_BITTER", "GUS_GRN_WATER", "GNG_DESC", "CLOCK_DN", "DRIVE_HUNGER",
    "DRIVE_FEAR", "DRIVE_FATIGUE", "DRIVE_CURIOSITY", "DRIVE_GROOM", "DN_WALK",
    "DN_FLIGHT", "DN_TURN", "DN_BACKUP", "DN_STARTLE", "VNC_CPG", "MN_LEG_L1",
    "MN_LEG_R1", "MN_LEG_L2", "MN_LEG_R2", "MN_LEG_L3", "MN_LEG_R3", "MN_WING_L",
    "MN_WING_R", "MN_PROBOSCIS", "MN_HEAD", "MN_ABDOMEN", "GENERIC_SENSORY",
    "GENERIC_CENTRAL", "GENERIC_DRIVES", "GENERIC_MOTOR",
)
GROUP_ID = {name: index for index, name in enumerate(GROUPS)}

# These groups are absent from the downloaded FAFB brain binary in this repo.
VIRTUAL_GROUPS = {
    "DRIVE_FEAR", "DRIVE_CURIOSITY", "DRIVE_GROOM", "SEZ_GROOM", "CLOCK_DN",
    "DN_WALK", "DN_FLIGHT", "DN_TURN", "DN_BACKUP", "DN_STARTLE",
    "MN_LEG_L1", "MN_LEG_R1", "MN_LEG_L2", "MN_LEG_R2", "MN_LEG_L3", "MN_LEG_R3",
    "MN_WING_L", "MN_WING_R",
}

# The first match is enough to make the machine fun, while every selected
# stimulus still enters the real graph. Counts are capped to keep large diffs
# from becoming an accidental gain control.
RULES = (
    ("verification", re.compile(r"(^|/)(test|tests|spec|__tests__)(/|\.|$)|assert|pytest|jest|vitest", re.I), "OLF_ORN_FOOD", 1.0),
    ("documentation", re.compile(r"(^|/)(readme|docs?|documentation)(/|\.|$)|\.md$|\.mdx$", re.I), "MB_KC", 0.55),
    ("security", re.compile(r"auth|credential|password|secret|token|permission|privilege|xss|csrf|injection|unsafe|\bpii\b", re.I), "OLF_ORN_DANGER", 1.35),
    ("workflow", re.compile(r"\.github/workflows?|actions?|ci/cd|pipeline|deploy|release", re.I), "GNG_DESC", 0.8),
    ("runtime", re.compile(r"worker|thread|async|await|queue|cache|stream|socket|server", re.I), "MECH_JO", 0.75),
    ("visual", re.compile(r"(^|/)(css|style|styles|ui|views?)(/|\.|$)|\.css$|\.html?$|\.tsx?$|\.jsx?$", re.I), "VIS_R1R6", 0.65),
    ("data", re.compile(r"data|schema|migration|sql|json|csv|database|db", re.I), "CX_PFN", 0.65),
    ("deletion", re.compile(r"^[-].{0,80}(delete|remove|drop|rename|deprecat|refactor)", re.I | re.M), "MECH_BRISTLE", 0.6),
)

# Readouts are downstream/circuit groups only. Direct stimulus groups are
# removed again at runtime, so a feature word cannot count as its own output.
# Missing groups remain visible in connectome.virtual_groups.
APP_OUTPUTS = {"MB_KC", "MB_MBON_APP", "LH_APP", "SEZ_FEED", "MN_PROBOSCIS", "MN_HEAD"}
RISK_OUTPUTS = {"OLF_LN", "OLF_PN", "NOCI", "MB_MBON_AV", "LH_AV"}
MOTOR_OUTPUTS = {"VNC_CPG", "MN_PROBOSCIS", "MN_HEAD", "MN_ABDOMEN", "GENERIC_MOTOR"}
SYNAPTIC_GAIN = 16.0
DEFAULT_TICKS = 20
GITHUB_API = "https://api.github.com"
COMMENT_MARKER = "<!-- flymerge:report:v1 -->"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_diff(text: str) -> dict:
    files: list[str] = []
    added = removed = 0
    changed_text: list[str] = []
    current = ""
    for line in text.splitlines():
        if line.startswith("diff --git "):
            match = re.search(r" b/(.+)$", line)
            current = match.group(1) if match else ""
            if current and current not in files:
                files.append(current)
        elif line.startswith("+++ b/"):
            current = line[6:]
            if current not in files:
                files.append(current)
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            if line[0] == "+":
                added += 1
            else:
                removed += 1
            changed_text.append(line[1:])
    return {
        "files": files,
        "added_lines": added,
        "removed_lines": removed,
        "changed_text": "\n".join(changed_text),
        "diff_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def map_stimuli(diff: dict) -> tuple[list[dict], list[dict]]:
    haystack = "\n".join(diff["files"]) + "\n" + diff["changed_text"]
    matches: list[dict] = []
    strengths: dict[str, float] = {}
    for label, pattern, group, weight in RULES:
        count = min(12, len(pattern.findall(haystack)))
        if not count:
            continue
        score = min(4.0, count * weight)
        strengths[group] = strengths.get(group, 0.0) + score
        matches.append({"feature": label, "group": group, "matches": count, "score": round(score, 3)})

    stimuli = []
    for group, score in sorted(strengths.items()):
        # Same base intensity as FlyBrain's worker, with a bounded feature gain.
        stimuli.append({"group": group, "intensity": round(0.15 * (1.0 + min(2.0, score / 4.0)), 4), "score": round(score, 3)})
    return stimuli, matches


class Connectome:
    def __init__(self, path: Path):
        self.path = path
        with gzip.open(path, "rb") as stream:
            self.raw = stream.read()
        if len(self.raw) < 8:
            raise ValueError(f"connectome is too small: {path}")
        self.neurons, self.edges = struct.unpack_from("<II", self.raw, 0)
        edge_end = 8 + self.edges * 12
        meta_end = edge_end + self.neurons * 3
        if meta_end > len(self.raw):
            raise ValueError(f"connectome is truncated: expected {meta_end} bytes, got {len(self.raw)}")
        self.edge_end = edge_end
        self.max_weight = 0.0
        self.row_ptr = [0] * (self.neurons + 1)
        previous_pre = -1
        for offset in range(8, edge_end, 12):
            pre, post, weight = struct.unpack_from("<IIf", self.raw, offset)
            if pre >= self.neurons:
                raise ValueError(f"edge has invalid presynaptic index {pre}")
            if post >= self.neurons:
                raise ValueError(f"edge has invalid postsynaptic index {post}")
            if pre < previous_pre:
                raise ValueError("connectome edges are not sorted by presynaptic index")
            if not math.isfinite(weight):
                raise ValueError(f"edge has non-finite weight at byte offset {offset}")
            previous_pre = pre
            self.row_ptr[pre + 1] += 1
            self.max_weight = max(self.max_weight, abs(weight))
        for index in range(1, len(self.row_ptr)):
            self.row_ptr[index] += self.row_ptr[index - 1]

        self.group_ids = [0] * self.neurons
        self.members = [[] for _ in GROUPS]
        for index in range(self.neurons):
            group = struct.unpack_from("<BH", self.raw, edge_end + index * 3)[1]
            if group >= len(GROUPS):
                raise ValueError(f"neuron has invalid group id {group}")
            self.group_ids[index] = group
            self.members[group].append(index)

    def stats(self) -> dict:
        return {
            "path": str(self.path),
            "bytes_uncompressed": len(self.raw),
            "neuron_count": self.neurons,
            "edge_count": self.edges,
            "max_abs_weight": round(self.max_weight, 6),
            "edge_pre_sorted": True,
            "edge_post_indices_valid": True,
            "edge_weights_finite": True,
            "csr_row_ptr_last": self.row_ptr[-1],
            "csr_row_ptr_monotonic": all(left <= right for left, right in zip(self.row_ptr, self.row_ptr[1:])),
            "nonempty_groups": sum(bool(group) for group in self.members),
            "virtual_groups": sorted(name for name in VIRTUAL_GROUPS if not self.members[GROUP_ID[name]]),
        }

    def simulate(
        self,
        stimuli: list[dict],
        ticks: int,
        pulse_ticks: int | None = None,
        edges_enabled: bool = True,
        synaptic_gain: float = SYNAPTIC_GAIN,
    ) -> dict:
        if ticks < 1 or not math.isfinite(synaptic_gain) or synaptic_gain <= 0:
            raise ValueError("ticks must be positive and synaptic_gain must be finite and positive")
        voltages = [0.0] * self.neurons
        refractory = bytearray(self.neurons)
        active: set[int] = set()
        group_spikes = [0] * len(GROUPS)
        tick_spikes: list[int] = []
        active_neurons_by_tick: list[int] = []
        refractory_neurons_by_tick: list[int] = []
        fired_total = 0
        synaptic_events = 0
        synaptic_abs_input = 0.0
        scale = 0.15 * synaptic_gain / self.max_weight if self.max_weight else 0.0

        real_stimuli = [item for item in stimuli if self.members[GROUP_ID[item["group"]]]]
        direct_group_ids = {GROUP_ID[item["group"]] for item in real_stimuli}
        downstream_group_ids: set[int] = set()
        for tick in range(ticks):
            if pulse_ticks is None or tick < pulse_ticks:
                for item in real_stimuli:
                    for index in self.members[GROUP_ID[item["group"]]]:
                        voltages[index] += item["intensity"]
                        active.add(index)

            fired: list[int] = []
            next_active: set[int] = set()
            for index in active:
                if refractory[index]:
                    refractory[index] -= 1
                    voltages[index] = 0.0
                else:
                    voltages[index] *= 0.95
                if refractory[index] == 0 and voltages[index] >= 1.0:
                    fired.append(index)
                    voltages[index] = 0.0
                    refractory[index] = 3
                    # A fired cell must remain in the state machine so its
                    # refractory countdown advances on later ticks.
                    next_active.add(index)
                elif abs(voltages[index]) > 0.001 or refractory[index]:
                    next_active.add(index)

            for pre in fired:
                group_spikes[self.group_ids[pre]] += 1
                if edges_enabled:
                    start, end = self.row_ptr[pre], self.row_ptr[pre + 1]
                    for offset in range(8 + start * 12, 8 + end * 12, 12):
                        _pre, post, weight = struct.unpack_from("<IIf", self.raw, offset)
                        input_value = weight * scale
                        voltages[post] += input_value
                        synaptic_events += 1
                        synaptic_abs_input += abs(input_value)
                        downstream_group_ids.add(self.group_ids[post])
                        next_active.add(post)
            fired_total += len(fired)
            tick_spikes.append(len(fired))
            active_neurons_by_tick.append(len(active))
            refractory_neurons_by_tick.append(sum(bool(value) for value in refractory))
            active = next_active

        rates = {
            name: round(group_spikes[index] / max(1, ticks * len(self.members[index])), 8)
            for index, name in enumerate(GROUPS)
            if group_spikes[index]
        }
        readout_group_ids = downstream_group_ids - direct_group_ids
        readout_group_spikes = {
            name: group_spikes[group_id]
            for group_id, name in enumerate(GROUPS)
            if group_id in readout_group_ids and group_spikes[group_id]
        }
        app = sum(group_spikes[GROUP_ID[name]] for name in APP_OUTPUTS if GROUP_ID[name] in readout_group_ids)
        risk = sum(group_spikes[GROUP_ID[name]] for name in RISK_OUTPUTS if GROUP_ID[name] in readout_group_ids)
        motor = sum(group_spikes[GROUP_ID[name]] for name in MOTOR_OUTPUTS if GROUP_ID[name] in readout_group_ids)
        readout_fired_total = sum(group_spikes[group_id] for group_id in readout_group_ids)
        return {
            "ticks": ticks,
            "leak_rate": 0.95,
            "threshold": 1.0,
            "refractory_period": 3,
            "weight_scale": 0.15,
            "effective_weight_scale": round(0.15 * synaptic_gain, 6),
            "stimulated_groups": [item["group"] for item in real_stimuli],
            "virtual_stimuli_skipped": [item["group"] for item in stimuli if not self.members[GROUP_ID[item["group"]]]],
            "fired_neurons_total": fired_total,
            "fired_neurons_by_tick": tick_spikes,
            "active_neurons_by_tick": active_neurons_by_tick,
            "refractory_neurons_by_tick": refractory_neurons_by_tick,
            "active_neurons_final": len(active),
            "active_neurons_final_by_group": {
                name: sum(1 for index in active if self.group_ids[index] == group_id)
                for group_id, name in enumerate(GROUPS)
                if any(self.group_ids[index] == group_id for index in active)
            },
            "refractory_neurons_final": sum(bool(value) for value in refractory),
            "edges_enabled": edges_enabled,
            "synaptic_gain": synaptic_gain,
            "downstream_groups": [GROUPS[group_id] for group_id in sorted(downstream_group_ids)],
            "readout_groups": sorted(readout_group_spikes),
            "readout_group_spikes": readout_group_spikes,
            "downstream_fired_neurons_total": sum(group_spikes[group_id] for group_id in downstream_group_ids),
            "readout_fired_neurons_total": readout_fired_total,
            "synaptic_events": synaptic_events,
            "synaptic_abs_input": round(synaptic_abs_input, 6),
            "nonzero_group_rates": rates,
            "appetitive_channel_spikes": app,
            "aversive_channel_spikes": risk,
            "motor_output_spikes": motor,
        }


def decide(diff: dict, simulation: dict) -> dict:
    if not diff["files"] or not (diff["added_lines"] + diff["removed_lines"]):
        return {"verdict": "hold", "confidence": 1.0, "reason": "empty diff"}
    app = simulation["appetitive_channel_spikes"]
    risk = simulation["aversive_channel_spikes"]
    motor = simulation["motor_output_spikes"]
    if simulation["readout_fired_neurons_total"] == 0:
        return {"verdict": "hold", "confidence": 0.95, "reason": "the real connectome produced no downstream readout spikes for this stimulus"}
    if risk > app * 1.25 and risk >= 3:
        confidence = clamp(0.55 + (risk - app) / max(1, risk + app), 0.55, 0.98)
        return {"verdict": "reject", "confidence": round(confidence, 3), "reason": "aversive output dominated appetitive output"}
    if app > risk * 1.1 and app >= 3 and motor >= 2:
        confidence = clamp(0.55 + (app - risk) / max(1, app + risk), 0.55, 0.98)
        return {"verdict": "approve", "confidence": round(confidence, 3), "reason": "approach and motor output outweighed aversive output"}
    if risk == 0 and motor >= 10:
        return {"verdict": "approve", "confidence": 0.55, "reason": "no aversive channel fired and the real motor circuit was active"}
    return {"verdict": "hold", "confidence": 0.6, "reason": "the simulated outputs were mixed or too weak"}


def evaluate(
    diff_text: str,
    data_dir: Path,
    ticks: int,
    compare_no_edges: bool = False,
    synaptic_gain: float = SYNAPTIC_GAIN,
) -> dict:
    diff = parse_diff(diff_text)
    stimuli, matches = map_stimuli(diff)
    connectome_path = data_dir / "connectome.bin.gz"
    if not connectome_path.is_file():
        raise FileNotFoundError(f"missing real connectome: {connectome_path}")
    connectome = Connectome(connectome_path)
    simulation = connectome.simulate(stimuli, ticks, synaptic_gain=synaptic_gain)
    result = {
        "tool": "FlyMerge",
        "version": 1,
        "input": {key: value for key, value in diff.items() if key != "changed_text"},
        "diff_features": matches,
        "stimuli": stimuli,
        "connectome": connectome.stats(),
        "simulation": simulation,
        "decision": decide(diff, simulation),
    }
    if compare_no_edges:
        without_edges = connectome.simulate(stimuli, ticks, edges_enabled=False, synaptic_gain=synaptic_gain)
        comparable = (
            "fired_neurons_total",
            "appetitive_channel_spikes",
            "aversive_channel_spikes",
            "motor_output_spikes",
            "downstream_fired_neurons_total",
            "readout_fired_neurons_total",
            "synaptic_events",
            "synaptic_abs_input",
        )
        contribution = {key: simulation[key] - without_edges[key] for key in comparable}
        without_edges_decision = decide(diff, without_edges)
        result["edge_ablation"] = {
            "without_edges": {key: without_edges[key] for key in comparable},
            "without_edges_decision": without_edges_decision,
            "edge_contribution": contribution,
            "connection_contributed": contribution["synaptic_events"] > 0,
            "connection_changed_spike_output": any(value != 0 for key, value in contribution.items() if key not in {"synaptic_events", "synaptic_abs_input"}),
            "decision_changed": result["decision"]["verdict"] != without_edges_decision["verdict"],
        }
    return result


def _safe(value: object) -> str:
    return html.escape(str(value), quote=True)


def markdown(result: dict, *, marker: bool = False, pr_number: int | None = None, run_url: str | None = None) -> str:
    decision = result["decision"]
    simulation = result["simulation"]
    verdict = str(decision["verdict"])
    badge = {"approve": "✅", "reject": "🚨", "hold": "🫥"}.get(verdict, "🪰")
    oracle = {
        "approve": "追い風。回路は前進を選んだ。",
        "reject": "警報。回路は危険側へ傾いた。",
        "hold": "沈黙。回路はまだ決められない。",
    }.get(verdict, "神託は不明。")
    lines = [
        *([COMMENT_MARKER, ""] if marker else []),
        f"## 🪰 FlyMerge verdict: `{_safe(verdict.upper())}` {badge}",
        "",
        f"> 🔮 神託: {_safe(oracle)}",
        "",
        f"**Confidence** `{_safe(decision['confidence'])}` · **Diff** `{_safe(result['input']['diff_sha256'][:12])}` · **Files** `{len(result['input']['files'])}` (+{result['input']['added_lines']}/-{result['input']['removed_lines']})",
        "",
        "| 🧠 Connectome | ⏱ Simulation |",
        "|---|---|",
        f"| `{result['connectome']['neuron_count']:,}` neurons · `{result['connectome']['edge_count']:,}` edges | `{simulation['ticks']}` ticks · gain `{simulation['synaptic_gain']}` |",
        "",
        "### Signal channels",
        "| Approach | Aversive | Motor |",
        "|---:|---:|---:|",
        f"| `{simulation['appetitive_channel_spikes']}` | `{simulation['aversive_channel_spikes']}` | `{simulation['motor_output_spikes']}` |",
    ]
    if "edge_ablation" in result:
        ablation = result["edge_ablation"]
        lines.extend(
            [
                "",
                "### 🕸️ Edge ablation",
                f"Real edges: `{simulation['readout_fired_neurons_total']:,}` readout spikes · `{simulation['synaptic_events']:,}` synaptic events · verdict `{_safe(verdict)}`",
                f"Without edges: `{ablation['without_edges']['readout_fired_neurons_total']:,}` readout spikes · verdict `{_safe(ablation['without_edges_decision']['verdict'])}` · decision changed `{ablation['decision_changed']}`",
            ]
        )
    details = [
        "",
        "<details>",
        "<summary>🧬 Neural details</summary>",
        "",
        f"- Fired neurons: `{simulation['fired_neurons_total']:,}`; downstream readout: `{simulation['readout_fired_neurons_total']:,}` in `{len(simulation['readout_groups'])}` groups.",
        f"- Reason: {_safe(decision['reason'])}",
    ]
    details.extend(["", "</details>"])
    lines.extend(details)
    if run_url and re.fullmatch(r"https://[^/\s]+/[^/\s]+/[^/\s]+/actions/runs/\d+", run_url):
        lines.extend(["", f"[View the FlyMerge Actions run]({_safe(run_url)})"])
    if pr_number is not None:
        lines.extend(["", f"_PR #{pr_number} · This is a playful heuristic, not a software-quality or scientific decision procedure._"])
    else:
        lines.extend(["", "_This is a playful heuristic, not a software-quality or scientific decision procedure._"])
    return "\n".join(lines) + "\n"


def github_request(
    url: str,
    token: str,
    method: str = "GET",
    payload: dict | None = None,
    accept: str = "application/vnd.github+json",
    opener=None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"GitHub API returned HTTP {error.code}: {detail}") from error


def fetch_pull_request(repo: str, number: int, token: str, opener=None) -> tuple[dict, str]:
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise ValueError("--repo must be OWNER/REPO")
    if number < 1:
        raise ValueError("--pr must be a positive pull request number")
    if not token:
        raise ValueError("a GitHub token is required")
    base_url = f"{GITHUB_API}/repos/{repo}/pulls/{number}"
    _status, metadata_raw = github_request(base_url, token, opener=opener)
    metadata = json.loads(metadata_raw.decode("utf-8"))
    if metadata.get("number", number) != number:
        raise RuntimeError("GitHub returned metadata for a different pull request")
    try:
        base_sha = metadata["base"]["sha"]
        head_sha = metadata["head"]["sha"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("GitHub PR metadata did not contain base.sha and head.sha") from error
    _status, diff_raw = github_request(
        base_url,
        token,
        accept="application/vnd.github.diff",
        opener=opener,
    )
    try:
        diff_text = diff_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("GitHub PR diff was not valid UTF-8") from error
    _status, metadata_after_raw = github_request(base_url, token, opener=opener)
    metadata_after = json.loads(metadata_after_raw.decode("utf-8"))
    if metadata_after.get("number", number) != number:
        raise RuntimeError("GitHub returned metadata for a different pull request after fetching the diff")
    try:
        base_after = metadata_after["base"]["sha"]
        head_after = metadata_after["head"]["sha"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("GitHub PR metadata after diff did not contain base.sha and head.sha") from error
    if base_after != base_sha or head_after != head_sha:
        raise RuntimeError("PR base/head SHA changed while fetching the diff")
    pull_request = {
        "number": metadata.get("number", number),
        "state": metadata.get("state"),
        "draft": bool(metadata.get("draft", False)),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "diff_sha256": hashlib.sha256(diff_raw).hexdigest(),
    }
    return pull_request, diff_text


def merge_pr(result: dict, repo: str, number: int, token: str, sha: str | None, pull_request: dict, opener=None) -> dict:
    if result["decision"]["verdict"] != "approve":
        raise RuntimeError("refusing merge because FlyMerge did not approve")
    if not sha:
        raise ValueError("a PR head SHA is required for merge")
    if pull_request.get("number") != number:
        raise RuntimeError("refusing merge because PR metadata number does not match the requested PR")
    if pull_request.get("head_sha") != sha:
        raise RuntimeError("refusing merge because evaluated head SHA does not match the current PR head")
    if result.get("input", {}).get("diff_sha256") != pull_request.get("diff_sha256"):
        raise RuntimeError("refusing merge because evaluated diff does not match the current PR diff")
    if pull_request.get("state") != "open":
        raise RuntimeError("refusing merge because the PR is not open")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise ValueError("--repo must be OWNER/REPO")
    if number < 1:
        raise ValueError("--pr must be a positive pull request number")
    if not token:
        raise ValueError("a GitHub token is required")
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}/merge"
    status, raw = github_request(
        url,
        token,
        method="PUT",
        payload={"sha": sha, "merge_method": "squash"},
        opener=opener,
    )
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("merged") is not True:
        raise RuntimeError(f"GitHub merge API did not merge the PR: {payload.get('message', 'merged=false')}")
    return {"status": status, "merged": True, "message": payload.get("message"), "sha": payload.get("sha")}


def self_test() -> None:
    parsed = parse_diff("diff --git a/tests/a.py b/tests/a.py\n+++ b/tests/a.py\n+assert True\n-unsafe old code\n")
    assert parsed["files"] == ["tests/a.py"]
    assert parsed["added_lines"] == 1 and parsed["removed_lines"] == 1
    stimuli, features = map_stimuli(parsed)
    assert any(item["group"] == "OLF_ORN_FOOD" for item in stimuli)
    assert any(item["feature"] == "verification" for item in features)
    assert decide({"files": [], "added_lines": 0, "removed_lines": 0}, {"fired_neurons_total": 0})["verdict"] == "hold"

    def write_fixture(path: Path, edges: list[tuple[int, int, float]]) -> None:
        neurons = 10
        raw = struct.pack("<II", neurons, len(edges))
        raw += b"".join(struct.pack("<IIf", pre, post, weight) for pre, post, weight in edges)
        raw += b"".join(struct.pack("<BH", 0, 0 if index < 8 else index - 7) for index in range(neurons))
        with gzip.open(path, "wb") as stream:
            stream.write(raw)

    with tempfile.TemporaryDirectory(prefix="flymerge-fixture-") as directory:
        root = Path(directory)
        fixture = root / "connectome.bin.gz"
        edges = [(0, 8, 10.0), (0, 9, -10.0)] + [(index, 8, 10.0) for index in range(1, 8)]
        write_fixture(fixture, edges)
        connectome = Connectome(fixture)
        stats = connectome.stats()
        assert stats["neuron_count"] == 10 and stats["edge_count"] == 9
        assert stats["edge_pre_sorted"] and stats["edge_post_indices_valid"] and stats["edge_weights_finite"]
        assert stats["csr_row_ptr_last"] == stats["edge_count"] and stats["csr_row_ptr_monotonic"]
        for invalid_gain in (float("nan"), float("inf"), float("-inf")):
            try:
                connectome.simulate([], ticks=1, synaptic_gain=invalid_gain)
            except ValueError:
                pass
            else:
                raise AssertionError("non-finite synaptic gain was accepted")
        simulation = connectome.simulate(
            [{"group": "VIS_R1R6", "intensity": 1.1}],
            ticks=6,
            pulse_ticks=1,
        )
        assert simulation["fired_neurons_by_tick"][0] == 8
        assert simulation["fired_neurons_by_tick"][1] == 1
        assert simulation["refractory_neurons_final"] == 0
        assert simulation["active_neurons_final_by_group"]["VIS_ME"] == 1
        assert "VIS_R1R6" not in simulation["readout_groups"]
        assert simulation["synaptic_events"] > 0
        without_edges = connectome.simulate(
            [{"group": "VIS_R1R6", "intensity": 1.1}],
            ticks=6,
            pulse_ticks=1,
            edges_enabled=False,
        )
        assert simulation["fired_neurons_total"] > without_edges["fired_neurons_total"]
        assert without_edges["synaptic_events"] == 0

        for malformed in (
            [(1, 8, 1.0), (0, 8, 1.0)],
            [(0, 10, 1.0)],
            [(0, 8, float("nan"))],
        ):
            malformed_path = root / f"bad-{len(malformed)}-{len(str(malformed))}.bin.gz"
            write_fixture(malformed_path, malformed)
            try:
                Connectome(malformed_path)
            except ValueError:
                pass
            else:
                raise AssertionError("malformed connectome was accepted")

        diff_text = "diff --git a/tests/a.py b/tests/a.py\n+++ b/tests/a.py\n+assert True\n"
        head_sha = "head123"
        base_sha = "base123"
        requests: list[tuple[str, str, bytes | None]] = []

        class MockResponse:
            def __init__(self, status: int, body: bytes):
                self.status = status
                self.body = body

            def read(self) -> bytes:
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_opener(request, timeout=30):
            del timeout
            requests.append((request.method, request.full_url, request.data))
            accept = request.get_header("Accept")
            if request.full_url.endswith("/pulls/7") and accept == "application/vnd.github+json":
                return MockResponse(200, json.dumps({
                    "number": 7,
                    "state": "open",
                    "draft": False,
                    "base": {"sha": base_sha},
                    "head": {"sha": head_sha},
                }).encode("utf-8"))
            if request.full_url.endswith("/pulls/7") and accept == "application/vnd.github.diff":
                return MockResponse(200, diff_text.encode("utf-8"))
            if request.full_url.endswith("/pulls/7/merge"):
                return MockResponse(200, b'{"merged": false, "message": "not allowed"}')
            raise AssertionError(f"unexpected mocked URL: {request.full_url}")

        pull_request, fetched_diff = fetch_pull_request("owner/repo", 7, "token", opener=fake_opener)
        assert pull_request["head_sha"] == head_sha and pull_request["base_sha"] == base_sha
        assert fetched_diff == diff_text
        assert not any(url.endswith(".diff") for _method, url, _data in requests)
        metadata_calls = 0

        def changing_opener(request, timeout=30):
            nonlocal metadata_calls
            del timeout
            accept = request.get_header("Accept")
            if accept == "application/vnd.github.diff":
                return MockResponse(200, diff_text.encode("utf-8"))
            metadata_calls += 1
            changed_head = head_sha if metadata_calls == 1 else "head-changed"
            return MockResponse(200, json.dumps({
                "number": 7,
                "state": "open",
                "base": {"sha": base_sha},
                "head": {"sha": changed_head},
            }).encode("utf-8"))

        try:
            fetch_pull_request("owner/repo", 7, "token", opener=changing_opener)
        except RuntimeError as error:
            assert "changed while fetching" in str(error)
        else:
            raise AssertionError("metadata race was accepted")
        good_result = {"decision": {"verdict": "approve"}, "input": {"diff_sha256": pull_request["diff_sha256"]}}
        try:
            merge_pr({"decision": {"verdict": "reject"}, "input": good_result["input"]}, "owner/repo", 7, "token", head_sha, pull_request, opener=fake_opener)
        except RuntimeError:
            pass
        else:
            raise AssertionError("rejected result reached merge gate")
        put_count = sum(method == "PUT" for method, _url, _data in requests)
        try:
            merge_pr(good_result, "owner/repo", 7, "token", "wrong", pull_request, opener=fake_opener)
        except RuntimeError:
            pass
        else:
            raise AssertionError("head mismatch reached merge endpoint")
        assert sum(method == "PUT" for method, _url, _data in requests) == put_count
        bad_diff_result = {"decision": {"verdict": "approve"}, "input": {"diff_sha256": "not-the-api-diff"}}
        try:
            merge_pr(bad_diff_result, "owner/repo", 7, "token", head_sha, pull_request, opener=fake_opener)
        except RuntimeError:
            pass
        else:
            raise AssertionError("diff mismatch reached merge endpoint")
        assert sum(method == "PUT" for method, _url, _data in requests) == put_count
        try:
            merge_pr(good_result, "owner/repo", 7, "token", head_sha, pull_request, opener=fake_opener)
        except RuntimeError as error:
            assert "did not merge" in str(error)
        else:
            raise AssertionError("merged=false was accepted")
        put_requests = [data for method, _url, data in requests if method == "PUT"]
        assert len(put_requests) == 1
        assert json.loads(put_requests[0].decode("utf-8"))["sha"] == head_sha

        report_result = {
            "decision": {"verdict": "approve", "confidence": 0.98, "reason": "renderer fixture"},
            "input": {"files": ["docs/demo.md"], "added_lines": 1, "removed_lines": 0, "diff_sha256": "a" * 64},
            "connectome": {"neuron_count": 10, "edge_count": 9},
            "simulation": {
                "ticks": 20, "synaptic_gain": 16.0, "fired_neurons_total": 4,
                "readout_fired_neurons_total": 3, "readout_groups": ["VIS_ME"],
                "synaptic_events": 8, "appetitive_channel_spikes": 4,
                "aversive_channel_spikes": 0, "motor_output_spikes": 2,
            },
            "edge_ablation": {
                "without_edges": {"readout_fired_neurons_total": 0},
                "without_edges_decision": {"verdict": "hold"},
                "decision_changed": True,
            },
        }
        report = markdown(report_result, marker=True, pr_number=7, run_url="https://github.com/owner/repo/actions/runs/1")
        def expect(condition, message):
            if not condition:
                raise RuntimeError(message)
        expect(COMMENT_MARKER in report, "marker missing")
        expect("## 🪰 FlyMerge verdict: `APPROVE`" in report, "verdict heading missing")
        expect("139,255" not in report and "10` neurons" in report, "fixture values missing")
        expect("Edge ablation" in report and "View the FlyMerge Actions run" in report, "report sections missing")
    print("self-test: PASS")


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge a PR diff with the local FlyWire-derived connectome")
    parser.add_argument("--diff", type=Path, help="unified diff file; stdin when omitted")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--gain", type=float, default=SYNAPTIC_GAIN, help="dimensionless synaptic readout gain")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--fail-on-reject", action="store_true")
    parser.add_argument("--merge", action="store_true", help="explicitly call GitHub's merge endpoint after an approve")
    parser.add_argument("--comment-markdown", action="store_true", help="render a marker for a GitHub PR report comment")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--pr", type=int, default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--sha", default=None)
    parser.add_argument("--run-url", default=None, help="Actions run URL to include in the PR report")
    parser.add_argument("--compare-no-edges", action="store_true", help="also run an explicit real-edge ablation")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.ticks < 1 or args.ticks > 200:
        parser.error("--ticks must be between 1 and 200")
    if not math.isfinite(args.gain) or args.gain <= 0:
        parser.error("--gain must be finite and positive")

    if args.merge:
        if not args.diff:
            parser.error("--merge requires --diff so the evaluated input is explicit")
        if not args.sha:
            parser.error("--merge requires --sha with the PR head SHA")
        repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
        token = args.token or os.environ.get("GITHUB_TOKEN")
        number = args.pr
        if not repo or not token or not number:
            parser.error("--merge requires --repo, --pr, and --token (or GITHUB_* environment variables)")
        pull_request, api_diff = fetch_pull_request(repo, number, token)
        diff_bytes = args.diff.read_bytes()
        try:
            diff_text = diff_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("refusing merge because --diff is not valid UTF-8") from error
        if diff_bytes != api_diff.encode("utf-8"):
            raise RuntimeError("refusing merge because --diff does not exactly match the current PR diff from GitHub")
    else:
        pull_request = None
        diff_text = args.diff.read_text(encoding="utf-8") if args.diff else sys.stdin.read()
    result = evaluate(
        diff_text,
        args.data_dir,
        args.ticks,
        compare_no_edges=args.compare_no_edges,
        synaptic_gain=args.gain,
    )
    if args.merge:
        result["pull_request"] = pull_request
        result["merge"] = merge_pr(result, repo, number, token, args.sha, pull_request)

    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(
            markdown(
                result,
                marker=args.comment_markdown,
                pr_number=args.pr if args.comment_markdown else None,
                run_url=args.run_url if args.comment_markdown else None,
            ),
            encoding="utf-8",
        )
    print(rendered, end="")
    return 2 if args.fail_on_reject and result["decision"]["verdict"] == "reject" else 0


if __name__ == "__main__":
    raise SystemExit(main())
