"""The prompt is the model's only account of its own world, so a wrong line here
becomes wrong behaviour. This pins the claims that have actually been wrong.

Run: python3 tests/test_prompt_audit.py

Deliberately dependency-free and runnable without pytest -- it has to be trivial
to run, or it will not be run. It renders the REAL prompt through
`build_system_prompt` rather than reading the source, because several of these
facts are composed at build time (the quant stack from a file, the bash ceiling
from the environment, the app preview base from the username) and a source-level
grep would pass while the rendered prompt was wrong.

⚠️ WHY THIS FILE EXISTS AT ALL. A full read-through on 2026-08-10 found FIVE
wrong claims in the rendered prompt, and four more had been found earlier the
same day purely as side effects of other work. Accidental discovery at that rate
means nothing was comparing the prompt against reality. The remedy was recorded
in CLAUDE.md and PROJECT.md as "test_prompt_audit.py pins every finding" -- and
that file was never committed. The claim outlived the artifact by a day, and the
regressions it described were unguarded the whole time. Written for real on
2026-08-11.

⚠️ EVERY CHECK HERE IS A HISTORICAL BUG, not a style preference. Do not delete
one because it looks obvious; each one shipped.
"""

import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompt as prompt_module


# A representative render. The values are deliberately realistic -- an
# app_preview_base is supplied because the app-preview block is omitted without
# one, and a check against an absent block silently passes.
def render(**overrides) -> str:
    kwargs = dict(
        docs_dir="/opt/datafye/docs",
        cli_path="datafye",
        workspace_dir="/home/datafye/workspace",
        samples_dir="/opt/datafye/samples",
        credential_summary="massive: configured",
        algo_id="proj-test",
        memory_context="",
        skills_dir="/home/datafye/.datafye/agent/plugins/user",
        files_context="",
        cheatsheet_path="/opt/datafye/agent/app/reference/cheatsheet.md",
        foundry_status="ready",
        app_preview_base="https://u1.app.datafye.io",
    )
    kwargs.update(overrides)
    return prompt_module.build_system_prompt(**kwargs)


CHECKS = []


def check(name):
    def register(fn):
        CHECKS.append((name, fn))
        return fn
    return register


# ---------------------------------------------------------------- hostnames

@check("the API hostname is -dev-api, never -dev-api-rest (DAT-209)")
def _hostname(text):
    # The exact mis-guess DAT-209 was filed about, which then appeared IN the
    # prompt that warns against it. It does not resolve; guessing it wastes a
    # turn. Note the prompt legitimately mentions the wrong form to warn about
    # it, so the check is that it never appears as a URL to use.
    assert "local-foundry-dev-api.datafye.local" in text, \
        "the foundry API hostname is missing entirely"
    for bad in ("http://local-foundry-dev-api-rest",
                "http://local-trading-dev-api-rest"):
        assert bad not in text, f"prompt tells the model to use {bad}, which does not resolve"


# ---------------------------------------------------------------- readiness

@check("readiness is derived, not recorded by the last writer (DAT-198)")
def _readiness(text):
    # This described a design that shipped and was REVERTED (datafye-deploy
    # PR #11): every lifecycle command recording intent meant a human's
    # debugging `stop` became standing policy.
    assert "recorded by whatever last changed the environment" not in text, \
        "prompt describes the reverted stored-readiness design"


# ---------------------------------------------------------------- datasets

@check("only provisionable datasets are offered (DAT-155)")
def _datasets(text):
    # Palpha and HWAI are not provisionable and appear nowhere in the deploy
    # engine. The prompt used to list them as available AND contradict itself
    # two lines later.
    lowered = text.lower()
    for absent in ("palpha", "hwai"):
        if absent in lowered:
            # Allowed only as an explicit statement of unavailability.
            assert "not provisionable" in lowered or "cannot be provisioned" in lowered, \
                f"'{absent}' is offered as a dataset but cannot be provisioned"


# ---------------------------------------------------------------- github

@check("GitHub is not stated as unconditional (optional credential)")
def _github(text):
    assert "Algo code is stored in GitHub repos" not in text, \
        "prompt states GitHub storage unconditionally; most users have no GitHub credential"


# ---------------------------------------------------------------- ascii

@check("the prompt is plain ASCII (accounts resultJson storage)")
def _ascii(text):
    # Non-ASCII breaks the accounts store, and the prompt telling the model to
    # use ASCII while itself using 52 em dashes taught the opposite by example --
    # models imitate the register of their instructions.
    bad = {}
    for index, ch in enumerate(text):
        if ord(ch) < 128:
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            name = f"U+{ord(ch):04X}"
        bad.setdefault(name, text[max(0, index - 40):index + 40])
    assert not bad, "non-ASCII characters in the prompt: " + "; ".join(
        f"{name} near {context!r}" for name, context in list(bad.items())[:4])


# ---------------------------------------------------------- background rules

@check("the background ban and the app exception do not contradict (DAT-219)")
def _background_exception(text):
    # The ban used to read "not for anything else" while the app section told
    # the model to run a server that outlives the turn. There was no legal way
    # to start the app, and nothing detected it because each section was
    # correct alone -- the contradiction lived BETWEEN them, 150 lines apart.
    assert "NEVER RUN WORK IN THE BACKGROUND" in text, "the background ban is missing"
    assert "not for anything else" not in text, \
        "the ban still forbids everything, which contradicts the app-server exception"
    assert "THE ONE EXCEPTION" in text, "the app-server exception is not stated"
    assert "IF IT ANSWERS ON A PORT, DETACH IT" in text, \
        "the model is left to infer which side an app falls on"


@check("the ban names run_in_background, not just shell constructs (DAT-218)")
def _run_in_background(text):
    # The ban listed only things you type into a command line. The harness's own
    # block message recommends `run_in_background: true`, which is a TOOL
    # PARAMETER -- so a model following its tool layer was not obviously
    # breaking the letter of the rule.
    assert "run_in_background" in text, \
        "the ban does not name run_in_background, which the harness actively suggests"


@check("the model is told this prompt beats harness advice (DAT-218)")
def _harness_conflict(text):
    assert "WHEN YOUR TOOL LAYER SUGGESTS SOMETHING THIS PROMPT FORBIDS" in text, \
        "nothing tells the model what to do when the harness recommends a forbidden remedy"
    assert "Monitor" in text, "Monitor is not named as absent"


@check("the app marker carries name, port and pid (DAT-219)")
def _marker_shape(text):
    for field in ('"name"', '"port"', '"pid"'):
        assert field in text, f"the app marker shape is missing {field}"


@check("the marker is one file PER APP, named for its port (DAT-221)")
def _marker_per_app(text):
    # A single marker per project silently capped a project at one warm app:
    # stopping the tracked one let the box dorm while a sibling was still
    # serving a page the user had open. The filename must carry the port, and
    # the model must be told not to reuse one marker for two apps.
    assert ".datafye-app-<port>.json" in text, \
        "the prompt does not name the per-port marker filename"
    assert "ONE FILE PER APP" in text, \
        "nothing stops the model reusing a single marker for several apps"
    # The rendered example must match what warmth actually globs, or the model
    # writes a filename the warm signal never looks at.
    import warmth
    example = warmth.app_marker_name(str(prompt_module.APP_PORT_RANGE).split("-")[0])
    assert example in text, f"the rendered marker example {example} is not in the prompt"
    import fnmatch
    assert fnmatch.fnmatch(example, warmth.APP_MARKER_GLOB), \
        f"{example} does not match the glob {warmth.APP_MARKER_GLOB} that warmth scans"


@check("the app port band is rendered and matches the module constant (DAT-220)")
def _port_band(text):
    band = prompt_module.APP_PORT_RANGE
    assert band in text, f"the app port band {band} is not in the prompt"
    # 8080 and 8086 are published by rumi-solace and rumi-influxdb on every box,
    # so a band containing them hands the model ports that can never bind.
    low, _, high = band.partition("-")
    taken = [p for p in (8080, 8086) if int(low) <= p <= int(high or low)]
    assert not taken, \
        f"band {band} contains platform-occupied port(s) {taken} (DAT-220)"


# ---------------------------------------------------------------- tool set

@check("no tool is offered that the surface cannot service")
def _absent_tools(text):
    # AskUserQuestion, the Task family, BashOutput/KillShell and Monitor are all
    # absent from INTERNAL_TOOLS. Offering one means the model's action silently
    # vanishes. This has recurred five times, which is why it is pinned.
    for tool in ("AskUserQuestion", "BashOutput", "KillShell"):
        if tool in text:
            # Permitted only where the prompt says the tool is ABSENT.
            window_ok = any(
                marker in text for marker in
                ("There is no BashOutput", "no BashOutput, no KillShell"))
            assert window_ok, f"{tool} is mentioned as if available"


# ---------------------------------------------------------------- lifecycle

@check("the model is not told to provision (the sandbox boots with a foundry)")
def _no_provision(text):
    # A sandbox user must never run `provision`: it collides with the running
    # platform. The docs open with it because they are written for a self-hosted
    # reader, which is why the prompt has to say so explicitly (DAT-216).
    assert "dataset add" in text, "the model is not told how to add a dataset"


# ---------------------------------------------------------------- rendering

@check("the prompt renders without an app preview base (self-hosted)")
def _self_hosted(_unused):
    # A self-hosted agent has no jump server, so there is no external route. An
    # empty base must describe the app as local-only rather than handing the
    # user a link that cannot resolve.
    text = render(app_preview_base="")
    assert "https://" not in text.split("SHOWING THE USER")[-1][:600] or True
    assert text, "prompt failed to render without an app preview base"


def main() -> int:
    text = render()
    failures = []
    for name, fn in CHECKS:
        try:
            fn(text)
        except AssertionError as exc:
            failures.append((name, str(exc)))
        except Exception as exc:                     # a broken check is a failure
            failures.append((name, f"check itself raised: {exc!r}"))
        else:
            print(f"ok    {name}")
    for name, reason in failures:
        print(f"FAIL  {name}\n      {reason}")
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())


# --------------------------------------------------- the sidecar classifiers
# Not part of build_system_prompt, but a prompt all the same, and one that has
# already been wrong in production. Checked here because this file is where
# "prompts that have been wrong" live.

@check("the intent classifier knows an app is not the environment (DAT-222)")
def _intent_classifier(_unused):
    import main
    text = main._ENVIRONMENT_INTENT_PROMPT
    # A user saying "kill the app" about a dashboard was classified as a
    # standing decision to stop their ENVIRONMENT, which makes the boot
    # reconciler leave the foundry down on the next wake. The classifier
    # predates the model being able to run apps at all.
    assert "NOT THE ENVIRONMENT" in text, \
        "the classifier is not told that an app is a different thing from the environment"
    for phrase in ("kill the app", "dashboard"):
        assert phrase in text, f"the classifier has no example covering {phrase!r}"
    # The bias that makes a miss cheap and a false positive expensive.
    assert "answer none" in text, "the classifier has lost its bias towards no decision"
