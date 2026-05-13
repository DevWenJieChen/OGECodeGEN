from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import requests
import math
from pydantic import BaseModel, Field

from src.core.pipeline_state import PipelineState, KnowledgeDoc
from src.tools.model_clients import ChatLLMClient
from src.tools import prompt_loader

# ======================
# Local knowledge base (JSON)
# ======================

KB_DIR = Path("data_json")

PRODUCT_INFO: List[dict] = json.loads((KB_DIR / "product_info.json").read_text(encoding="utf-8"))
PRODUCT_KEYWORD: List[dict] = json.loads((KB_DIR / "product_keyword.json").read_text(encoding="utf-8"))
SCENES_PRODUCT_INFO: List[dict] = json.loads((KB_DIR / "scenes_product_info.json").read_text(encoding="utf-8"))


class DataRecommendation(BaseModel):
    sample_data_text: Optional[str] = None
    collection_data_text: Optional[str] = None
    bands: Optional[str] = None
    product_info: Optional[str] = None


class RetrievalDataOutput(BaseModel):
    task_bbox: Optional[List[float]] = None
    recommendations: List[DataRecommendation] = Field(default_factory=list)
    en_info: Optional[List[DataRecommendation]] = None

# ============================================================
# Lightweight similarity matching: prefer rapidfuzz, fall back to difflib
# ============================================================

def similarity(a: str, b: str) -> float:
    """
        Return a similarity score from 0 to 100.
        - Prefer rapidfuzz, which is recommended for speed and quality.
        - Fall back to difflib, which has no external dependency.
    """
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz
        return float(fuzz.WRatio(a, b))
    except Exception:
        import difflib
        return difflib.SequenceMatcher(a=a, b=b).ratio() * 100


# ======================
# Optional: AMap bbox
# ======================

def amap_get_bbox(region_text: str, amap_key: str) -> Optional[List[float]]:
    """
    Return a bbox from region text: min_lon, min_lat, max_lon, max_lat.
    This is only used to provide spatial-range context to the LLM and downstream modules.
    Strategy:
    1) Prefer the AMap administrative district API /v3/config/district and compute the task bbox from its polyline.
    2) If no administrative district or polyline is found, fall back to geocode/geo to obtain a center point,
       then expand the bbox with an estimated radius based on administrative level.
    """
    if not region_text or not amap_key:
        raise ValueError("AMap api_key is required")

    # ---------- Administrative district API query ----------
    try:
        district_url = "https://restapi.amap.com/v3/config/district"
        district_params = {
            "keywords": region_text,
            "subdistrict": 0,
            "extensions": "all",
            "key": amap_key,
            "output": "JSON",
        }
        r = requests.get(district_url, params=district_params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "1":
            districts = data.get("districts") or []
            if districts:
                polyline = districts[0].get("polyline")
                if polyline:
                    min_lon = min_lat = float("inf")
                    max_lon = max_lat = float("-inf")

                    # polyline: lon,lat;lon,lat|lon,lat;...
                    for block in polyline.split("|"):
                        for pt in block.split(";"):
                            if not pt:
                                continue
                            lon_s, lat_s = pt.split(",")
                            lon = float(lon_s)
                            lat = float(lat_s)
                            min_lon = min(min_lon, lon)
                            min_lat = min(min_lat, lat)
                            max_lon = max(max_lon, lon)
                            max_lat = max(max_lat, lat)

                    if min_lon < max_lon and min_lat < max_lat:
                        return [min_lon, min_lat, max_lon, max_lat]
    except Exception:
        # It is normal for the district lookup to fail; fall back directly.
        pass
    # ---------- geocode + radius expansion fallback ----------
    try:
        geo_url = "https://restapi.amap.com/v3/geocode/geo"
        geo_params = {
            "key": amap_key,
            "address": region_text,
            "output": "JSON",
        }
        r = requests.get(geo_url, params=geo_params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "1" and data.get("geocodes"):
            loc = data["geocodes"][0].get("location")
            if loc:
                lon, lat = map(float, loc.split(","))
                # Simple heuristic; avoid complex rules.
                if "省" in region_text or "自治区" in region_text or "直辖市" in region_text:
                    radius_km = 300.0
                elif "市" in region_text:
                    radius_km = 80.0
                elif "区" in region_text or "县" in region_text:
                    radius_km = 30.0
                else:
                    radius_km = 50.0  # Default fallback
                # Approximate km-to-degree conversion: 1 degree latitude ~= 110.574 km; 1 degree longitude ~= 111.320 * cos(lat) km
                dlat = radius_km / 110.574
                dlon = radius_km / (111.320 * max(math.cos(math.radians(lat)), 1e-6))
                return [lon - dlon,lat - dlat,lon + dlon,lat + dlat]

    except Exception:
        pass

    return None


# ======================
# Keyword matching: data_constraints <-> product_keyword.keyword
# ======================

def match_product_ids_by_keyword(
    data_constraints: Optional[str],
    threshold: float,
) -> Dict[str, object]:
    """
    Match data_constraints against product_keyword.keyword by similarity to obtain candidate product_ids.
    Minimal enhanced version:
    1) Prefer direct keyword hits: containment receives a high score.
    2) Use partial_ratio + WRatio.
    3) Include semantic_desc in matching.
    """
    text = (data_constraints or "").strip()
    product_ids: Set[int] = set()
    hits: List[dict] = []

    if not text:
        return {"product_ids": product_ids, "hits": hits}

    text_lower = text.lower()

    for rec in PRODUCT_KEYWORD:
        kw = str(rec.get("keyword", "")).strip()
        semantic_desc = str(rec.get("semantic_desc", "")).strip()

        if not kw:
            continue

        kw_lower = kw.lower()
        semantic_desc_lower = semantic_desc.lower()

        # Strategy 2: prefer containment hits, then partial_ratio / WRatio.
        kw_score = 0.0
        desc_score = 0.0

        try:
            from rapidfuzz import fuzz

            if kw_lower in text_lower:
                kw_score = 100.0
            else:
                kw_score = max(
                    float(fuzz.partial_ratio(text_lower, kw_lower)),
                    float(fuzz.WRatio(text_lower, kw_lower)),
                )

            if semantic_desc_lower:
                desc_score = max(
                    float(fuzz.partial_ratio(text_lower, semantic_desc_lower)),
                    float(fuzz.WRatio(text_lower, semantic_desc_lower)),
                )

        except Exception:
            # Fallback when rapidfuzz is unavailable.
            if kw_lower in text_lower:
                kw_score = 100.0
            else:
                kw_score = similarity(text_lower, kw_lower)

            if semantic_desc_lower:
                desc_score = similarity(text_lower, semantic_desc_lower)

        # Strategy 3: take the higher score between keyword and semantic_desc.
        score = max(kw_score, desc_score)

        if score >= threshold:
            hits.append({
                "keyword": kw,
                "score": score,
                "product_ids": rec.get("product_ids", []),
                "semantic_desc": semantic_desc,
            })

            ids = rec.get("product_ids", [])
            if isinstance(ids, list):
                for x in ids:
                    if isinstance(x, int):
                        product_ids.add(x)
                    elif isinstance(x, str) and x.strip().isdigit():
                        product_ids.add(int(x.strip()))

    hits.sort(key=lambda x: x["score"], reverse=True)
    return {"product_ids": product_ids, "hits": hits}

def get_products_by_ids(product_ids: Set[int]) -> List[dict]:
    """Retrieve corresponding product information from product_info."""
    if not product_ids:
        return []
    out: List[dict] = []
    for p in PRODUCT_INFO:
        pid = p.get("product_id")
        if isinstance(pid, int) and pid in product_ids:
            out.append(p)
    return out

def collect_scenes_by_products(products: List[dict]) -> Dict[str, List[dict]]:
    """
    The key in scenes_product_info.json is product_name, such as LC08_C02_L1.
    Therefore, retrieve the scene list by product_info.name here.
    By default, this uses information from the first images.
    """
    result: Dict[str, List[dict]] = {}
    for p in products:
        name = p.get("name")
        if isinstance(name, str) and name.strip():
            result[name.strip()] = SCENES_PRODUCT_INFO.get(name.strip(), [])[0:2]
    return result

def _parse_intent_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

_JSON_DECODER = json.JSONDecoder()

def _normalize_json_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return text

    m = re.search(r"```(?:json|python|py)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        text = m.group(1).strip()

    text = re.sub(r'\\u(?![0-9a-fA-F]{4})', r'\\\\u', text)
    text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)
    return text.strip()

def _extract_first_json_value(text: str) -> Optional[str]:
    if not text:
        return None
    for m in re.finditer(r'[\{\[]', text):
        start = m.start()
        try:
            _, end = _JSON_DECODER.raw_decode(text[start:])
            return text[start:start + end]
        except Exception:
            continue
    return None

def _safe_json_object_from_llm(raw: str, dump_prefix: str = "llm_json") -> dict:
    raw = "" if raw is None else str(raw)

    debug_dir = Path("debug_llm")
    debug_dir.mkdir(parents=True, exist_ok=True)
    dump_path = debug_dir / f"{dump_prefix}_{uuid.uuid4().hex}.txt"
    dump_path.write_text(raw, encoding="utf-8")

    text = _normalize_json_text(raw)
    if not text:
        raise RuntimeError(f"{dump_prefix}: empty LLM output, dumped to {dump_path}")

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    candidate = _extract_first_json_value(text)
    if candidate:
        candidate = _normalize_json_text(candidate)
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    raise RuntimeError(
        f"{dump_prefix}: non-JSON or unsupported JSON output, dumped to {dump_path}. "
        f"raw_head={text[:1000]!r}"
    )

# ======================
# Main entry point
# ======================
def run(pls: PipelineState, llm: ChatLLMClient, cfg: dict) -> PipelineState:
    # intent = json.loads(pls.intent_json or "{}")
    intent = _parse_intent_json(pls.intent_json or "")
    data_info = getattr(pls, "data_info", "") or ""
    # If there are no data constraints, retrieve according to the user query.
    # data_constraints: str = intent.get("data_constraints",None) if intent else pls.user_query
    raw_data_constraints = intent.get("data_constraints") if isinstance(intent, dict) else None
    if isinstance(raw_data_constraints, str) and raw_data_constraints.strip():
        data_constraints = raw_data_constraints.strip()
    else:
        data_constraints = pls.user_query

    # 1. Check explicit markers in data_constraints; only test whether productID and coverageID appear.
    has_product_id_mark = isinstance(data_constraints, str) and "productID" in data_constraints
    has_coverage_id_mark = isinstance(data_constraints, str) and "coverageID" in data_constraints

    # 2. Recall products through keyword similarity matching.
    threshold = float(cfg.get("retrieval", {}).get("product_similarity_min", 80))
    match_result = match_product_ids_by_keyword(data_constraints, threshold=threshold)
    product_ids: Set[int] = match_result["product_ids"]
    keyword_hits: List[dict] = match_result["hits"]

    # 3. Fetch candidate products from product_info.
    candidate_products = get_products_by_ids(product_ids)

    # 4. Decide whether scene context is needed.
    need_scene_context = not (has_product_id_mark and has_coverage_id_mark)
    candidate_scenes = collect_scenes_by_products(candidate_products) if need_scene_context else {}

    # 5. bbox, optional: space_region -> AMap bbox.
    task_bbox = None
    space_region = intent.get("space_region") if intent else None
    amap_key = cfg.get("amap", {}).get("api_key")
    if isinstance(space_region, str) and space_region.strip() and amap_key:
        try:
            task_bbox = amap_get_bbox(space_region.strip(), amap_key)
        except Exception:
            task_bbox = None

    # 6. Let the LLM uniformly output data_spec_text; retrieval_data does not perform deeper judgment.
    system = prompt_loader.load("retrieval_data/data_system_prompts.md")
    prompt = prompt_loader.render(
        "retrieval_data/data_user_prompts.md",
        user_query=pls.user_query,
        intent_json=pls.intent_json if intent else "null",
        data_info=data_info if data_info else "null",
        data_constraints=data_constraints if data_constraints is not None else "null",
        keyword_hits=json.dumps(keyword_hits, ensure_ascii=False),
        candidate_products=json.dumps(candidate_products, ensure_ascii=False),
        candidate_scenes=json.dumps(candidate_scenes, ensure_ascii=False),
        task_bbox=json.dumps(task_bbox, ensure_ascii=False) if task_bbox else "null",
        user_lang=pls.lang,
    )
    try:
        llm_out_model = llm.invoke_structured(
            system_prompt=system,
            user_prompt=prompt,
            schema=RetrievalDataOutput,
        )
        llm_out = llm_out_model.model_dump()
    except Exception:
        raw = llm.invoke(system_prompt=system, user_prompt=prompt)
        llm_out = _safe_json_object_from_llm(raw, dump_prefix="retrieval_data")
    # raw = llm.invoke(system_prompt=system, user_prompt=prompt)
    # llm_out = _safe_json_object_from_llm(raw, dump_prefix="retrieval_data")
    pls.task_bbox = llm_out.get("task_bbox", task_bbox)
    if not isinstance(llm_out, dict):
        llm_out = {"task_bbox": task_bbox, "recommendations": []}

    # 7. KnowledgeDoc
    if not isinstance(llm_out, dict):
        # In rare cases, the model may output a non-JSON object; fall back to an empty structure here.
        llm_out = {"task_bbox": task_bbox, "recommendations": []}
    # Fallback: recommendations must be a list.
    recommendations  = llm_out.get("recommendations", [])
    if not isinstance(recommendations , list):
        recommendations  = []
        llm_out["recommendations"] = recommendations

    # 8) Write data_docs. Do not overwrite; append directly to avoid overwriting docs written by previous modules such as intent.
    pls.data_docs = []
    for rec in recommendations:
        if not isinstance(rec, dict):
            continue
        pls.data_docs.append(
            KnowledgeDoc(
                source="retrieval_data",
                text=json.dumps(rec, ensure_ascii=False, indent=2),
            )
        )
    if pls.lang == "en":
        en_recommendations = llm_out.get("en_info", recommendations)
        tmp_docs: List[KnowledgeDoc] = []
        for rec in en_recommendations:

            if not isinstance(rec, dict):
                continue
            tmp_docs.append(
                KnowledgeDoc(
                    source="retrieval_data",
                    text=json.dumps(rec, ensure_ascii=False, indent=2),
                )
            )
        pls.en_info["data_docs"] = tmp_docs
    pls.trace["retrieval_data"] = {
        "keyword_hits": keyword_hits,
        "candidate_products_n": len(candidate_products),
        "recommendations_n": len(pls.data_docs),
        "task_bbox": task_bbox,
        "data_constraints_used": data_constraints,
        "need_scene_context": need_scene_context,
        "data_info_present": bool(data_info),
    }
    return pls