"""Repli local minimal pour une démo hors ligne si LangGraph n'est pas installé.

Ce module N'EST PAS LangGraph : sans checkpoints/conditions/parallélisme.
Quand langgraph est installé, l'orchestrateur utilise le véritable StateGraph.
"""
from __future__ import annotations

START = "__start__"
END = "__end__"


class StateGraph:
    def __init__(self, schema):
        self.handlers = {}
        self.edges = {}

    def add_node(self, name, handler):
        self.handlers[name] = handler

    def add_edge(self, start, end):
        self.edges[start] = end

    def compile(self):
        handlers = self.handlers.copy()
        edges = self.edges.copy()

        class Executable:
            def invoke(self, inputs):
                state = dict(inputs)
                current = edges[START]
                visited = set()
                while current != END:
                    if current in visited:
                        raise RuntimeError("Cycle interdit dans le graphe de secours")
                    visited.add(current)
                    state.update(handlers[current](state))
                    current = edges[current]
                return state
        return Executable()
