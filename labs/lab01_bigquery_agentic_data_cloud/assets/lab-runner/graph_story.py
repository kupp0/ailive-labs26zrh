"""Graph story helpers for the lab consoles (Lab 10 and Lab 11 carry identical copies).

Standard library only, so the rules below are unit-tested without Google
libraries (tests/test_graph_story.py):

  * which seed customer ids the browser may send (only ones the server itself
    resolved as at-risk: the console URL is public);
  * whether the SQL and GQL rankings agree;
  * how query rows become the Cytoscape elements of the ego graph widget.

Nothing here builds SQL. Queries stay fixed text in actions.py, with the seed
customer passed as a query parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# Used with fullmatch: `$` alone would also accept a trailing newline.
CUSTOMER_ID_PATTERN = re.compile(r"[0-9]{1,12}")

# Size caps for the ego graph. Beyond ~40 nodes the rings stop being legible.
MAX_PEERS = 8
MAX_RECOMMENDATIONS = 5

# Concentric rings, innermost first: hop distance from the seed customer.
RING_SEED, RING_SHARED, RING_PEER, RING_REC, RING_CATEGORY = 0, 1, 2, 3, 4


def validate_customer_id(value: Any, allowed: Iterable[str]) -> str:
    """Returns `value` if it is a well-formed id the server resolved as at-risk.

    Args:
        value: The raw value from the request body.
        allowed: Customer ids the server resolved itself (the at-risk chips).

    Returns:
        The validated customer id.

    Raises:
        ValueError: If the value is not a string of digits, or is not one of
            the server-resolved at-risk customers.
    """
    if not isinstance(value, str) or not CUSTOMER_ID_PATTERN.fullmatch(value):
        raise ValueError("customer_id must be a numeric id; pick one of the at-risk chips")
    if value not in set(allowed):
        raise ValueError(f"customer {value} is not in the current at-risk list; reload the chips")
    return value


def ranking(rows: Sequence[Sequence[Any]], id_index: int = 0) -> list[str]:
    """Returns the ordered recommendation ids of a result, as strings."""
    return [str(row[id_index]) for row in rows]


def same_ranking(left: Sequence[Sequence[Any]], right: Sequence[Sequence[Any]], id_index: int = 0) -> bool:
    """True if both results rank the same ids in the same order (and are non-empty)."""
    a, b = ranking(left, id_index), ranking(right, id_index)
    return bool(a) and a == b


@dataclass(frozen=True)
class EgoEdge:
    """One row of the ego-graph query.

    Attributes:
        kind: "owns" (seed -> shared product), "peer_owns" (peer -> shared
            product), "peer_recs" (peer -> recommended product) or
            "in_category" (recommended product -> category).
        src: Source node id.
        src_name: Source display name.
        dst: Destination node id.
        dst_name: Destination display name.
        origin: Optional. The engines that hold this purchase, e.g. "Spanner" or
            "AlloyDB+BigQuery+Spanner" (Lab 11's unified graph). Empty when the
            graph has a single source (Lab 10); the edge then carries no origin.
    """

    kind: str
    src: str
    src_name: str
    dst: str
    dst_name: str
    origin: str = ""


_EDGE_LABEL = {"owns": "PURCHASED", "peer_owns": "PURCHASED", "peer_recs": "PURCHASED", "in_category": "IN_CATEGORY"}
_DST_RING = {"owns": RING_SHARED, "peer_owns": RING_SHARED, "peer_recs": RING_REC, "in_category": RING_CATEGORY}
_DST_LABEL = {"owns": "Product", "peer_owns": "Product", "peer_recs": "Product", "in_category": "Category"}


def _short(text: str, limit: int = 28) -> str:
    """Shortens a display name for a node label."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_ego_graph(seed_id: str, seed_name: str, edges: Iterable[EgoEdge]) -> dict[str, list[dict[str, Any]]]:
    """Builds Cytoscape elements for the seed customer's recommendation neighbourhood.

    Rings: 0 seed · 1 products the seed bought (that peers share) · 2 lookalike
    peers · 3 recommended products · 4 their categories. Node ids are prefixed
    by label, so a customer and a product with the same numeric id never merge.

    Args:
        seed_id: The seed customer id.
        seed_name: The seed customer's display name.
        edges: Rows of the ego-graph query.

    Returns:
        {"nodes": [...], "edges": [...]} in Cytoscape's element format.

    Raises:
        ValueError: On an unknown edge kind.
    """
    nodes: dict[str, dict[str, Any]] = {}

    def add_node(node_id: str, label: str, name: str, ring: int) -> None:
        # A node reached at two distances keeps the inner ring.
        if node_id not in nodes or nodes[node_id]["ring"] > ring:
            nodes[node_id] = {"id": node_id, "label": label, "name": _short(name), "full": name, "ring": ring}

    seed_node = f"c:{seed_id}"
    add_node(seed_node, "Customer", seed_name or seed_id, RING_SEED)

    edge_list: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    shared_products: set[str] = set()
    for e in edges:
        if e.kind not in _EDGE_LABEL:
            raise ValueError(f"unknown ego edge kind {e.kind!r}")
        if e.kind == "owns":
            src = seed_node
        elif e.kind == "in_category":
            src = f"p:{e.src}"
        else:
            src = f"c:{e.src}"
            add_node(src, "Customer", e.src_name or e.src, RING_PEER)
        dst = (f"k:{e.dst}" if e.kind == "in_category" else f"p:{e.dst}")
        add_node(dst, _DST_LABEL[e.kind], e.dst_name or e.dst, _DST_RING[e.kind])
        if e.kind in ("owns", "peer_owns"):
            shared_products.add(dst)
        if (src, dst) in seen:
            # Same pair again: shared-product evidence outranks a recommendation.
            if e.kind in ("owns", "peer_owns"):
                seen[(src, dst)]["kind"] = e.kind
            continue
        edge = {"id": f"{src}>{dst}", "source": src, "target": dst, "label": _EDGE_LABEL[e.kind], "kind": e.kind}
        if e.origin:
            edge["origin"] = e.origin
        seen[(src, dst)] = edge
        edge_list.append(edge)

    # Products the seed owns that no displayed peer shares add noise, not evidence.
    peer_shared = {ed["target"] for ed in edge_list if ed["kind"] == "peer_owns"}
    drop = {n for n in shared_products if n not in peer_shared}
    edge_list = [ed for ed in edge_list if ed["target"] not in drop]
    for n in drop:
        nodes.pop(n, None)

    return {"nodes": list(nodes.values()), "edges": edge_list}


def schema_graph() -> dict[str, list[dict[str, Any]]]:
    """Returns the three node labels and two edge labels as a tiny graph (Card 1)."""
    return {
        "nodes": [
            {"id": "Customer", "label": "Customer", "name": "Customer", "full": "graph_customers", "ring": 0},
            {"id": "Product", "label": "Product", "name": "Product", "full": "graph_products", "ring": 1},
            {"id": "Category", "label": "Category", "name": "Category", "full": "graph_categories", "ring": 2},
        ],
        "edges": [
            {"id": "PURCHASED", "source": "Customer", "target": "Product", "label": "PURCHASED", "kind": "schema"},
            {"id": "IN_CATEGORY", "source": "Product", "target": "Category", "label": "IN_CATEGORY", "kind": "schema"},
        ],
    }
