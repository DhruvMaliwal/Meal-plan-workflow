"""Anthropic API wrapper with a mock/offline mode.

Everything that needs judgement goes through here:
  * extract_inventory(images)    -> vision -> InventoryExtraction
  * extract_last_plan(images)    -> vision -> LastPlanExtraction
  * infer_attributes(batch)      -> AttributeBatch
  * assemble_plan(payload)       -> plan JSON (uses the Epicure MCP connector server-side)

Prompts are plain files in /prompts so the operator can tune them.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Type, TypeVar

from pydantic import BaseModel, Field

from .config import load_config

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Structured-output schemas
# ---------------------------------------------------------------------------
class InventoryItem(BaseModel):
    item: str
    canonical: str = ""
    category: str = "Staple"
    quantity: float | None = None
    unit: str = ""
    notes: str = ""


class InventoryExtraction(BaseModel):
    items: list[InventoryItem]
    warnings: list[str] = Field(default_factory=list)


class PlanEntry(BaseModel):
    day: str
    meal_slot: str
    dish: str


class FormatTemplate(BaseModel):
    orientation: str = "days_as_rows"
    day_label_style: str = "Weekday name"
    slot_headers: list[str] = Field(default_factory=lambda: ["Breakfast", "Lunch", "Dinner"])
    dish_separator: str = " + "
    includes_links: bool = False
    includes_notes_column: bool = False
    extra_columns: list[str] = Field(default_factory=list)
    style_notes: str = ""


class LastPlanExtraction(BaseModel):
    entries: list[PlanEntry]
    format_template: FormatTemplate = Field(default_factory=FormatTemplate)
    warnings: list[str] = Field(default_factory=list)


class DishAttributeGuess(BaseModel):
    dish: str
    slots: list[str]
    component: str
    cuisine: str
    gravy: bool
    est_minutes: int
    confidence: float = 0.6
    notes: str = ""


class AttributeBatch(BaseModel):
    dishes: list[DishAttributeGuess]


class SlotChoice(BaseModel):
    day: int
    slot: str
    dishes: list[str]
    rationale: str = ""


class PlanChoice(BaseModel):
    slots: list[SlotChoice]
    tradeoffs: str = ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
def _prompt(name: str) -> str:
    return (load_config().path("prompts") / f"{name}.md").read_text(encoding="utf-8")


def _image_block(img: bytes | str | Path) -> dict:
    if isinstance(img, (str, Path)):
        p = Path(img)
        data = p.read_bytes()
        mt = mimetypes.guess_type(p.name)[0] or "image/png"
    else:
        data = img
        mt = "image/png"
        if data[:3] == b"\xff\xd8\xff":
            mt = "image/jpeg"
        elif data[:4] == b"RIFF":
            mt = "image/webp"
        elif data[:3] == b"GIF":
            mt = "image/gif"
    return {"type": "image", "source": {"type": "base64", "media_type": mt,
                                        "data": base64.standard_b64encode(data).decode("ascii")}}


def _extract_json(text: str) -> Any:
    """Parse the first JSON object/array in a text blob."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [0])
    depth, end = 0, None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise ValueError("no JSON found in model output")
    return json.loads(text[start:end + 1])


class LLM:
    def __init__(self, mock: bool | None = None):
        cfg = load_config()
        self.cfg = cfg
        self.mock = cfg["llm"]["mock"] if mock is None else mock
        self.model = cfg["llm"]["model"]
        self.vision_model = cfg["llm"].get("vision_model", self.model)
        self.max_tokens = int(cfg["llm"].get("max_tokens", 16000))
        self.effort = cfg["llm"].get("effort", "high")
        self.use_fallbacks = bool(cfg["llm"].get("server_side_fallbacks", True))
        self.last_usage: dict[str, Any] = {}
        self.calls: list[dict] = []          # audit trail shown in the UI
        self._client = None

    # ---- plumbing --------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.mock or bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def _record(self, kind: str, resp: Any, note: str = "") -> None:
        usage = getattr(resp, "usage", None)
        u = {"input": getattr(usage, "input_tokens", None), "output": getattr(usage, "output_tokens", None)}
        self.last_usage = u
        self.calls.append({"kind": kind, "model": getattr(resp, "model", None), **u,
                           "stop": getattr(resp, "stop_reason", None), "note": note})

    def _beta_kwargs(self, extra_betas: list[str] | None = None) -> dict:
        betas = list(extra_betas or [])
        kw: dict[str, Any] = {}
        if self.use_fallbacks:
            betas.append("server-side-fallback-2026-07-01")
            kw["fallbacks"] = "default"
        if betas:
            kw["betas"] = betas
        return kw

    def _structured(self, schema: Type[T], system: str, content: list[dict] | str, model: str | None = None,
                    kind: str = "call") -> T:
        """Structured call with graceful degradation.

        1. beta.messages.parse(output_format=schema) with server-side fallbacks
        2. same without fallbacks (if the org/model rejects that beta)
        3. plain create + 'return JSON' instruction, parsed manually
        """
        import anthropic
        client = self.client()
        model = model or self.model
        messages = [{"role": "user", "content": content}]
        attempts = [self._beta_kwargs(), {}]
        last_err: Exception | None = None
        for kw in attempts:
            try:
                resp = client.beta.messages.parse(
                    model=model, max_tokens=self.max_tokens, system=system, messages=messages,
                    output_format=schema, output_config={"effort": self.effort}, **kw)
                self._record(kind, resp)
                if resp.stop_reason == "refusal":
                    raise RuntimeError(f"model refused ({kind})")
                if resp.parsed_output is not None:
                    return resp.parsed_output
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                return schema.model_validate(_extract_json(text))
            except anthropic.BadRequestError as e:
                last_err = e
                continue
        # 3. plain text fallback
        sys2 = system + "\n\nReturn ONLY a JSON object that validates against this JSON schema:\n" + json.dumps(schema.model_json_schema())
        resp = client.messages.create(model=model, max_tokens=self.max_tokens, system=sys2, messages=messages)
        self._record(kind, resp, note=f"plain-json fallback ({last_err})")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return schema.model_validate(_extract_json(text))

    # ---- tasks -------------------------------------------------------------
    def extract_inventory(self, images: list[bytes | str | Path]) -> InventoryExtraction:
        if self.mock:
            return self._mock_inventory()
        content = [_image_block(i) for i in images] + [{"type": "text", "text": "Extract the inventory from these image(s)."}]
        return self._structured(InventoryExtraction, _prompt("extract_inventory"), content, self.vision_model, "extract_inventory")

    def extract_last_plan(self, images: list[bytes | str | Path]) -> LastPlanExtraction:
        if self.mock:
            return self._mock_last_plan()
        content = [_image_block(i) for i in images] + [{"type": "text", "text": "Extract the meal plan and its layout from these image(s)."}]
        return self._structured(LastPlanExtraction, _prompt("extract_last_plan"), content, self.vision_model, "extract_last_plan")

    def infer_attributes(self, dishes: list[dict]) -> AttributeBatch:
        """dishes: [{dish, ingredients:[{name, class}], flags}] ."""
        if self.mock:
            raise RuntimeError("infer_attributes is not available in mock mode (heuristics are used instead)")
        lines = []
        for d in dishes:
            ing = ", ".join(f"{i['name']}({i['class'][0]})" for i in d["ingredients"])
            flags = ", ".join(k for k, v in d.get("flags", {}).items() if v)
            lines.append(f"- {d['dish']} :: {ing}" + (f" :: flags: {flags}" if flags else ""))
        content = "Ingredient roles: B=Base, H=Hero, O=Optional/Base-Optional, F=Fat, G=Garnish.\n\n" + "\n".join(lines)
        return self._structured(AttributeBatch, _prompt("infer_attributes"), content, self.model, "infer_attributes")

    def assemble_plan(self, prompt_vars: dict, schema_hint: dict) -> tuple[PlanChoice, str]:
        """Assembly with the Epicure MCP connector (server-side). Returns (choice, raw_text)."""
        if self.mock:
            raise RuntimeError("assemble_plan is not available in mock mode (heuristic assembler is used)")
        import anthropic
        client = self.client()
        system = _prompt("assemble_plan").format(**prompt_vars)
        user = "Assemble the plan now. Output only the JSON object."
        epi = self.cfg["epicure_mcp"]
        fmt = {"type": "json_schema", "schema": schema_hint}
        variants: list[dict] = []
        if epi.get("enabled", True):
            mcp_kw = dict(mcp_servers=[{"type": "url", "url": epi["url"], "name": epi["name"]}],
                          tools=[{"type": "mcp_toolset", "mcp_server_name": epi["name"]}])
            variants.append({**self._beta_kwargs(["mcp-client-2025-11-20"]), **mcp_kw, "output_config": {"effort": self.effort, "format": fmt}})
            variants.append({"betas": ["mcp-client-2025-11-20"], **mcp_kw, "output_config": {"effort": self.effort}})
        variants.append({**self._beta_kwargs(), "output_config": {"effort": self.effort, "format": fmt}})
        variants.append({"output_config": {"effort": self.effort}})
        last_err: Exception | None = None
        for kw in variants:
            try:
                with client.beta.messages.stream(model=self.model, max_tokens=self.max_tokens, system=system,
                                                 messages=[{"role": "user", "content": user}], **kw) as stream:
                    resp = stream.get_final_message()
                note = "with epicure" if "mcp_servers" in kw else "no tools"
                self._record("assemble_plan", resp, note=note)
                if resp.stop_reason == "refusal":
                    raise RuntimeError("model refused to assemble the plan")
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                tool_calls = [b for b in resp.content if getattr(b, "type", "") in ("mcp_tool_use", "server_tool_use")]
                self.calls[-1]["epicure_calls"] = len(tool_calls)
                return PlanChoice.model_validate(_extract_json(text)), text
            except (anthropic.BadRequestError, anthropic.APIStatusError) as e:
                last_err = e
                continue
        raise RuntimeError(f"assembly failed: {last_err}")

    # ---- mock data ---------------------------------------------------------
    def _samples(self) -> Path:
        return self.cfg.path("samples")

    def _mock_inventory(self) -> InventoryExtraction:
        import csv
        p = self._samples() / "sample_inventory.csv"
        items = []
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                q = r.get("quantity")
                items.append(InventoryItem(item=r["item"], canonical=r.get("canonical", ""), category=r.get("category", "Staple"),
                                           quantity=float(q) if q not in (None, "") else None, unit=r.get("unit", ""), notes=r.get("notes", "")))
        return InventoryExtraction(items=items, warnings=["MOCK MODE: inventory loaded from data/samples/sample_inventory.csv, not from your image."])

    def _mock_last_plan(self) -> LastPlanExtraction:
        p = self._samples() / "sample_last_plan.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        out = LastPlanExtraction.model_validate(data)
        out.warnings.append("MOCK MODE: last plan loaded from data/samples/sample_last_plan.json, not from your image.")
        return out
