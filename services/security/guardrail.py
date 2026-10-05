import logging
import lancedb
from services.rag.config import LANCEDB_DIR, GUARDRAIL_THRESHOLD
from services.rag.embeddings import embedding_service

logger = logging.getLogger(__name__)

class GuardrailService:
    def __init__(self, db_dir=LANCEDB_DIR, threshold: float = GUARDRAIL_THRESHOLD):
        self.db_dir = db_dir
        self.threshold = threshold
        self.table_name = "guardrail_signatures"
        self._table = None
        
    @property
    def table(self):
        if self._table is None:
            try:
                db = lancedb.connect(str(self.db_dir))
                if self.table_name in db.table_names():
                    self._table = db.open_table(self.table_name)
                else:
                    logger.warning(f"Guardrail table '{self.table_name}' not found. Seeding required.")
            except Exception as e:
                logger.error(f"Failed to connect to LanceDB guardrail table: {e}")
        return self._table

    async def is_prompt_injection(self, text: str) -> bool:
        """
        Returns True if the text closely matches a known prompt injection signature.
        """
        if not text or not text.strip():
            return False
            
        table = self.table
        if table is None:
            return False
            
        try:
            # Embed the user input
            vectors = await embedding_service.get_embeddings([text])
            if not vectors or len(vectors) == 0:
                return False
                
            query_vector = vectors[0]
            
            # Search LanceDB using Cosine distance
            results = table.search(query_vector).metric("cosine").limit(1).to_pandas()
            
            if len(results) > 0:
                # Cosine distance: 0 is identical, 1 is orthogonal
                # Similarity = 1 - distance
                distance = results.iloc[0]["_distance"]
                similarity = 1.0 - distance
                
                if similarity >= self.threshold:
                    matched_text = results.iloc[0]["text"]
                    logger.warning(f"Guardrail triggered! Similarity: {similarity:.3f} | Matched: '{matched_text[:50]}...'")
                    return True
                    
        except Exception as e:
            logger.error(f"Error during guardrail check: {e}")
            
        return False

# Singleton instance
guardrail_service = GuardrailService()
