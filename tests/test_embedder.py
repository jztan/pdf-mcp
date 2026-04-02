"""Unit tests for pdf_mcp.embedder. All tests mock fastembed — no model download."""
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def test_check_available_raises_when_fastembed_missing():
    """check_available() raises ImportError with install hint when fastembed absent."""
    import pdf_mcp.embedder as emb

    # Setting a sys.modules entry to None blocks the import
    with patch.dict(sys.modules, {"fastembed": None}):
        with pytest.raises(ImportError, match="pip install 'pdf-mcp\\[semantic\\]'"):
            emb.check_available()
