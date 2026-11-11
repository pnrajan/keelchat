"""Knowledge Service: documents on S3, vectors in LanceDB, embeddings from SiliconFlow (via litellm).

Indexing strategies: simple, contextual (Anthropic-style contextual retrieval), raptor, full_context.
Every dependency sits behind a port with an in-memory fake.
"""
