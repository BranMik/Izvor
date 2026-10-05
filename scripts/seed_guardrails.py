import asyncio
import os
import sys
from pathlib import Path

# Add project root to sys.path so we can import from services
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasets import load_dataset
import lancedb
import pyarrow as pa
from services.rag.config import LANCEDB_DIR, EMBEDDING_DIM
from services.rag.embeddings import EmbeddingService

async def seed_guardrails():
    print("Loading deepset/prompt-injections dataset from HuggingFace...")
    # Load dataset
    dataset = load_dataset("deepset/prompt-injections", split="train")
    
    # Filter for actual injections (label == 1)
    injections = dataset.filter(lambda x: x["label"] == 1)
    texts = [row["text"] for row in injections]
    
    print(f"Found {len(texts)} prompt injection examples.")
    
    print("Connecting to LanceDB...")
    db = lancedb.connect(str(LANCEDB_DIR))
    
    schema = pa.schema([
        pa.field("id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM))
    ])
    
    table_name = "guardrail_signatures"
    if table_name in db.table_names():
        print(f"Dropping existing {table_name} table...")
        db.drop_table(table_name)
        
    table = db.create_table(table_name, schema=schema)
    
    print("Initializing EmbeddingService...")
    embedder = EmbeddingService()
    
    # Process in batches to respect rate limits and show progress
    batch_size = 50
    total_inserted = 0
    
    print("Embedding and inserting vectors...")
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i:i+batch_size]
        print(f"Processing batch {i//batch_size + 1} ({len(batch_texts)} items)...")
        
        # Get embeddings
        vectors = await embedder.get_embeddings(batch_texts)
        
        # Prepare data for insertion
        data = []
        for j, (text, vector) in enumerate(zip(batch_texts, vectors)):
            data.append({
                "id": f"inj_{i+j}",
                "text": text,
                "vector": vector
            })
            
        # Insert into LanceDB
        table.add(data)
        total_inserted += len(data)
        
        # Add a delay to prevent rate limits
        await asyncio.sleep(1.0)
        
    print(f"Successfully seeded {total_inserted} guardrail signatures into LanceDB!")

if __name__ == "__main__":
    asyncio.run(seed_guardrails())
