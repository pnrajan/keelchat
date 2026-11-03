from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from yachat.api.app import create_app
from yachat.datasets.models import (
    Dataset,
    DatasetIn,
    StructuredDataset,
    StructuredRef,
    TableSource,
    UnstructuredDataset,
)
from yachat.datasets.sources import SourceRejected, check_uri
from yachat.policy.compiler import build
from yachat.registry.core import REGISTRY, CapabilityConfig, Node, NodeResult, NodeSpec, ToolSpec, _input_model_from_signature
from yachat.runtime.decide.tool_gateway import ToolGateway
from yachat.runtime.llm import ModelConfig
from yachat.runtime.llm_factory import llm_from_env
from yachat.runtime.runtime import Runtime
from yachat.state.store import InMemoryTenantToolStore

received: list[Dataset] = []


async def peek(question: str, dataset: Dataset) -> dict[str, Any]:
    """Test tool: records the dataset the gateway resolved."""
    received.append(dataset)
    return {"tables": [t.name for t in dataset.spec.tables]}  # type: ignore[union-attr]


class PeekConfig(CapabilityConfig):
    dataset: StructuredRef


class PeekNode(Node[PeekConfig]):
    config_model = PeekConfig

    async def act(self, ctx: Any, config: PeekConfig) -> NodeResult:
        res = await ctx.services.gateway.call("peek", {"question": ctx.text, "dataset": config.dataset})
        return NodeResult(str(res.data))


@pytest.fixture
def peek_capability(monkeypatch):
    spec = ToolSpec(name="peek", fn=peek, input_model=_input_model_from_signature("peek", peek), output_schema={}, dataset="structured")
    monkeypatch.setitem(REGISTRY._tools, "peek", spec)
    monkeypatch.setitem(REGISTRY._nodes, "peek_node", NodeSpec(name="peek_node", cls=PeekNode, config_model=PeekConfig, tools=("peek",)))
    received.clear()


def structured(*tables: str) -> DatasetIn:
    return DatasetIn(description="sales", spec=StructuredDataset(tables=[TableSource(name=t, uri=f"s3://data/{t}.csv") for t in tables]))


POLICY = """
agent: analyst
charter: Answer questions about sales data.
flow:
  - peek_node: {dataset: sales}
"""


def test_dataset_models():
    t = TableSource(name="orders", uri="/data/orders.PARQUET")
    assert t.format == "parquet"
    with pytest.raises(ValidationError, match="cannot infer the format"):
        TableSource(name="orders", uri="/data/orders.xlsx")
    with pytest.raises(ValidationError, match="duplicate table names"):
        StructuredDataset(tables=[TableSource(name="a", uri="a.csv"), TableSource(name="a", uri="b.csv")])
    with pytest.raises(ValidationError):
        TableSource(name="Orders; DROP", uri="a.csv")  # table names are SQL identifiers
    a, b = Dataset.create("t1", "sales", structured("orders")), Dataset.create("t1", "sales", structured("orders", "refunds"))
    assert a.kind == "structured" and a.ref == "t1/sales" and a.version != b.version
    assert Dataset.create("t2", "sales", structured("orders")).version == a.version  # content hash, not identity


def test_sources_must_stay_under_the_data_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.csv").write_text("a\n1\n")
    (tmp_path / "secret.csv").write_text("x\n")
    (root / "link.csv").symlink_to(tmp_path / "secret.csv")
    monkeypatch.delenv("YACHAT_DATA_ROOT", raising=False)
    with pytest.raises(SourceRejected, match="YACHAT_DATA_ROOT"):
        check_uri(str(root / "ok.csv"))
    monkeypatch.setenv("YACHAT_DATA_ROOT", str(root))
    check_uri(str(root / "ok.csv"))
    check_uri(f"file://{root}/ok.csv")
    for bad in (f"{root}/../secret.csv", str(root / "link.csv"), str(root / "missing.csv"), "https://example.com/a.csv"):
        with pytest.raises(SourceRejected):
            check_uri(bad)
    monkeypatch.setenv("YACHAT_DATA_BUCKETS", "data, other")
    check_uri("s3://data/orders.csv")
    with pytest.raises(SourceRejected, match="not in YACHAT_DATA_BUCKETS"):
        check_uri("s3://someone-else/orders.csv")


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("YACHAT_DATA_BUCKETS", "data")
    app = create_app(Runtime(llm_from_env(), models=ModelConfig.from_env()), tool_store=InMemoryTenantToolStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, app.state.runtime


async def test_dataset_api(client):
    c, _ = client
    body = structured("orders").model_dump(mode="json")
    r = await c.put("/v1/tenants/acme/datasets/sales", json=body)
    assert r.status_code == 200 and r.json()["spec"]["tables"][0]["format"] == "csv" and r.json()["tenant"] == "acme"
    v1 = r.json()["version"]
    assert (await c.put("/v1/tenants/acme/datasets/sales", json=structured("orders", "refunds").model_dump(mode="json"))).json()["version"] != v1
    assert [d["name"] for d in (await c.get("/v1/tenants/acme/datasets")).json()] == ["sales"]
    assert (await c.get("/v1/tenants/other/datasets")).json() == []  # tenants never see each other's datasets
    assert (await c.get("/v1/tenants/other/datasets/sales")).status_code == 404
    docs = {"spec": {"kind": "unstructured", "documents": [{"uri": "s3://data/a.txt"}]}}
    assert (await c.put("/v1/tenants/acme/datasets/sales", json=docs)).status_code == 409  # policies checked its kind
    assert (await c.put("/v1/tenants/acme/datasets/notes", json=docs)).status_code == 200
    bad = {"spec": {"kind": "structured", "tables": [{"name": "t", "uri": "s3://elsewhere/t.csv"}]}}
    assert (await c.put("/v1/tenants/acme/datasets/x", json=bad)).status_code == 422
    assert (await c.put("/v1/tenants/acme/datasets/Bad-Name", json=body)).status_code == 422
    assert (await c.delete("/v1/tenants/acme/datasets/sales")).json() == {"deleted": "sales"}
    assert (await c.delete("/v1/tenants/acme/datasets/sales")).status_code == 404


async def test_build_checks_dataset_references(client, peek_capability):
    c, _ = client
    r = await c.post("/v1/agents", json={"yaml": POLICY, "tenant": "acme"})
    assert r.status_code == 422 and r.json()["detail"][0]["path"] == "flow[0].peek_node.dataset" and "unregistered dataset" in r.json()["detail"][0]["message"]
    docs = {"spec": {"kind": "unstructured", "documents": [{"uri": "s3://data/a.txt"}]}}
    await c.put("/v1/tenants/acme/datasets/sales", json=docs)
    r = await c.post("/v1/agents", json={"yaml": POLICY, "tenant": "acme"})
    assert r.status_code == 422 and "needs a structured one" in r.json()["detail"][0]["message"]
    await c.delete("/v1/tenants/acme/datasets/sales")
    await c.put("/v1/tenants/acme/datasets/sales", json=structured("orders").model_dump(mode="json"))
    r = await c.post("/v1/agents", json={"yaml": POLICY, "tenant": "acme"})
    assert r.status_code == 201 and r.json()["datasets"] == ["sales"]
    assert (await c.post("/v1/agents", json={"yaml": POLICY, "tenant": "other"})).status_code == 422  # acme's dataset only


def test_offline_build_skips_the_catalog_and_hash_is_stable_without_datasets(peek_capability, policy_yaml):
    assert build(POLICY).datasets == ["sales"]
    assert build(policy_yaml).datasets == [] and build(policy_yaml).hash == build(policy_yaml, datasets={}).hash


async def test_gateway_resolves_only_the_policys_datasets(peek_capability):
    assert REGISTRY.tool("peek").input_schema["properties"]["dataset"]["type"] == "string"  # the model sees a name
    sales = Dataset.create("acme", "sales", structured("orders"))
    gw = ToolGateway({"peek"}, tenant="acme", datasets={"sales": sales})
    ok = await gw.call("peek", {"question": "q", "dataset": "sales"})
    assert ok.ok and ok.data == {"tables": ["orders"]} and received == [sales]
    for name in ("other", "Bad Name"):
        res = await gw.call("peek", {"question": "q", "dataset": name})
        assert not res.ok and res.error_kind in ("dataset_not_allowed", "invalid_args")
    notes = Dataset.create("acme", "notes", DatasetIn(spec=UnstructuredDataset(documents=[{"uri": "s3://data/a.txt"}])))  # type: ignore[list-item]
    wrong_kind = ToolGateway({"peek"}, tenant="acme", datasets={"notes": notes})
    assert (await wrong_kind.call("peek", {"question": "q", "dataset": "notes"})).error_kind == "dataset_not_allowed"


async def test_runtime_resolves_the_tenants_dataset_each_turn(peek_capability):
    rt = Runtime(llm_from_env(), models=ModelConfig.from_env())
    await rt.datasets.put(Dataset.create("acme", "sales", structured("orders")))
    await rt.datasets.put(Dataset.create("other", "sales", structured("secret")))
    policy = await rt.register_policy(build(POLICY, tenant="acme", datasets={"sales": "structured"}))
    state = await rt.start_session("analyst", "u1", tenant="acme")
    gw = await rt._gateway(policy, state)
    assert gw.datasets["sales"].ref == "acme/sales"
    await rt.datasets.put(Dataset.create("acme", "sales", structured("orders", "refunds")))  # new version applies next turn
    assert [t.name for t in (await rt._gateway(policy, state)).datasets["sales"].spec.tables] == ["orders", "refunds"]  # type: ignore[union-attr]


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_dataset_store():
    from yachat.state.postgres_store import PostgresDatasetStore

    store = PostgresDatasetStore(url=os.environ["DATABASE_URL"])
    tenant = f"t{os.getpid()}"
    await store.put(Dataset.create(tenant, "sales", structured("orders")))
    await store.put(Dataset.create(tenant, "sales", structured("orders", "refunds")))
    got = await store.get(tenant, "sales")
    assert got is not None and len(got.spec.tables) == 2 and await store.get("nobody", "sales") is None  # type: ignore[union-attr]
    assert [d.name for d in await store.list(tenant)] == ["sales"]
    assert await store.delete(tenant, "sales") and not await store.delete(tenant, "sales")


async def test_registration_is_checked_by_the_data_service_when_one_is_configured(client, monkeypatch, tmp_path):
    from yachat.analytics.analyst import Analyst
    from yachat.analytics.api import create_app as analytics_app
    from yachat.analytics.service import AnalyticsService

    c, _ = client
    (tmp_path / "orders.csv").write_text("region,amount\nEU,1\n")
    monkeypatch.setenv("YACHAT_DATA_ROOT", str(tmp_path))
    service = analytics_app(AnalyticsService(Analyst(llm_from_env(), ModelConfig.from_env())))
    real = httpx.AsyncClient

    def routed(*a, **kw):  # the runtime's call to ANALYTICS_URL reaches the real Analytics Service app in-process
        return real(transport=ASGITransport(app=service), base_url="http://analytics.internal", **{k: v for k, v in kw.items() if k != "base_url"})

    monkeypatch.setattr(httpx, "AsyncClient", routed)
    monkeypatch.setenv("ANALYTICS_URL", "http://analytics.internal")
    ok = {"spec": {"kind": "structured", "tables": [{"name": "orders", "uri": str(tmp_path / "orders.csv")}]}}
    assert (await c.put("/v1/tenants/acme/datasets/sales", json=ok)).status_code == 200
    outside = {"spec": {"kind": "structured", "tables": [{"name": "t", "uri": "/etc/hosts", "format": "csv"}]}}
    r = await c.put("/v1/tenants/acme/datasets/broken", json=outside)
    assert r.status_code == 422 and "outside YACHAT_DATA_ROOT" in r.json()["detail"]  # the service's reason
    (tmp_path / "bad.parquet").write_bytes(b"not parquet")
    bad = {"spec": {"kind": "structured", "tables": [{"name": "t", "uri": str(tmp_path / "bad.parquet")}]}}
    r = await c.put("/v1/tenants/acme/datasets/broken", json=bad)
    assert r.status_code == 422 and r.json()["detail"] == "source_unreadable"  # a category, never DuckDB's text
    assert (await c.get("/v1/tenants/acme/datasets/broken")).status_code == 404  # not stored


async def test_deleting_an_unstructured_dataset_purges_its_documents(client, monkeypatch, tmp_path):
    from yachat.knowledge.api import create_app as knowledge_app
    from yachat.knowledge.docstore import InMemoryDocumentStore
    from yachat.knowledge.embedder import FakeEmbedder
    from yachat.knowledge.service import KnowledgeService, make_indexer
    from yachat.knowledge.vectorstore import InMemoryIndex

    c, _ = client
    (tmp_path / "faq.md").write_text("Refunds take five days.")
    monkeypatch.setenv("YACHAT_DATA_ROOT", str(tmp_path))
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    service = knowledge_app(svc)
    real = httpx.AsyncClient

    def routed(*a, **kw):  # the runtime's calls to KNOWLEDGE_URL reach the real Knowledge Service app in-process
        return real(transport=ASGITransport(app=service), base_url="http://knowledge.internal", **{k: v for k, v in kw.items() if k != "base_url"})

    monkeypatch.setattr(httpx, "AsyncClient", routed)
    monkeypatch.setenv("KNOWLEDGE_URL", "http://knowledge.internal")
    body = {"spec": {"kind": "unstructured", "documents": [{"uri": str(tmp_path / "faq.md")}]}}
    assert (await c.put("/v1/tenants/acme/datasets/faq", json=body)).status_code == 200
    assert len(await svc.docs.list("acme/faq")) == 1 and await svc.index.count() >= 1
    assert (await c.delete("/v1/tenants/acme/datasets/faq")).status_code == 200
    assert await svc.docs.count() == 0 and await svc.index.count() == 0  # documents and vectors gone, not only the catalog row
    assert (await c.get("/v1/tenants/acme/datasets/faq")).status_code == 404


async def test_a_dataset_whose_data_cannot_be_purged_is_kept(client, monkeypatch, tmp_path):
    c, rt = client
    (tmp_path / "faq.md").write_text("Refunds take five days.")
    monkeypatch.setenv("YACHAT_DATA_ROOT", str(tmp_path))
    from yachat.datasets.models import Dataset, DatasetIn, DocumentSource, UnstructuredDataset

    await rt.datasets.put(Dataset.create("acme", "faq", DatasetIn(spec=UnstructuredDataset(documents=[DocumentSource(uri=str(tmp_path / "faq.md"))]))))
    monkeypatch.setenv("KNOWLEDGE_URL", "http://127.0.0.1:9")  # nothing listens: the purge fails
    r = await c.delete("/v1/tenants/acme/datasets/faq")
    assert r.status_code == 502 and "try again" in r.json()["detail"]
    assert (await c.get("/v1/tenants/acme/datasets/faq")).status_code == 200  # kept, so the delete can be retried
