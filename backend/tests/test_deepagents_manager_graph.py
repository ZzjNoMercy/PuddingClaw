"""Tests for LangGraph structure extraction in DeepAgentsAgentManager."""

from graph.deepagents_manager import DeepAgentsAgentManager


class _FakeGraph:
    def __init__(self, nodes, edges):
        self.nodes = nodes
        self.edges = edges


class _FakeAgent:
    def __init__(self, graph):
        self._graph = graph

    def get_graph(self):
        return self._graph


class _FakeXrayAgent:
    def __init__(self, graph):
        self._graph = graph
        self.xray = None

    def get_graph(self, xray=False):
        self.xray = xray
        return self._graph


def test_graph_structure_with_tuple_nodes_and_edges():
    manager = DeepAgentsAgentManager()
    graph = _FakeGraph(
        nodes=[("__start__", {"type": "start"}), ("tools", {"type": "tool"}), ("model", None)],
        edges=[("__start__", "model"), ("model", "tools"), ("tools", "model")],
    )
    structure = manager._graph_structure(_FakeAgent(graph))
    assert structure is not None
    assert {n["id"] for n in structure["nodes"]} == {"__start__", "tools", "model"}
    assert structure["edges"] == [
        {"source": "__start__", "target": "model"},
        {"source": "model", "target": "tools"},
        {"source": "tools", "target": "model"},
    ]


def test_graph_structure_with_dict_nodes_and_edges():
    manager = DeepAgentsAgentManager()
    graph = _FakeGraph(
        nodes=[
            {"id": "a", "type": "start"},
            {"id": "b", "type": "normal"},
        ],
        edges=[
            {"source": "a", "target": "b"},
        ],
    )
    structure = manager._graph_structure(_FakeAgent(graph))
    assert structure is not None
    assert {n["id"] for n in structure["nodes"]} == {"a", "b"}
    assert structure["edges"] == [{"source": "a", "target": "b"}]


def test_graph_structure_prefers_xray_and_mermaid_payload():
    manager = DeepAgentsAgentManager()

    class MermaidGraph(_FakeGraph):
        def draw_mermaid(self):
            return "graph TD\n  a --> b"

        def draw_mermaid_png(self, **_kwargs):
            return b"png-bytes"

    agent = _FakeXrayAgent(
        MermaidGraph(
            nodes=[("a", None), ("b", None)],
            edges=[("a", "b")],
        )
    )
    structure = manager._graph_structure(agent)

    assert agent.xray is True
    assert structure is not None
    assert structure["mermaid"] == "graph TD\n  a --> b"
    assert structure["mermaid_png_data_url"] == "data:image/png;base64,cG5nLWJ5dGVz"


def test_graph_structure_graceful_when_get_graph_missing():
    manager = DeepAgentsAgentManager()
    assert manager._graph_structure(object()) is None


def test_graph_structure_graceful_when_agent_raises():
    manager = DeepAgentsAgentManager()

    class BadAgent:
        def get_graph(self):
            raise RuntimeError("no graph")

    assert manager._graph_structure(BadAgent()) is None
