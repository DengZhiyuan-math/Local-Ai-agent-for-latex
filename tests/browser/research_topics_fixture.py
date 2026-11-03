"""Extend the isolated research fixture with deterministic topic save failures."""
from pathlib import Path
import runpy
import sys


work = Path(sys.argv[1]).resolve()
repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / "prism_local"))
import httpbase
import registry

if "--state-dir" in sys.argv:
    shared_state = Path(sys.argv[sys.argv.index("--state-dir") + 1]).resolve()
    registry.state_dir = lambda: shared_state

original_listen = httpbase.listen


def listen(*args, **kwargs):
    import server
    first = server.AGENT.backends["fixture"]
    server.AGENT.backends["fixture-2"] = type(first)("fixture-2", {
        "base_url": first.base_url, "models": ["fixture-model", "fixture-model-2"],
        "default_model": "fixture-model"})
    return original_listen(*args, **kwargs)


httpbase.listen = listen
import research_topics
import research

original_topic_replace = research_topics.replace_file
original_research_replace = research.replace_file


def should_fail(path):
    path = Path(path)
    if (work / "fail-topic-markdown").exists() and path.suffix == ".md":
        return True
    if path.name == "knowledge.json":
        if (work / "fail-topic-index").exists():
            return True
        if ((work / "fail-topic-final-index").exists()
                and any((work / "project/research/topics").glob("*.md"))):
            return True
    return False


def topic_replace(path, data, **kwargs):
    if should_fail(path):
        raise OSError("fixture topic persistence unavailable")
    return original_topic_replace(path, data, **kwargs)


def research_replace(path, data, **kwargs):
    if should_fail(path):
        raise OSError("fixture topic index persistence unavailable")
    return original_research_replace(path, data, **kwargs)


research_topics.replace_file = topic_replace
research.replace_file = research_replace

runpy.run_path(str(Path(__file__).with_name("research_fixture.py")), run_name="__main__")
