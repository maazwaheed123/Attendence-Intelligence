"""Retrieval: query understanding, structured SQL retrieval (Step 8), documents (Step 10).

RETRIEVAL_VERSION is stored with every response (and later with feedback examples),
so a change in retrieval behaviour is always traceable.
"""

RETRIEVAL_VERSION = "r1.0-hybrid"  # Step 10: RRF + dedupe + lexical rerank
