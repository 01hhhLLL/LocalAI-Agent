"""CPU BGE embeddings and Faiss search; results are candidates only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
MODEL_DIR = ROOT / "models" / "model-bge-small-zh-v1.5"
REGISTRY_DB = ROOT / "data" / "application-registry.sqlite3"
INDEX_DIR = ROOT / "data" / "application-registry-faiss"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def registry_rows() -> list[dict]:
    if not REGISTRY_DB.is_file():
        raise FileNotFoundError("application registry is missing")
    with closing(sqlite3.connect(REGISTRY_DB, timeout=10)) as database:
        rows = database.execute("""
            SELECT a.app_id, a.display_name, a.tool_name, l.alias
            FROM applications AS a JOIN aliases AS l ON l.app_id = a.app_id
            WHERE a.enabled = 1 AND a.approved = 1 AND l.approved = 1
            ORDER BY a.app_id, l.alias_key
        """).fetchall()
    return [{"app_id": app_id, "display_name": display_name,
             "tool_name": tool_name, "alias": alias}
            for app_id, display_name, tool_name, alias in rows]


def _fingerprint(rows: list[dict]) -> str:
    payload = json.dumps({"model": MODEL_REVISION, "rows": rows},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _corpus_text(row: dict) -> str:
    if row["alias"] == row["display_name"]:
        return row["alias"]
    return f"{row['alias']}；应用：{row['display_name']}"


def _model():
    required = (MODEL_DIR / "config.json", MODEL_DIR / "model.safetensors",
                MODEL_DIR / "tokenizer.json")
    if not all(path.is_file() for path in required):
        raise FileNotFoundError("pinned BGE model files are not present; run retrieval download")
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(min(4, max(1, os.cpu_count() or 1)))
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True)
    model = AutoModel.from_pretrained(MODEL_DIR, local_files_only=True)
    model.to("cpu")
    model.eval()
    return torch, tokenizer, model


def _encode(texts: list[str], model_bundle) -> "object":
    torch, tokenizer, model = model_bundle
    import numpy as np

    vectors = []
    for start in range(0, len(texts), 8):
        batch = texts[start:start + 8]
        encoded = tokenizer(batch, padding=True, truncation=True, max_length=512,
                            return_tensors="pt")
        with torch.inference_mode():
            hidden = model(**encoded).last_hidden_state[:, 0]
            normalized = torch.nn.functional.normalize(hidden, p=2, dim=1)
        vectors.append(normalized.cpu().numpy().astype(np.float32, copy=False))
    return np.ascontiguousarray(np.concatenate(vectors, axis=0), dtype=np.float32)


def _index(rows: list[dict], model_bundle):
    import faiss

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    fingerprint = _fingerprint(rows)
    stem = f"{fingerprint}-{MODEL_REVISION[:12]}"
    index_path = INDEX_DIR / f"{stem}.index"
    metadata_path = INDEX_DIR / f"{stem}.json"
    if index_path.is_file() and metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("fingerprint") == fingerprint and metadata.get("rows") == rows:
            index = faiss.read_index(str(index_path))
            if index.ntotal == len(rows) and index.d == 512:
                return index, rows, fingerprint

    vectors = _encode([_corpus_text(row) for row in rows], model_bundle)
    index = faiss.IndexFlatIP(512)
    index.add(vectors)
    if index_path.exists() or metadata_path.exists():
        suffix = uuid.uuid4().hex[:8]
        index_path = INDEX_DIR / f"{stem}-{suffix}.index"
        metadata_path = INDEX_DIR / f"{stem}-{suffix}.json"
    temp_index = INDEX_DIR / f".{uuid.uuid4().hex}.index.tmp"
    temp_metadata = INDEX_DIR / f".{uuid.uuid4().hex}.json.tmp"
    try:
        faiss.write_index(index, str(temp_index))
        temp_metadata.write_text(json.dumps({"fingerprint": fingerprint,
                                              "model_id": MODEL_ID,
                                              "model_revision": MODEL_REVISION,
                                              "rows": rows}, ensure_ascii=False,
                                             indent=2), encoding="utf-8")
        try:
            os.rename(temp_index, index_path)
            os.rename(temp_metadata, metadata_path)
        except OSError:
            if index_path.exists() and metadata_path.exists():
                return faiss.read_index(str(index_path)), rows, fingerprint
            raise
    finally:
        for temporary in (temp_index, temp_metadata):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return index, rows, fingerprint


def rebuild() -> dict:
    rows = registry_rows()
    if not rows:
        raise RuntimeError("no approved application aliases are registered")
    model_bundle = _model()
    index, _rows, fingerprint = _index(rows, model_bundle)
    return {"status": "ready", "applications": len({row['app_id'] for row in rows}),
            "aliases": len(rows), "faiss_vectors": index.ntotal,
            "device": "cpu", "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION, "registry_fingerprint": fingerprint}


def _clean_query(text: str) -> str:
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
        raise ValueError("query must contain 1 to 500 characters")
    query = text.strip()
    for phrase in ("请帮我", "麻烦帮我", "请", "麻烦", "帮我", "打开", "启动", "运行", "一下"):
        query = query.replace(phrase, " ")
    query = re.sub(r"\s+", " ", query).strip()
    return query or text.strip()


def query(text: str, top_k: int = 3) -> dict:
    if not 1 <= top_k <= 10:
        raise ValueError("top_k must be between 1 and 10")
    rows = registry_rows()
    if not rows:
        return {"status": "no_semantic_candidate", "candidates": [], "executes": False}
    model_bundle = _model()
    index, rows, fingerprint = _index(rows, model_bundle)
    vector = _encode([QUERY_INSTRUCTION + _clean_query(text)], model_bundle)
    scores, positions = index.search(vector, min(top_k, len(rows)))
    candidates = []
    seen = set()
    for score, position in zip(scores[0].tolist(), positions[0].tolist()):
        if position < 0:
            continue
        row = rows[position]
        if row["app_id"] in seen:
            continue
        seen.add(row["app_id"])
        candidates.append({"app_id": row["app_id"],
                           "display_name": row["display_name"],
                           "tool_name": row["tool_name"],
                           "matched_alias": row["alias"],
                           "score": round(float(score), 5),
                           "source": "bge_faiss_cpu"})
    return {"status": "candidate" if candidates else "no_semantic_candidate",
            "candidates": candidates, "executes": False,
            "registry_fingerprint": fingerprint, "device": "cpu"}


def download() -> dict:
    from huggingface_hub import snapshot_download

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID, revision=MODEL_REVISION,
        local_dir=MODEL_DIR,
        allow_patterns=("config.json", "model.safetensors", "tokenizer.json",
                        "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"),
    )
    return {"status": "downloaded", "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION, "model_dir": str(MODEL_DIR)}


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("download")
    commands.add_parser("rebuild")
    query_parser = commands.add_parser("query")
    query_parser.add_argument("text")
    query_parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()
    try:
        result = download() if args.command == "download" else (
            rebuild() if args.command == "rebuild" else query(args.text, args.top_k))
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__,
                          "error": str(error)[:300]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
